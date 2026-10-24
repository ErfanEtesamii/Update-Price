"""User-facing platform names — the single source of truth.

Every name a person sees in Telegram (keyboard buttons, report titles, "who
started an update" notices) comes from PLATFORM_DISPLAY_NAMES below. Names are
Finglish (Latin script), each prefixed with a colored-circle emoji marking its
platform family (🔴 Digikala, 🟣 SnappShop, 🟠 TapsiShop, 🔵 Basalam, 🟢 the
Faraz Honar site); keyboard buttons additionally add the cart icon on top of
these (see telegram_bot/keyboards.py), so a button and the report it leads to
can never drift apart.

Platform KEYS (the dict keys) are the machine names used everywhere else: the
platform registry, the state DB, the sheet mapping's platform_ref_columns.

    digikala   = the main DigiKala seller account   (sheet column "دیجی کالا اصلی")
    digikala2  = the second, fast DigiKala account  (sheet column "دیجی کالا سریع")

To add a platform: add one line here and register its client in
platforms/registry.py.
"""
from __future__ import annotations

PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "digikala": "🔴 Digikala",
    "digikala2": "🔴 Digikala Saree",
    "snappshop_tehran": "🟣 SnappShop Tehran",
    "snappshop_isfahan": "🟣 SnappShop Isfahan",
    "tapsi_shop": "🟠 TapsiShop",
    "basalam": "🔵 Basalam",
    "woocommerce": "🟢 Site Faraz Honar",
}


def platform_display_name(platform: str) -> str:
    return PLATFORM_DISPLAY_NAMES.get(platform, platform.title())


def platform_plain_name(platform: str) -> str:
    """Same as platform_display_name but without the leading colored-circle
    emoji — used inline after a product name (e.g. "Widget (Digikala #123)")
    where the emoji would be visual clutter repeated on every line, unlike
    the report title/summary lines above it where it stays.
    """
    name = platform_display_name(platform)
    _, _, rest = name.partition(" ")
    return rest or name
