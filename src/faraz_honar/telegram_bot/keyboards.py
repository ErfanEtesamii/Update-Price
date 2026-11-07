"""Keyboards for the Telegram control panel: a persistent main-menu/platform
reply keyboard (the primary UI — see handlers.py's menu_message_handler),
plus the original inline platform-selection keyboard (still independently
usable/tested; not part of the persistent-menu flow).
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from faraz_honar.platform_names import PLATFORM_DISPLAY_NAMES, platform_display_name

CALLBACK_PREFIX = "update:"
ALL_PLATFORMS_KEY = "all"

# -- Persistent reply-keyboard menu (spec: "professional, persistent control
# panel") ---------------------------------------------------------------------

BTN_SELECT_PLATFORM = "📦 انتخاب پلتفرم"
BTN_LAST_STATUS = "📊 گزارش"
# Read-only: compares every platform's live prices with the sheet and lists the
# products whose price differs (handlers._handle_check_prices).
BTN_CHECK_PRICES = "🔍 چک و بررسی قیمت‌ها"
BTN_STATUS = "📊 وضعیت اجرا"
BTN_STOP = "⏹ توقف اجرا"
BTN_BACK = "🔙 بازگشت"

# Arms the bot to accept the NEXT .xlsx document sent in this chat as the new
# product source (see handlers.py::document_message_handler); the file is
# used in place of a Google Sheet for every subsequent run/check.
BTN_SEND_EXCEL = "📤 ارسال اکسل محصولات"

# Last platform button in the "📦 انتخاب پلتفرم" submenu: runs every platform
# one after another (see handlers._start_all_platforms_run).
BTN_ALL_PLATFORMS = "🚀 همه‌ی پلتفرم‌ها"

# Destructive, low-frequency operation — shares a row with BTN_STOP so the
# main-menu buttons stay compact; a confirmation step (بله/خیر) still guards it
# (see build_main_menu_keyboard and handlers._handle_clear_database).
BTN_CLEAR_DATABASE = "🧹 پاکسازی دیتابیس"

# Yes/No confirmation buttons, reused for both the parallel-run warning and
# the stop-run warning (handlers._PENDING_CONFIRMATION_KEY tracks which one
# a given "بله"/"خیر" reply belongs to).
BTN_CONFIRM_YES = "✅ بله"
BTN_CONFIRM_NO = "❌ خیر"

# Seven platforms are implemented today (see platforms/registry.py). Button
# labels are derived from platform_names.PLATFORM_DISPLAY_NAMES (the one place
# a platform's user-facing name lives), so a button and the report it leads to
# can never drift apart.

# Every platform button starts with the same cart icon.
PLATFORM_BUTTON_ICON = "🛒"

# Button label -> platform key. Matching is exact, so a stale button still
# showing an OLD label in someone's Telegram client (e.g. "🛒 DigiKala" from
# before the rename, which used to mean the fast account) matches nothing and
# just re-shows the menu — it can never start the wrong account.
_PLATFORM_BUTTONS: dict[str, str] = {
    f"{PLATFORM_BUTTON_ICON} {display_name}": platform
    for platform, display_name in PLATFORM_DISPLAY_NAMES.items()
}


# How many platform buttons share one row of the "📦 انتخاب پلتفرم" submenu.
_SUBMENU_BUTTONS_PER_ROW = 2


def platform_button_label(platform: str) -> str:
    for label, name in _PLATFORM_BUTTONS.items():
        if name == platform:
            return label
    return platform.title()


def platform_from_button_label(label: str) -> str | None:
    return _PLATFORM_BUTTONS.get(label)


def build_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_SELECT_PLATFORM, BTN_CHECK_PRICES],
            [BTN_LAST_STATUS, BTN_STATUS],
            [BTN_SEND_EXCEL],
            [BTN_STOP, BTN_CLEAR_DATABASE],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_platform_submenu_keyboard(platforms: list[str]) -> ReplyKeyboardMarkup:
    # Two platform buttons per row, so the keyboard stays short instead of
    # covering half the chat (7 platforms = 4 rows rather than 7).
    labels = [platform_button_label(p) for p in platforms]
    rows = [labels[i : i + _SUBMENU_BUTTONS_PER_ROW] for i in range(0, len(labels), _SUBMENU_BUTTONS_PER_ROW)]
    # Only worth offering when there is more than one platform to chain.
    if len(platforms) > 1:
        rows.append([BTN_ALL_PLATFORMS])
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def build_confirm_keyboard() -> ReplyKeyboardMarkup:
    """The بله/خیر keyboard shown for the parallel-run and stop-run
    warnings. Not persistent — it's swapped back to the main menu keyboard
    as soon as the person answers."""
    return ReplyKeyboardMarkup(
        [[BTN_CONFIRM_YES, BTN_CONFIRM_NO]], resize_keyboard=True, is_persistent=False
    )


def build_platform_keyboard(platforms: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(platform_display_name(p), callback_data=f"{CALLBACK_PREFIX}{p}")]
        for p in platforms
    ]
    if platforms:
        all_button = InlineKeyboardButton(
            "All Platforms", callback_data=f"{CALLBACK_PREFIX}{ALL_PLATFORMS_KEY}"
        )
        rows.append([all_button])
    return InlineKeyboardMarkup(rows)


def platforms_from_callback_data(data: str, available_platforms: list[str]) -> list[str]:
    if not data.startswith(CALLBACK_PREFIX):
        raise ValueError(f"Unrecognized callback data: {data!r}")
    key = data[len(CALLBACK_PREFIX) :]
    if key == ALL_PLATFORMS_KEY:
        return list(available_platforms)
    if key not in available_platforms:
        raise ValueError(f"Unknown platform in callback data: {key!r}")
    return [key]