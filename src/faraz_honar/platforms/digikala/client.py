"""DigiKala Seller Open API integration (spec section 4), against the
official OAS3 spec you provided (`GET /open-api/v1/`, path list + schemas).

Endpoints used, all under `/open-api/v1`:
  - `GET /variants` (params `search[search_term]`, `page`, `size`) to find the
    seller's variant for a given DigiKala product, per its documented
    description: "Search in variant id, title and product id, title".
  - `GET /variants/{variant_id}` as a fallback detail fetch.
  - `PUT /variants/{variant_id}` with `{"selling_price": ...}` to update price
    — this is your manually-tested reference script's exact endpoint, and the
    spec confirms it (PUT body's `selling_price` is optional, no
    `credit_increase_percentage` required). The spec also lists
    `PATCH /variants/selling-price`, which additionally REQUIRES
    `credit_increase_percentage` — a DigiKala-specific installment-price
    concept with no business rule defined for it here, so that endpoint is
    deliberately NOT used; ask if you want it instead.
    CONFIRMED against a real response: DigiKala rejects `selling_price`
    with HTTP 400 ("دو رقم آخر قیمت باید ۰۰ باشد" — "the last two digits
    of the price must be 00") unless it's an exact multiple of 100 Rial.
    The 12%-cap computation (`floor(reference_price * 1.12)` in
    core/pricing.py, which must NOT be touched) can produce a value that
    isn't, even from a clean target price — see update_price, which rounds
    DOWN to the nearest 100 before sending (never rounds up, so it never
    sends a price higher than what the pricing algorithm decided).

Field mapping (confirmed by the spec's own field descriptions):
  - `data.items[].id` = "variant id" -> this is the VARIANT_ID.
  - `data.items[].price_list` = "reference price" -> this is what the 12%
    rule compares against (spec section 7), fetched live, not from the sheet.
  - `data.items[].product_id` / `product_url` -> matched against the sheet's
    DKP code (see links.py) to find the right item; the spec doesn't document
    a direct "search by DKP code" filter, so both fields are checked.

DRY_RUN semantics (updated): reads are allowed, writes are not. Resolving a
variant and reading its live `price_list` make real GET requests even when
DRY_RUN=true, so the 12% calculation can be verified against real DigiKala
data. `update_price` (the only write) still short-circuits before any
request whenever DRY_RUN=true — that is the one thing DRY_RUN exists to
block, and it stays blocked exactly as before.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from faraz_honar.core.exceptions import (
    DigikalaReferencePriceRejected,
    PlatformAPIError,
    PlatformAuthError,
    VariantResolutionError,
)
from faraz_honar.core.models import Product
from faraz_honar.platforms.base import PlatformClient
from faraz_honar.platforms.digikala.auth import TokenManager
from faraz_honar.platforms.digikala.links import extract_dkp_code

logger = logging.getLogger(__name__)

_VARIANTS_PATH = "/open-api/v1/variants"
_MAX_SEARCH_PAGES = 5
_SEARCH_PAGE_SIZE = 50

# The exact validation message DigiKala returns when selling_price is
# outside its own reference-price bracket (errors.selling_price[0]).
# Substring match (not exact-equality) in case DigiKala wraps/prefixes it.
_REFERENCE_PRICE_RANGE_ERROR_FA = "قیمت فروش باید در بازه قیمت مرجع باشد"

# HTTP methods this client ever sends that count as a "write" for rate-limit
# throttling purposes (currently only PUT, for update_price). Everything
# else (GET) is a "read". POST/PATCH are listed defensively in case a future
# write endpoint is added here; DigiKala token refresh's own POST call goes
# through TokenManager, not this client, so it's unaffected either way.
_WRITE_METHODS = frozenset({"PUT", "PATCH", "POST", "DELETE"})

# Whole-catalog listing used by the read-only price check (see
# DigikalaClient.prepare_price_check): one request per _SEARCH_PAGE_SIZE
# variants instead of one search (or several) per product.
_MAX_CATALOG_PAGES = 100

# HTTP 429 handling (see DigikalaClient._request).
_RATE_LIMIT_MAX_RETRIES = 4
_RATE_LIMIT_BACKOFF_SECONDS = 5.0  # used when DigiKala sends no Retry-After; doubles per retry
_RATE_LIMIT_MAX_WAIT_SECONDS = 60.0  # upper bound for any single wait
_RATE_LIMIT_MAX_DELAY_FACTOR = 4.0  # adaptive delay never grows beyond base * this ...
_RATE_LIMIT_MIN_MAX_DELAY_SECONDS = 5.0  # ... or this, whichever is larger
_RATE_LIMIT_RECOVERY_STREAK = 20  # successful requests in a row before the delay shrinks again


class _SharedCooldown:
    """A "stop sending requests until ..." deadline shared by every DigikalaClient
    that talks to the same DigiKala host.

    "digikala" and "digikala2" are separate seller accounts but share one
    host (and one machine/IP), and a price check runs them in parallel threads.
    When one of them is told to slow down (HTTP 429) the other is very likely
    about to be told the same, so both pause. Only a 429 ever sets the
    deadline, so nothing is delayed in normal operation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0  # time.monotonic() deadline; 0.0 = never rate limited

    def wait(self) -> None:
        if self._until == 0.0:  # never rate limited: don't even read the clock
            return
        remaining = self._until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def trigger(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        with self._lock:
            if deadline > self._until:
                self._until = deadline


_SHARED_COOLDOWNS: dict[str, _SharedCooldown] = {}
_SHARED_COOLDOWNS_LOCK = threading.Lock()


def _shared_cooldown_for(base_url: str) -> _SharedCooldown:
    with _SHARED_COOLDOWNS_LOCK:
        return _SHARED_COOLDOWNS.setdefault(base_url, _SharedCooldown())


def reset_shared_rate_limits() -> None:
    """Forget every shared cooldown (used by tests so one test's 429 can't make
    another test wait)."""
    with _SHARED_COOLDOWNS_LOCK:
        _SHARED_COOLDOWNS.clear()


def _retry_after_seconds(response: requests.Response) -> float | None:
    """DigiKala's `Retry-After` header in seconds, or None when it is missing or
    not a plain number (an HTTP-date form is ignored; the backoff covers it)."""
    headers = getattr(response, "headers", None)
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:  # noqa: BLE001 - never let header parsing break a request
        return None
    if not isinstance(raw, (str, int, float)):
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


class DigikalaClient(PlatformClient):
    """One DigiKala seller account. Every account (digikala, digikala2, ...) reuses
    this exact same class — the only thing that differs between accounts is
    the `name` (used to look up the right sheet column and token set) and
    the `token_manager` instance passed in (see platforms/registry.py). No
    account-specific subclass or second implementation is needed.
    """

    # update_price rounds DOWN to a multiple of 100 Rial (DigiKala rejects
    # anything else), so the price check compares against that rounded value.
    price_step_rial = 100

    def __init__(
        self,
        *,
        name: str = "digikala",
        base_url: str,
        token_manager: TokenManager,
        dry_run: bool,
        timeout_seconds: float = 15.0,
        read_delay_seconds: float = 1.0,
        write_delay_seconds: float = 2.0,
        rate_limit_max_retries: int = _RATE_LIMIT_MAX_RETRIES,
        rate_limit_backoff_seconds: float = _RATE_LIMIT_BACKOFF_SECONDS,
        user_agent: str | None = None,
    ):
        self.name = name
        # DigiKala's Open API only accepts its own "DigikalaTrustedBot/
        # SelfToken/<seller code>" User-Agent (see config.py); None keeps the
        # library default (used by tests).
        self._user_agent = user_agent
        self._base_url = base_url.rstrip("/")
        self._token_manager = token_manager
        self._dry_run = dry_run
        self._timeout = timeout_seconds
        # Short-lived, per-process cache of the last variant item resolved for
        # a given variant_id, so get_reference_price doesn't need a second
        # API round trip right after resolve_variant_id found it. Always
        # re-resolved fresh on the next resolve_variant_id call (e.g. each
        # hourly recheck), so this never serves stale prices across runs.
        self._last_resolved_item_by_variant: dict[str, dict] = {}
        # Rate-limit throttling state (see _throttle). Reads and writes are
        # tracked independently; None means "no request of this kind sent
        # yet", so the very first one of each kind is never delayed.
        self._read_delay_seconds = read_delay_seconds
        self._write_delay_seconds = write_delay_seconds
        self._last_read_request_at: float | None = None
        self._last_write_request_at: float | None = None
        # HTTP 429 handling: wait (Retry-After, else exponential backoff) and
        # retry the SAME request instead of failing the product, slow the
        # throttle down while DigiKala keeps pushing back, and pause the other
        # DigiKala account too (shared host). 0 retries = the old behaviour
        # (a 429 is returned to the caller as a failure).
        self._rate_limit_max_retries = max(0, rate_limit_max_retries)
        self._rate_limit_backoff_seconds = rate_limit_backoff_seconds
        self._base_read_delay_seconds = read_delay_seconds
        self._base_write_delay_seconds = write_delay_seconds
        self._ok_streak = {"read": 0, "write": 0}
        self._shared_cooldown = _shared_cooldown_for(self._base_url)
        # Price-check only: variants of the whole account, indexed by DKP code,
        # loaded by prepare_price_check (None = not loaded -> search per product).
        self._catalog_by_dkp: dict[str, list[dict]] | None = None

    # -- Variant resolution ------------------------------------------------------
    def resolve_variant_id(self, product: Product) -> str:
        raw_ref = product.platform_ref(self.name)
        if not raw_ref:
            raise VariantResolutionError(
                f"Product '{product.internal_id}' has no DigiKala reference in the sheet's "
                f"'{self.name}' column.",
                retryable=False,
            )

        dkp_code = extract_dkp_code(raw_ref)

        # Reads are allowed under DRY_RUN (DRY_RUN only blocks writes — see
        # update_price). This is a real GET request regardless of DRY_RUN.
        item = self._find_variant_by_dkp_code(dkp_code, product)
        variant_id = str(item["id"])
        self._last_resolved_item_by_variant[variant_id] = item
        return variant_id

    def _find_variant_by_dkp_code(self, dkp_code: str, product: Product) -> dict:
        # A DKP code can map to several variants of the seller (e.g. an old,
        # deactivated one next to the live one). Only a variant that is
        # active AND sold on the DigiKala channel may be priced: the stale
        # one has a different (lower) reference price, so comparing the
        # target against it makes DigiKala reject the update with the
        # reference-price-range error. Stop paging as soon as a sellable
        # match is found (saves requests -> fewer 429s); keep looking only
        # while all matches so far are inactive.
        indexed = self._catalog_by_dkp.get(dkp_code) if self._catalog_by_dkp is not None else None
        if indexed:
            # Price check with a preloaded catalog: no request at all for this
            # product. Same choice rules as a search result.
            sellable = [i for i in indexed if _is_sellable(i)]
            if sellable:
                return _pick_variant(sellable, dkp_code, product)
            # only inactive variants listed -> fall through to the normal search
            # (which also covers the "fall back to the first inactive one" case).

        inactive_matches: list[dict] = []
        for page in range(1, _MAX_SEARCH_PAGES + 1):
            response = self._request(
                "GET",
                _VARIANTS_PATH,
                params={"search[search_term]": dkp_code, "page": page, "size": _SEARCH_PAGE_SIZE},
            )
            self._raise_for_error_status(response, context="variant search")
            body = response.json().get("data", {})
            items = body.get("items", [])

            sellable = [i for i in items if _item_matches_dkp_code(i, dkp_code) and _is_sellable(i)]
            if sellable:
                return _pick_variant(sellable, dkp_code, product)
            inactive_matches.extend(i for i in items if _item_matches_dkp_code(i, dkp_code))

            total_pages = body.get("pager", {}).get("total_pages", page)
            if page >= total_pages:
                break

        if inactive_matches:
            logger.warning(
                "DigiKala %s: DKP %s (product '%s') only has inactive variant(s); "
                "falling back to the first one.",
                self.name,
                dkp_code,
                product.internal_id,
            )
            return inactive_matches[0]

        raise VariantResolutionError(
            f"No DigiKala variant found matching DKP code '{dkp_code}' (product "
            f"'{product.internal_id}') via GET {_VARIANTS_PATH}?search[search_term]="
            f"{dkp_code}.",
            retryable=False,
        )

    # -- Reference price -----------------------------------------------------------
    def get_reference_price(self, product: Product, variant_id: str) -> int:
        # Reads are allowed under DRY_RUN (see resolve_variant_id) — this may
        # make a real GET request (variant detail fetch) regardless of DRY_RUN.
        item = self._last_resolved_item_by_variant.get(variant_id)
        if item is None:
            item = self._fetch_variant_detail(variant_id)

        price_list = item.get("price_list")
        if price_list is None:
            raise PlatformAPIError(
                f"DigiKala variant {variant_id} response has no 'price_list' "
                "(reference price) field.",
                retryable=False,
            )
        return int(price_list)

    def get_current_price(self, product: Product, variant_id: str) -> int:
        """Read-only (price-check button): the variant's real SELLING price.

        get_reference_price returns `price_list` (DigiKala's reference bracket,
        which is not what update_price changes). The price update writes
        `selling_price`, and DigiKala echoes it back as `price_sale` (see
        update_price's confirmation log), so that is the field to compare with
        the sheet. Uses the item cached by resolve_variant_id; only fetches the
        variant detail when `price_sale` is missing from the list item.
        """
        item = self._last_resolved_item_by_variant.get(variant_id)
        if item is None or item.get("price_sale") is None:
            item = self._fetch_variant_detail(variant_id)
        price_sale = item.get("price_sale")
        if price_sale is None:
            raise PlatformAPIError(
                f"DigiKala variant {variant_id} response has no 'price_sale' "
                "(selling price) field.",
                retryable=False,
            )
        return int(price_sale)

    def _fetch_variant_detail(self, variant_id: str) -> dict:
        response = self._request("GET", f"{_VARIANTS_PATH}/{variant_id}")
        self._raise_for_error_status(response, context="variant detail fetch")
        return response.json().get("data", {})

    # -- Read-only price check: read the account's variants in bulk ---------------
    def prepare_price_check(self) -> None:
        """Lists the account's variants page by page ONCE and indexes them by DKP
        code, so checking N products costs about N/50 requests instead of at
        least N (one search each, up to 5 pages for a product that is not
        found). Far fewer requests = far fewer HTTP 429s.

        Purely an optimisation: if the listing fails (or a product is not in
        it) the check silently falls back to the per-product search, exactly as
        before. Never raises. Only the price check calls this; price updates
        keep resolving variants one by one.
        """
        self._catalog_by_dkp = None
        index: dict[str, list[dict]] = {}
        try:
            for page in range(1, _MAX_CATALOG_PAGES + 1):
                response = self._request(
                    "GET", _VARIANTS_PATH, params={"page": page, "size": _SEARCH_PAGE_SIZE}
                )
                self._raise_for_error_status(response, context="variant catalog listing")
                body = response.json().get("data", {})
                for item in body.get("items", []):
                    for code in _dkp_codes_of_item(item):
                        index.setdefault(code, []).append(item)
                total_pages = body.get("pager", {}).get("total_pages", page)
                if page >= total_pages:
                    break
        except Exception as exc:  # noqa: BLE001 - optimisation only; fall back to searching
            logger.warning(
                "DigiKala %s: could not list the variant catalog for the price check (%s); "
                "falling back to one search per product.",
                self.name,
                exc,
            )
            return
        self._catalog_by_dkp = index
        logger.info(
            "DigiKala %s: price check preloaded %d DKP code(s) from the variant catalog.",
            self.name,
            len(index),
        )

    def finish_price_check(self) -> None:
        self._catalog_by_dkp = None
        self._last_resolved_item_by_variant.clear()

    # -- Price update (confirmed contract, from your tested script + the spec) -----
    def update_price(self, variant_id: str, new_price: int) -> None:
        # DigiKala requires selling_price to be a multiple of 100 Rial (see
        # module docstring). Rounding DOWN never sends a price higher than
        # what the pricing algorithm (core/pricing.py) decided.
        price = _round_down_to_multiple_of_hundred(new_price)

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would PUT selling_price=%s to DigiKala variant %s (no request sent)",
                price,
                variant_id,
            )
            return

        response = self._request(
            "PUT", f"{_VARIANTS_PATH}/{variant_id}", json_body={"selling_price": price}
        )
        self._raise_for_error_status(response, context="price update")

        # Verify and log the response (spec requirement: "verify and properly
        # handle the API response") rather than trusting the HTTP status alone.
        try:
            body = response.json()
        except ValueError:
            logger.warning(
                "DigiKala price update for variant %s returned HTTP %s but a non-JSON body: %r",
                variant_id,
                response.status_code,
                response.text[:500],
            )
            return

        confirmed_price = body.get("data", {}).get("price_sale")
        logger.info(
            "DigiKala variant %s price update confirmed: requested selling_price=%s, "
            "response price_sale=%s",
            variant_id,
            price,
            confirmed_price,
        )

    # -- Shared request/auth plumbing -----------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        allow_refresh: bool = True,
    ) -> requests.Response:
        # Proactive check: if the access token's own JWT `exp` claim shows it's
        # already expired, refresh BEFORE wasting a request that's certain to
        # 401 (see TokenManager.is_expiring — local decode only, no network
        # call). `allow_refresh` is cleared immediately after so this counts
        # as THE one refresh permitted for this request — the reactive 401
        # check below cannot also trigger a second refresh for the same call.
        if allow_refresh and self._token_manager.is_expiring():
            logger.info("DigiKala access token appears expired; refreshing before %s %s", method, path)
            self._refresh_or_raise(context=f"{method} {path} (proactive, token expired)")
            allow_refresh = False

        url = f"{self._base_url}{path}"
        token = self._token_manager.current_tokens().access_token
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._user_agent:
            headers["User-Agent"] = self._user_agent

        rate_limit_retries = 0
        while True:
            # Another request (maybe from the OTHER DigiKala account) may have
            # just been told to slow down: wait that out before sending.
            self._shared_cooldown.wait()
            self._throttle(method)
            try:
                response = requests.request(
                    method, url, headers=headers, json=json_body, params=params, timeout=self._timeout
                )
            except requests.RequestException as exc:
                raise PlatformAPIError(
                    f"DigiKala request failed ({method} {path}): {exc}", retryable=True
                ) from exc

            if response.status_code == 401 and allow_refresh:
                self._refresh_or_raise(context=f"{method} {path} (401 response)")
                # allow_refresh=False on the retry: at most one refresh per
                # original request, whichever path triggered it — never a loop.
                return self._request(
                    method, path, json_body=json_body, params=params, allow_refresh=False
                )

            if response.status_code == 429 and self._rate_limit_max_retries > 0:
                wait = self._on_rate_limited(method, response, rate_limit_retries)
                if rate_limit_retries >= self._rate_limit_max_retries:
                    logger.warning(
                        "DigiKala %s: %s %s still rate limited (HTTP 429) after %d retries; "
                        "giving up on this request (all DigiKala requests pause %.0fs).",
                        self.name,
                        method,
                        path,
                        rate_limit_retries,
                        wait,
                    )
                    return response
                rate_limit_retries += 1
                logger.warning(
                    "DigiKala %s: HTTP 429 on %s %s; waiting %.0fs, then retrying (%d/%d).",
                    self.name,
                    method,
                    path,
                    wait,
                    rate_limit_retries,
                    self._rate_limit_max_retries,
                )
                continue  # the wait itself happens in _shared_cooldown.wait() above

            if response.status_code != 429:
                self._on_request_ok(method)
            return response

    def _on_rate_limited(self, method: str, response: requests.Response, retries_so_far: int) -> float:
        """DigiKala said "too many requests": pause EVERY DigiKala request (both
        accounts) for the time it asked for — `Retry-After`, else an
        exponential backoff — and slow this client's own spacing down so the
        rest of the run stays under the limit. Returns the pause in seconds."""
        wait = _retry_after_seconds(response)
        if wait is None:
            wait = self._rate_limit_backoff_seconds * (2**retries_so_far)
        wait = min(wait, _RATE_LIMIT_MAX_WAIT_SECONDS)
        self._shared_cooldown.trigger(wait)

        kind = "write" if method in _WRITE_METHODS else "read"
        self._ok_streak[kind] = 0
        base = self._base_write_delay_seconds if kind == "write" else self._base_read_delay_seconds
        current = self._write_delay_seconds if kind == "write" else self._read_delay_seconds
        ceiling = max(base * _RATE_LIMIT_MAX_DELAY_FACTOR, _RATE_LIMIT_MIN_MAX_DELAY_SECONDS)
        slowed = min(max(current * 1.5, 1.0), ceiling)
        if kind == "write":
            self._write_delay_seconds = slowed
        else:
            self._read_delay_seconds = slowed
        return wait

    def _on_request_ok(self, method: str) -> None:
        """After a long streak of accepted requests, give back the extra spacing
        added by _on_rate_limited (never below the configured delay)."""
        kind = "write" if method in _WRITE_METHODS else "read"
        base = self._base_write_delay_seconds if kind == "write" else self._base_read_delay_seconds
        current = self._write_delay_seconds if kind == "write" else self._read_delay_seconds
        if current <= base:
            return
        self._ok_streak[kind] += 1
        if self._ok_streak[kind] < _RATE_LIMIT_RECOVERY_STREAK:
            return
        self._ok_streak[kind] = 0
        eased = max(base, current * 0.8)
        if kind == "write":
            self._write_delay_seconds = eased
        else:
            self._read_delay_seconds = eased

    def _throttle(self, method: str) -> None:
        """Sleep just long enough to respect the configured delay since the
        last DigiKala request of the same kind (read vs write) — a simple,
        centralized rate-limit precaution to reduce HTTP 429s (spec: keep
        this at the HTTP-request layer, not scattered across business
        methods). Reads and writes are throttled independently against
        DIGIKALA_READ_DELAY_SECONDS / DIGIKALA_WRITE_DELAY_SECONDS, and the
        very first request of a kind is never delayed since there is no
        prior timestamp to measure against.
        """
        is_write = method in _WRITE_METHODS
        delay = self._write_delay_seconds if is_write else self._read_delay_seconds
        last_at = self._last_write_request_at if is_write else self._last_read_request_at

        now = time.monotonic()
        if last_at is None or delay <= 0:
            new_last_at = now
        else:
            remaining = delay - (now - last_at)
            if remaining > 0:
                time.sleep(remaining)
            # Anchor the new timestamp to the target instant (last_at +
            # delay) rather than re-reading the clock, so throttling stays
            # exact even though time.sleep() is itself imprecise/patchable.
            new_last_at = max(now, last_at + delay)

        if is_write:
            self._last_write_request_at = new_last_at
        else:
            self._last_read_request_at = new_last_at

    def _refresh_or_raise(self, *, context: str) -> None:
        self._token_manager.invalidate_cache()
        try:
            self._token_manager.refresh()
        except Exception as refresh_exc:  # noqa: BLE001
            raise PlatformAuthError(
                f"DigiKala access token expired/invalid and refresh could not be completed "
                f"({context}): {refresh_exc}",
                status_code=401,
                retryable=False,
            ) from refresh_exc

    @staticmethod
    def _raise_for_error_status(response: requests.Response, *, context: str) -> None:
        if response.status_code in (200, 201):
            return
        if response.status_code == 401:
            raise PlatformAuthError(
                f"DigiKala rejected the access token even after refresh ({context}): "
                f"{response.text}",
                status_code=401,
                retryable=False,
            )
        if response.status_code == 400 and context == "price update":
            try:
                body = response.json()
            except ValueError:
                body = {}
            selling_price_errors = body.get("errors", {}).get("selling_price", [])
            if any(_REFERENCE_PRICE_RANGE_ERROR_FA in msg for msg in selling_price_errors):
                raise DigikalaReferencePriceRejected(
                    f"DigiKala rejected the requested selling_price for this "
                    f"variant as outside its current reference-price range "
                    f"({context}): {response.text}",
                    status_code=400,
                    retryable=False,
                )
        # 429 (rate limited) is deliberately NOT retryable: retrying
        # immediately only makes the rate-limiting worse. Treat it as a
        # failure for this run, same as any other non-retryable error —
        # the next scheduled run (or hourly recheck) will try again.
        # 5xx (server-side) and network errors remain retryable — capped at
        # PriceUpdateService's max_retry_attempts (1 retry, 2 attempts total).
        retryable = response.status_code >= 500
        raise PlatformAPIError(
            f"DigiKala {context} failed: HTTP {response.status_code}: {response.text}",
            status_code=response.status_code,
            retryable=retryable,
        )


def _round_down_to_multiple_of_hundred(rial: int) -> int:
    # DigiKala requires selling_price to be a multiple of 100 Rial ("دو رقم
    # آخر قیمت باید ۰۰ باشد" — confirmed against a real HTTP 400 response).
    # Rounding down (never up) guarantees the sent price never exceeds what
    # the pricing algorithm decided.
    return (rial // 100) * 100


def _dkp_codes_of_item(item: dict) -> set[str]:
    """Every DKP code an item can be matched by (its product_id and the code in
    its product_url) -- the same two fields _item_matches_dkp_code looks at."""
    codes: set[str] = set()
    product_id = str(item.get("product_id", "") or "")
    if product_id:
        codes.add(product_id)
    product_url = item.get("product_url")
    if product_url:
        try:
            codes.add(extract_dkp_code(product_url))
        except VariantResolutionError:
            pass
    return codes


def _item_matches_dkp_code(item: dict, dkp_code: str) -> bool:
    if str(item.get("product_id", "")) == dkp_code:
        return True
    product_url = item.get("product_url")
    if product_url:
        try:
            return extract_dkp_code(product_url) == dkp_code
        except VariantResolutionError:
            return False
    return False


def _is_sellable(item: dict) -> bool:
    """Active variant that is switched on for the DigiKala sales channel.
    A missing field counts as OK so partial/older responses behave as before."""
    if item.get("active") is False:
        return False
    channel = item.get("variant_selling_chanel") or {}
    return channel.get("active_digikala") is not False


def _pick_variant(sellable: list[dict], dkp_code: str, product: Product) -> dict:
    if len(sellable) > 1:
        # Genuinely multi-variant product (e.g. several colors). Nothing in
        # the sheet says which one is meant, so keep the first and make the
        # ambiguity visible instead of guessing silently.
        logger.warning(
            "DKP %s (product '%s') has %d active variants %s; using the first.",
            dkp_code,
            product.internal_id,
            len(sellable),
            [i.get("id") for i in sellable],
        )
    return sellable[0]