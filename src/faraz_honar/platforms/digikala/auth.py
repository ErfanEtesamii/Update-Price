"""DigiKala seller-API token lifecycle (spec sections 5 and 6).

Pattern (access token + refresh token, access token good for ~1 hour) and
the request shape are taken from the reference script you tested manually
(New_Token.py: POST {base_url}/open-api/v1/auth/refresh-token with
{"access_token": ..., "refresh_token": ...}, response has
data.access_token / data.refresh_token).

This is fully implemented but INERT by default: `refresh()` refuses to make
a real network call unless `enable_refresh=True` is explicitly passed
(wired from DIGIKALA_ENABLE_TOKEN_REFRESH, default false). That is the
"prepare it but do not execute it yet" switch from spec sections 5/6/15.

No token value is ever hardcoded here — bootstrap tokens come from
environment variables, and refreshed tokens are persisted to a
git-ignored local JSON file (see config.Settings.digikala_tokens_file / digikala2_tokens_file).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from faraz_honar.core.exceptions import ConfigurationError, PlatformAuthError

logger = logging.getLogger(__name__)

REFRESH_TOKEN_PATH = "/open-api/v1/auth/refresh-token"
_DEFAULT_EXPIRY_BUFFER_SECONDS = 60


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str


class TokenManager:
    def __init__(
        self,
        *,
        base_url: str,
        tokens_file: Path,
        bootstrap_access_token: str | None,
        bootstrap_refresh_token: str | None,
        enable_refresh: bool,
        timeout_seconds: float = 15.0,
        user_agent: str | None = None,
    ):
        self._user_agent = user_agent
        self._base_url = base_url.rstrip("/")
        self._tokens_file = tokens_file
        self._bootstrap_access_token = bootstrap_access_token
        self._bootstrap_refresh_token = bootstrap_refresh_token
        self._enable_refresh = enable_refresh
        self._timeout = timeout_seconds
        self._cached: TokenPair | None = None
        # Deliberately separate from _cached: callers outside this class
        # (see platforms/digikala/client.py::_refresh_or_raise) invalidate
        # _cached right before calling refresh(), so _resync_from_shared_file
        # can never use _cached as "the token we had before this resync
        # attempt" — it would always see None and its staleness check would
        # never fire. This field is updated every time current_tokens()
        # resolves a token and is NOT cleared by invalidate_cache(), so it
        # still holds the last real value across that pre-emptive clear.
        self._last_seen_access_token: str | None = None

    def _load_from_file(self) -> TokenPair | None:
        if not self._tokens_file.exists():
            return None
        try:
            data = json.loads(self._tokens_file.read_text(encoding="utf-8"))
            return TokenPair(access_token=data["access_token"], refresh_token=data["refresh_token"])
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Ignoring unreadable tokens file %s: %s", self._tokens_file, exc)
            return None

    def _save_to_file(self, tokens: TokenPair) -> None:
        self._tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_file.write_text(
            json.dumps({"access_token": tokens.access_token, "refresh_token": tokens.refresh_token}),
            encoding="utf-8",
        )
        os.chmod(self._tokens_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner read/write only

    def current_tokens(self) -> TokenPair:
        """Best-known token pair: cache, then the persisted file, then the
        bootstrap env vars. Raises ConfigurationError if none is available.
        """
        if self._cached:
            self._last_seen_access_token = self._cached.access_token
            return self._cached

        from_file = self._load_from_file()
        if from_file:
            self._cached = from_file
            self._last_seen_access_token = self._cached.access_token
            return self._cached

        if self._bootstrap_access_token and self._bootstrap_refresh_token:
            self._cached = TokenPair(
                access_token=self._bootstrap_access_token,
                refresh_token=self._bootstrap_refresh_token,
            )
            self._last_seen_access_token = self._cached.access_token
            return self._cached

        raise ConfigurationError(
            "No DigiKala access token available: set DIGIKALA_ACCESS_TOKEN and "
            "DIGIKALA_REFRESH_TOKEN, or run once with tokens already saved to "
            f"{self._tokens_file}."
        )

    def refresh(self) -> TokenPair:
        """Exchange the current (expired) access token + refresh token for a
        new pair, per the tested reference flow. NOT executed unless
        enable_refresh=True.

        DigiKala's refresh_token is rotating/single-use, so two independent
        processes must never both call the real refresh endpoint for the
        same account — whichever refreshes second invalidates the token the
        other just obtained. order-sync-platform owns real refreshing for
        both DigiKala accounts this project also uses; enable_refresh stays
        false here for that reason (see config.Settings.order_sync_data_dir).
        When disabled, a 401 here does not mean *this* process should
        refresh — it means the shared tokens_file may already have a newer
        pair written by order-sync-platform that this process just hasn't
        picked up yet, so resync from that file instead of calling the API.
        """
        if not self._enable_refresh:
            return self._resync_from_shared_file()

        current = self.current_tokens()
        url = f"{self._base_url}{REFRESH_TOKEN_PATH}"
        payload = {"access_token": current.access_token, "refresh_token": current.refresh_token}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._user_agent:
            headers["User-Agent"] = self._user_agent

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise PlatformAuthError(f"Token refresh request failed: {exc}", retryable=True) from exc

        if response.status_code != 200:
            raise PlatformAuthError(
                f"Token refresh failed: HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
                retryable=False,
            )

        data = response.json().get("data", {})
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token")
        if not new_access or not new_refresh:
            raise PlatformAuthError(
                f"Token refresh response missing expected fields: {response.text}",
                retryable=False,
            )

        tokens = TokenPair(access_token=new_access, refresh_token=new_refresh)
        self._cached = tokens
        self._save_to_file(tokens)
        logger.info("DigiKala access token refreshed successfully")
        return tokens

    def _resync_from_shared_file(self) -> TokenPair:
        """Drop the in-memory cache and re-read tokens_file, on the chance
        order-sync-platform (the process that actually owns refreshing —
        see refresh()'s docstring) has already written a newer pair since
        we last read it. No network call, so it can never race that
        process's own refresh.

        Raises ConfigurationError if the reload comes back with the exact
        same access token we already had (order-sync-platform evidently
        hasn't refreshed yet either — this project cannot make that happen
        by itself) or if no token is available at all.
        """
        # _last_seen_access_token, not _cached: a caller (e.g.
        # client.py::_refresh_or_raise) may already have invalidated the
        # cache before calling refresh(), which would make _cached always
        # None here and this staleness check never fire — see that field's
        # own docstring above.
        previous_access_token = self._last_seen_access_token
        self.invalidate_cache()
        new = self.current_tokens()
        if previous_access_token is not None and new.access_token == previous_access_token:
            raise ConfigurationError(
                "DigiKala token refresh is disabled in this project "
                "(DIGIKALA_ENABLE_TOKEN_REFRESH is not true) — refreshing is "
                f"order-sync-platform's job. Re-read {self._tokens_file} but "
                "it still has the same (expired) token, so that project "
                "likely hasn't refreshed yet either; will retry on the next "
                "request."
            )
        logger.info(
            "DigiKala token resynced from shared tokens file %s (owned by "
            "order-sync-platform)",
            self._tokens_file,
        )
        return new

    def invalidate_cache(self) -> None:
        """Force the next current_tokens() call to re-read from disk/env."""
        self._cached = None

    def is_expiring(self, buffer_seconds: int = _DEFAULT_EXPIRY_BUFFER_SECONDS) -> bool:
        """Best-effort local check of the current access token's JWT `exp`
        claim, so an already-expired token can trigger a refresh BEFORE
        wasting a real API call that's certain to 401 (as opposed to only
        ever reacting to a 401 after the fact).

        Never raises: any failure to determine expiry (no token configured,
        not a parseable JWT, no `exp` claim) returns False so the caller
        falls back to the existing reactive 401-triggered refresh — this is
        purely an optimization, not a new source of truth.
        """
        try:
            token = self.current_tokens().access_token
            exp = _decode_jwt_exp(token)
            if exp is None:
                return False
            return time.time() >= (exp - buffer_seconds)
        except Exception:  # noqa: BLE001 - best-effort by design, never blocks the caller
            return False


def _decode_jwt_exp(token: str) -> float | None:
    """Read the `exp` claim out of a JWT's payload segment without verifying
    the signature — this never makes a trust decision, it only tells us
    whether it's worth trying the token at all before a real request.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None
