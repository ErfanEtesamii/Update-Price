"""Central application settings, loaded from environment variables / .env.

Nothing here has a real secret baked in. Missing values are left as None /
empty and callers that actually need them raise a clear ConfigurationError
at the point of use (see core.exceptions) rather than at import time, so the
app can still start and report its own readiness.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Number of TELEGRAM_CHAT_ID_N / TELEGRAM_CHAT_NAME_N slots (N = 1..10).
_NUMBERED_CHAT_SLOTS = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # General
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")

    # Google Sheets
    google_sheet_id: str | None = Field(default=None, alias="GOOGLE_SHEET_ID")
    google_sheet_worksheet_gid: int = Field(default=0, alias="GOOGLE_SHEET_WORKSHEET_GID")
    # Alternative to the gid above: select a tab by its visible name (e.g.
    # "محصولات سایت جدید"). Takes precedence over the gid when set.
    google_sheet_worksheet_name: str | None = Field(default=None, alias="GOOGLE_SHEET_WORKSHEET_NAME")
    google_service_account_file: Path | None = Field(
        default=None, alias="GOOGLE_SERVICE_ACCOUNT_FILE"
    )
    product_source_csv_path: Path | None = Field(default=None, alias="PRODUCT_SOURCE_CSV_PATH")

    @field_validator("google_service_account_file", "product_source_csv_path", mode="before")
    @classmethod
    def _blank_path_is_unset(cls, value: object) -> object:
        # Pydantic coerces an empty env-var string to Path(".") (truthy!),
        # not None. Left blank (as .env.example instructs), that would make
        # the app silently think a CSV/key path IS configured and use the
        # wrong product source instead of failing with a clear error.
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("admin_chat_id", mode="before")
    @classmethod
    def _blank_admin_chat_id_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    # DigiKala has two independent seller accounts, both using the exact same
    # DigikalaClient/TokenManager code (see platforms/registry.py):
    #
    #   platform key  sheet column        Settings fields      .env variables
    #   ------------  ------------------  -------------------  ----------------------------
    #   digikala      "دیجی کالا اصلی"    digikala_*           DIGIKALA_ASLI_*
    #   digikala2     "دیجی کالا سریع"    digikala2_*          DIGIKALA_*  (no "ASLI")
    #
    # IMPORTANT: the platform keys and Settings field names were renamed, but
    # the .env VARIABLE NAMES were deliberately left exactly as they were, so
    # an existing .env keeps working and the two accounts' tokens can never
    # get swapped by accident. base_url and the rate-limit/retry settings
    # further below are shared by both accounts (same API); only
    # credentials/tokens differ. Never the same access/refresh tokens, never
    # the same tokens file, so refreshing one account can never touch the
    # other's tokens.
    digikala_base_url: str = Field(default="https://seller.digikala.com", alias="DIGIKALA_BASE_URL")

    # Since 2026-10-11 DigiKala's Open API rejects every ordinary User-Agent
    # (python-requests included) with 401 "Invalid User-Agent!". Digikala
    # support's instruction (2026-10-12): send the User-Agent
    # "DigikalaTrustedBot/SelfToken/<seller code>" - one seller code per
    # account: 570891 = main ("دیجی کالا اصلی", platform "digikala"),
    # 1385180 = fast ("دیجی کالا سریع", platform "digikala2").
    digikala_user_agent: str = Field(
        default="DigikalaTrustedBot/SelfToken/570891", alias="DIGIKALA_MAIN_USER_AGENT"
    )
    digikala2_user_agent: str = Field(
        default="DigikalaTrustedBot/SelfToken/1385180", alias="DIGIKALA_FAST_USER_AGENT"
    )

    # Main account -> platform key "digikala" (env vars keep their old DIGIKALA_ASLI_ names).
    digikala_access_token: str | None = Field(default=None, alias="DIGIKALA_ASLI_ACCESS_TOKEN")
    digikala_refresh_token: str | None = Field(default=None, alias="DIGIKALA_ASLI_REFRESH_TOKEN")
    digikala_enable_token_refresh: bool = Field(
        default=False, alias="DIGIKALA_ASLI_ENABLE_TOKEN_REFRESH"
    )

    # Fast account -> platform key "digikala2" (env vars keep their old plain DIGIKALA_ names).
    digikala2_access_token: str | None = Field(default=None, alias="DIGIKALA_ACCESS_TOKEN")
    digikala2_refresh_token: str | None = Field(default=None, alias="DIGIKALA_REFRESH_TOKEN")
    digikala2_enable_token_refresh: bool = Field(
        default=False, alias="DIGIKALA_ENABLE_TOKEN_REFRESH"
    )

    # Cross-project token ownership: DigiKala's refresh_token is rotating/
    # single-use, and "order-sync-platform" (the sibling
    # "Local Marketplace Order Synchronization Platform" project, sharing
    # both accounts) is the process responsible for actually calling
    # DigiKala's refresh endpoint for both accounts — see its
    # src/marketplaces/digikala.py (main/اصلی, its DIGIKALA_* account =
    # this project's "digikala") and digikala2.py (fast/سریع, its
    # DIGIKALA2_* account = this project's "digikala2"). This project must
    # never refresh (digikala_enable_token_refresh/digikala2_enable_token_
    # refresh stay false) and instead reads the token pair
    # order-sync-platform already wrote, by pointing digikala_tokens_file/
    # digikala2_tokens_file at *that* project's own data/ folder — an
    # absolute path, since the two projects are separate checkouts on the
    # same machine. Leave unset only if order-sync-platform isn't deployed
    # alongside this project (the two files then fall back to this
    # project's own local data dir, as before).
    order_sync_data_dir: Path | None = Field(default=None, alias="ORDER_SYNC_DATA_DIR")

    # Minimum spacing enforced between consecutive DigiKala requests of the
    # same kind (read vs write), to reduce HTTP 429 rate-limiting — see
    # platforms/digikala/client.py. Reads and writes are throttled
    # independently, and the very first request of each kind is never
    # delayed.
    digikala_read_delay_seconds: float = Field(default=1.0, alias="DIGIKALA_READ_DELAY_SECONDS")
    digikala_write_delay_seconds: float = Field(default=2.0, alias="DIGIKALA_WRITE_DELAY_SECONDS")

    # SnappShop Tehran (اسنپ‌شاپ تهران) — a separate platform, its own
    # permanent Bearer token (no refresh flow), its own 6-character
    # vendor/seller code, and its own agent-user identifier. Uses the
    # official SnappShop Automation API (/automation/v1/...), not the
    # earlier reverse-engineered endpoints. See platforms/snappshop/client.py
    # — the vendor code and agent user are NOT interchangeable (the vendor
    # code identifies the store and is used in the URL; the agent user
    # identifies the calling agent and is sent only via the User-Agent
    # header).
    snappshop_base_url: str = Field(
        default="https://apix.snappshop.ir/automation/v1", alias="SNAPPSHOP_BASE_URL"
    )
    snappshop_token: str | None = Field(default=None, alias="SNAPPSHOP_TOKEN")
    snappshop_vendor_code: str | None = Field(default=None, alias="SNAPPSHOP_VENDOR_CODE")
    snappshop_agent_user: str | None = Field(default=None, alias="SNAPPSHOP_AGENT_USER")
    # Optional HTTP(S) proxy (e.g. an Iranian exit IP) for SnappShop
    # requests only — see platforms/snappshop/client.py's Iran-IP handling.
    snappshop_proxy_url: str | None = Field(default=None, alias="SNAPPSHOP_PROXY_URL")
    snappshop_read_delay_seconds: float = Field(default=1.0, alias="SNAPPSHOP_READ_DELAY_SECONDS")
    snappshop_write_delay_seconds: float = Field(default=2.0, alias="SNAPPSHOP_WRITE_DELAY_SECONDS")

    # SnappShop Isfahan (اسنپ شاپ اصفهان) — a second, independent SnappShop
    # seller account, reusing the exact same SnappShopClient code as Tehran
    # above (see platforms/registry.py) — base_url and the shared
    # rate-limit settings are the same (same API), but never the same
    # token/vendor code/agent user, so the two accounts can never be
    # confused with each other.
    snappshop_isfahan_token: str | None = Field(default=None, alias="SNAPPSHOP_ISFAHAN_TOKEN")
    snappshop_isfahan_vendor_code: str | None = Field(
        default=None, alias="SNAPPSHOP_ISFAHAN_VENDOR_CODE"
    )
    snappshop_isfahan_agent_user: str | None = Field(
        default=None, alias="SNAPPSHOP_ISFAHAN_AGENT_USER"
    )

    # TapsiShop (تپسی شاپ) — a separate platform, its own vendor-gateway
    # base URL and a single static token (no refresh flow). See
    # platforms/tapsishop/client.py. Unlike SnappShop, TapsiShop's API is
    # already Rial-native — no Toman<->Rial conversion is needed here.
    tapsi_base_url: str = Field(default="https://vendorgw.tapsi.shop", alias="TAPSI_BASE_URL")
    tapsi_token: str | None = Field(default=None, alias="TAPSI_TOKEN")
    tapsi_read_delay_seconds: float = Field(default=1.0, alias="TAPSI_READ_DELAY_SECONDS")
    tapsi_write_delay_seconds: float = Field(default=2.0, alias="TAPSI_WRITE_DELAY_SECONDS")

    # Basalam (باسلام) — the store "فراز هنر" (vendor_id 1058040, hardcoded
    # in platforms/basalam/client.py — not configurable here since this
    # implementation is for this one specific store). A single static
    # access token (no refresh flow). See platforms/basalam/client.py.
    # Basalam's API is already Rial-native — no Toman<->Rial conversion is
    # needed here.
    basalam_base_url: str = Field(
        default="https://openapi.basalam.com/v1", alias="BASALAM_BASE_URL"
    )
    basalam_access_token: str | None = Field(default=None, alias="BASALAM_ACCESS_TOKEN")
    basalam_read_delay_seconds: float = Field(default=1.0, alias="BASALAM_READ_DELAY_SECONDS")
    basalam_write_delay_seconds: float = Field(default=2.0, alias="BASALAM_WRITE_DELAY_SECONDS")

    # WooCommerce (وردپرس) — the Faraz Honar WordPress/WooCommerce store
    # (farazhonar.com). A single static consumer key/secret pair (no
    # refresh flow). See platforms/woocommerce/client.py. WooCommerce's
    # API is already Rial-native — no Toman<->Rial conversion is needed
    # here. IMPORTANT: farazhonar.com's firewall blocks the standard
    # HTTP Basic Auth WooCommerce normally expects, so the consumer
    # key/secret are sent as URL query parameters instead — see the
    # client module docstring.
    woocommerce_base_url: str = Field(
        default="https://farazhonar.com/wp-json/wc/v3", alias="WOOCOMMERCE_BASE_URL"
    )
    woocommerce_consumer_key: str | None = Field(default=None, alias="WOOCOMMERCE_CONSUMER_KEY")
    woocommerce_consumer_secret: str | None = Field(
        default=None, alias="WOOCOMMERCE_CONSUMER_SECRET"
    )
    woocommerce_read_delay_seconds: float = Field(
        default=1.0, alias="WOOCOMMERCE_READ_DELAY_SECONDS"
    )
    woocommerce_write_delay_seconds: float = Field(
        default=2.0, alias="WOOCOMMERCE_WRITE_DELAY_SECONDS"
    )

    # Telegram
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_ids: str = Field(default="", alias="TELEGRAM_ALLOWED_USER_IDS")
    # Optional HTTP(S)/SOCKS proxy for api.telegram.org only (e.g.
    # http://127.0.0.1:10809). Blank = direct connection (previous behavior).
    telegram_proxy_url: str | None = Field(default=None, alias="TELEGRAM_PROXY_URL")
    # Connectivity/crash alerts sent to ADMIN_CHAT_ID (falls back to the first
    # TELEGRAM_ALLOWED_USER_IDS entry). Set to false to silence them.
    telegram_alerts_enabled: bool = Field(default=True, alias="TELEGRAM_ALERTS_ENABLED")
    # Minimum minutes between two connectivity alerts (outage storms = 1 alert).
    telegram_alert_cooldown_minutes: int = Field(default=30, alias="TELEGRAM_ALERT_COOLDOWN_MINUTES")
    # Timeout (seconds) for every Telegram request. The old default was 5s,
    # far too low for a slow/unstable link.
    telegram_request_timeout_seconds: float = Field(default=30.0, alias="TELEGRAM_REQUEST_TIMEOUT_SECONDS")
    # Comma-separated chat IDs that should receive the bot's reports
    # (hourly recheck report, and every manual-run message: start banner,
    # DRY_RUN banner, final report, stopped-by-user message) — see
    # telegram_bot/sending.py::broadcast_message_with_retry. Separate from
    # TELEGRAM_ALLOWED_USER_IDS above, which is about who may send commands
    # to the bot, not who receives its reports. When left blank, existing
    # behavior is preserved exactly: a manual run reports back only to the
    # chat that triggered it, and the hourly recheck reports to
    # TELEGRAM_ALLOWED_USER_IDS, same as before this setting existed.
    # NOTE: the unexpected-error message is NOT included here — it goes to
    # ADMIN_CHAT_ID only (falling back to this list when ADMIN_CHAT_ID is
    # blank), so regular team members no longer see raw error notices. See
    # telegram_bot/handlers.py::_execute_run.
    telegram_chat_ids: str = Field(default="", alias="TELEGRAM_CHAT_IDS")
    # Numbered per-person chat slots: TELEGRAM_CHAT_ID_1 .. TELEGRAM_CHAT_ID_10,
    # each an optional single numeric chat id, paired with the same-numbered
    # optional display name TELEGRAM_CHAT_NAME_1 .. TELEGRAM_CHAT_NAME_10 (e.g. a
    # Persian name shown in "who did what" notices). Purely additive — the
    # comma-separated TELEGRAM_ALLOWED_USER_IDS / TELEGRAM_CHAT_IDS above are
    # unchanged. See numbered_chat_ids_set and display_name() below.
    # Chat id of the one person who should also get every manual run's
    # final report even if not already in TELEGRAM_CHAT_IDS /
    # TELEGRAM_CHAT_ID_1..10 — they see the exact same report text as
    # everyone else (no separate admin-only detail; see
    # telegram_bot/reporting.py::format_final_digikala_report). Left blank,
    # no extra recipient is added and behavior is unchanged. See
    # telegram_bot/handlers.py::_send_final_report.
    admin_chat_id: int | None = Field(default=None, alias="ADMIN_CHAT_ID")
    telegram_chat_id_1: str = Field(default="", alias="TELEGRAM_CHAT_ID_1")
    telegram_chat_id_2: str = Field(default="", alias="TELEGRAM_CHAT_ID_2")
    telegram_chat_id_3: str = Field(default="", alias="TELEGRAM_CHAT_ID_3")
    telegram_chat_id_4: str = Field(default="", alias="TELEGRAM_CHAT_ID_4")
    telegram_chat_id_5: str = Field(default="", alias="TELEGRAM_CHAT_ID_5")
    telegram_chat_id_6: str = Field(default="", alias="TELEGRAM_CHAT_ID_6")
    telegram_chat_id_7: str = Field(default="", alias="TELEGRAM_CHAT_ID_7")
    telegram_chat_id_8: str = Field(default="", alias="TELEGRAM_CHAT_ID_8")
    telegram_chat_id_9: str = Field(default="", alias="TELEGRAM_CHAT_ID_9")
    telegram_chat_id_10: str = Field(default="", alias="TELEGRAM_CHAT_ID_10")
    telegram_chat_name_1: str = Field(default="", alias="TELEGRAM_CHAT_NAME_1")
    telegram_chat_name_2: str = Field(default="", alias="TELEGRAM_CHAT_NAME_2")
    telegram_chat_name_3: str = Field(default="", alias="TELEGRAM_CHAT_NAME_3")
    telegram_chat_name_4: str = Field(default="", alias="TELEGRAM_CHAT_NAME_4")
    telegram_chat_name_5: str = Field(default="", alias="TELEGRAM_CHAT_NAME_5")
    telegram_chat_name_6: str = Field(default="", alias="TELEGRAM_CHAT_NAME_6")
    telegram_chat_name_7: str = Field(default="", alias="TELEGRAM_CHAT_NAME_7")
    telegram_chat_name_8: str = Field(default="", alias="TELEGRAM_CHAT_NAME_8")
    telegram_chat_name_9: str = Field(default="", alias="TELEGRAM_CHAT_NAME_9")
    telegram_chat_name_10: str = Field(default="", alias="TELEGRAM_CHAT_NAME_10")

    # Business rules
    max_price_increase_pct: float = Field(default=0.12, alias="MAX_PRICE_INCREASE_PCT")
    # 2 = 1 retry, for retryable errors only (5xx/network). Non-retryable
    # errors (HTTP 429, auth, config, invalid data) stop after 1 attempt
    # regardless of this value — see platforms/digikala/client.py and
    # update/service.py::_update_one.
    max_retry_attempts: int = Field(default=2, alias="MAX_RETRY_ATTEMPTS")
    # Extra "error rounds" per platform run (update/service.py::_run_products):
    # after the first full pass, products that failed ONLY because of a
    # transient error (HTTP 408/425/429/5xx, timeout, network drop - see
    # core/retry.py::is_transient_error) are retried, round after round,
    # as long as the number of such products keeps shrinking. A cooldown
    # pause (interruptible by Cancel) lets rate limits clear between rounds.
    # False = exactly one pass, as before this feature existed. This is
    # separate from MAX_RETRY_ATTEMPTS above, which only governs the quick
    # in-place retry of a single product inside _update_one.
    error_rounds_enabled: bool = Field(default=True, alias="ERROR_ROUNDS_ENABLED")
    error_rounds_cooldown_seconds: float = Field(
        default=60.0, ge=0, alias="ERROR_ROUNDS_COOLDOWN_SECONDS"
    )
    # The hourly re-attempt of 12%-capped products (and its Telegram report)
    # is OFF by default: nobody asked for a separate hourly message, it made
    # the status menu show a phantom "running, 0 / 0" platform, and it changes
    # prices in the background. Capped products are already listed in every
    # manual run's final report. Set HOURLY_RECHECK_ENABLED=true to turn the
    # old behaviour back on.
    hourly_recheck_enabled: bool = Field(default=False, alias="HOURLY_RECHECK_ENABLED")
    hourly_recheck_minutes: int = Field(default=60, alias="HOURLY_RECHECK_MINUTES")
    # Scheduler offset (2026-09, part of the token/rate-limit isolation
    # work alongside DIGIKALA_ENABLE_TOKEN_REFRESH - see auth.py):
    # order-sync-platform's main poll job fires every POLL_INTERVAL_SECONDS
    # (default 120s) starting the instant its process starts. This bot's
    # hourly recheck job also fires relative to ITS OWN process start
    # (JobQueue.run_repeating's `first=`), so if both processes are ever
    # started at close to the same moment (e.g. both as Windows services
    # starting on boot), the two would land on the exact same wall-clock
    # second every single cycle - not a token conflict any more (that's
    # solved), but still two requests to the same marketplace endpoint at
    # once for no reason. This offset is simply added on top of the first
    # run's delay. Because HOURLY_RECHECK_MINUTES*60 is a whole multiple
    # of order-sync-platform's default 120s poll interval, a non-zero,
    # non-multiple-of-120 offset here (default 45s) keeps every future
    # hourly run permanently out of phase with that cadence too, not just
    # the first one. If POLL_INTERVAL_SECONDS is ever changed on the other
    # project to something this offset happens to be a multiple of,
    # adjust this value to no longer be one.
    schedule_offset_seconds: int = Field(default=45, alias="SCHEDULE_OFFSET_SECONDS")

    @property
    def telegram_allowed_user_ids_set(self) -> set[int]:
        return {
            int(part.strip())
            for part in self.telegram_allowed_user_ids.split(",")
            if part.strip()
        }

    @property
    def telegram_chat_ids_set(self) -> set[int]:
        return {
            int(part.strip())
            for part in self.telegram_chat_ids.split(",")
            if part.strip()
        }

    def _numbered_chat_entries(self) -> list[tuple[int, str]]:
        """(chat_id, display_name) for every filled TELEGRAM_CHAT_ID_N slot,
        in slot order. Empty/blank id slots are skipped; a blank name is
        returned as "" (the display_name() fallback handles it).
        """
        entries: list[tuple[int, str]] = []
        for n in range(1, _NUMBERED_CHAT_SLOTS + 1):
            raw_id = getattr(self, f"telegram_chat_id_{n}").strip()
            if not raw_id:
                continue
            entries.append((int(raw_id), getattr(self, f"telegram_chat_name_{n}").strip()))
        return entries

    @property
    def numbered_chat_ids_set(self) -> set[int]:
        return {chat_id for chat_id, _ in self._numbered_chat_entries()}

    def display_name(self, chat_id: int) -> str:
        """The configured TELEGRAM_CHAT_NAME_N paired with this chat id's
        TELEGRAM_CHAT_ID_N slot, or str(chat_id) when there is none.
        """
        for entry_id, name in self._numbered_chat_entries():
            if entry_id == chat_id and name:
                return name
        return str(chat_id)

    @property
    def digikala_tokens_file(self) -> Path:
        # Main account ("digikala", اصلی) == order-sync-platform's primary
        # DIGIKALA_* account, whose TokenManager (src/marketplaces/
        # digikala.py there) writes to <that project>/data/
        # digikala_tokens.json. Separate file from digikala2_tokens_file
        # below — the two accounts' tokens are never read from or written to
        # the same file. The local fallback file name (tokens_asli.json) is
        # kept as it was so tokens already saved on disk are still found.
        if self.order_sync_data_dir is not None:
            return self.order_sync_data_dir / "digikala_tokens.json"
        return self.data_dir / "tokens_asli.json"

    @property
    def digikala2_tokens_file(self) -> Path:
        # Fast account ("digikala2", سریع) == order-sync-platform's
        # DIGIKALA2_* account, whose TokenManager (src/marketplaces/
        # digikala2.py there) writes to <that project>/data/
        # digikala2_tokens.json. See order_sync_data_dir above — this
        # project only ever reads that file, never writes it. The local
        # fallback file name (tokens.json) is kept as it was.
        if self.order_sync_data_dir is not None:
            return self.order_sync_data_dir / "digikala2_tokens.json"
        return self.data_dir / "tokens.json"

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state.db"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Load settings fresh (cheap; avoids stale cache across tests)."""
    return Settings()