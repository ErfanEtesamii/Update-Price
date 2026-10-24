# Faraz Honar — Multi-Platform Price Updater

Reads product prices from a Google Sheet and updates them on seller
platforms (DigiKala first) through their APIs, respecting a maximum 12%
price-increase-per-update rule, with Telegram as the control/reporting
interface. Python only — no web UI.

## Status

The full documented flow is implemented against the real DigiKala OAS3
spec you provided: sheet product -> DigiKala link -> DKP code ->
`GET /open-api/v1/variants?search[search_term]=<dkp>` -> matched variant's
`id` (VARIANT_ID) and `price_list` (live reference price) ->  12% cap ->
`PUT /open-api/v1/variants/{id}` (your tested endpoint, confirmed official).
Token refresh is implemented against your tested reference script. Proven
end-to-end with fully mocked HTTP in `tests/test_digikala_integration.py` —
no real DigiKala request has ever been made by this code.

`DRY_RUN=true` (the default) blocks **every** real DigiKala HTTP call —
reads (variant search/detail) as well as the price-update write — per your
instruction not to make any real request yet. With `DRY_RUN=true`, every
product will correctly fail at the variant-resolution step with a message
saying so; that's expected, not a bug. Token refresh additionally requires
`DIGIKALA_ENABLE_TOKEN_REFRESH=true` before it will ever fire, independent
of `DRY_RUN`.

One deliberate judgment call, not 100% verifiable without a live call: the
list response has both `id` and `product_variant_id` fields per item — the
spec's own field description confirms `id` = "variant id", so that's what's
used. Worth a sanity check on your first live run.

## Project layout

```
src/faraz_honar/
  config.py              # Settings (env vars / .env)
  core/                  # models, 12% pricing rule, retry helper, exceptions
  sheets/                # Google Sheet <-> Product mapping (+ CSV fallback)
  platforms/             # PlatformClient interface; digikala/, woocommerce/, etc. implementations
  state/                 # SQLite: per-product/platform outcome history
  update/service.py      # orchestrates sheet -> pricing -> platform -> state
  telegram_bot/          # bot handlers, platform-selection keyboard, reports
  scheduler/             # hourly recheck job (12%-capped products; off unless HOURLY_RECHECK_ENABLED=true)
  main.py                # entrypoint: python -m faraz_honar.main
config/sheet_mapping.yaml # sheet column-name -> field mapping (edit, don't touch code)
tests/                    # pytest — all platform/Telegram calls are mocked
```

Adding a second platform = writing a `platforms/<name>/client.py` that
implements `platforms.base.PlatformClient` and registering it in
`platforms/registry.py`. Nothing else changes.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # then fill in real values, see below
```

Run tests / lint / types:

```bash
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
```

Run the app (requires `TELEGRAM_BOT_TOKEN` at minimum; will otherwise exit
with a clear error naming what's missing):

```bash
.venv/bin/python -m faraz_honar.main
```

## Configuration (`.env`)

See `.env.example` for the full list with comments. Summary:

| Variable | Purpose |
|---|---|
| `DRY_RUN` | `true` = never send real price-update requests (default) |
| `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` | live Google Sheets access |
| `PRODUCT_SOURCE_CSV_PATH` | dev-only: read products from a local CSV instead |
| `DIGIKALA_ACCESS_TOKEN`, `DIGIKALA_REFRESH_TOKEN` | bootstrap tokens (first run only; refreshed tokens persist to `DATA_DIR/tokens.json`) |
| `DIGIKALA_ENABLE_TOKEN_REFRESH` | must be `true` for the refresh call to ever fire |
| `WOOCOMMERCE_CONSUMER_KEY`, `WOOCOMMERCE_CONSUMER_SECRET` | Faraz Honar WooCommerce store (farazhonar.com) REST API credentials |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | bot control interface + allowlist |
| `TELEGRAM_CHAT_ID_1`..`TELEGRAM_CHAT_ID_10`, `TELEGRAM_CHAT_NAME_1`..`TELEGRAM_CHAT_NAME_10` | optional numbered per-person chats (see below) |
| `MAX_PRICE_INCREASE_PCT`, `MAX_RETRY_ATTEMPTS`, `HOURLY_RECHECK_MINUTES` | business-rule knobs, default to spec values (0.12, 3, 60) |

### Numbered chats (`TELEGRAM_CHAT_ID_N` / `TELEGRAM_CHAT_NAME_N`)

Fully optional and additive — leaving all of `TELEGRAM_CHAT_ID_1..10` unset
keeps the bot's authorization and messages exactly as before this feature.
When set, each `TELEGRAM_CHAT_ID_N` (N = 1..10) is:

- **Authorized** to use the bot, on top of anyone already listed in
  `TELEGRAM_ALLOWED_USER_IDS` (the two lists are unioned; the allowlist is
  still fail-closed if both are empty).
- **Notified** with a short "who did what" heads-up — e.g. `🔔 آقای اعتصامی
  به‌روزرسانی قیمت‌های دیجی‌کالا را شروع کرد.` — whenever *any* configured
  person starts a platform price update, except the person who triggered it.
  Sending is best-effort and never blocks or fails the actual price-update
  run.

`TELEGRAM_CHAT_NAME_N` is an optional display name (e.g. a Persian name)
shown in that notice for the matching `TELEGRAM_CHAT_ID_N`; if left blank,
the raw chat id is shown instead. See `.env.example` for the exact format.

## Still needed from you

1. **Target price column confirmation** — `config/sheet_mapping.yaml`
   currently guesses `target_price_column: "ق 140411"` based on the header
   text pattern in your sample sheet. Confirm or correct it.
2. **Google service-account credentials** — create a Google Cloud service
   account, download its JSON key, share the sheet with its `client_email`
   (Viewer is enough), set `GOOGLE_SERVICE_ACCOUNT_FILE` to the key path.
3. **DigiKala access token** (and refresh token) — you said you'd send these
   separately. Put them in `.env` as `DIGIKALA_ACCESS_TOKEN` /
   `DIGIKALA_REFRESH_TOKEN` (or `DATA_DIR/tokens.json`).
4. **Your go-ahead to flip `DRY_RUN=false`** once you want a real (still
   just read-only at first, if you like) DigiKala call — nothing else is
   blocking that switch.
5. Telegram bot token and allowed user ID are already set in your local
   `.env` (git-ignored) — nothing further needed there.
