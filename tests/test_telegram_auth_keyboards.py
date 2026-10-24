from types import SimpleNamespace

import pytest

from faraz_honar.config import Settings
from faraz_honar.telegram_bot import handlers
from faraz_honar.telegram_bot.auth import build_allowed_ids, is_allowed
from faraz_honar.telegram_bot.keyboards import (
    BTN_ALL_PLATFORMS,
    BTN_BACK,
    BTN_CHECK_PRICES,
    BTN_LAST_STATUS,
    BTN_SELECT_PLATFORM,
    BTN_STATUS,
    BTN_STOP,
    BTN_CLEAR_DATABASE,
    build_main_menu_keyboard,
    build_platform_keyboard,
    build_platform_submenu_keyboard,
    platform_button_label,
    platform_from_button_label,
    platforms_from_callback_data,
)


def test_empty_allowlist_denies_everyone():
    assert is_allowed(12345, set()) is False


def test_allowed_user_passes():
    assert is_allowed(12345, {12345, 67890}) is True


def test_unknown_user_denied():
    assert is_allowed(999, {12345}) is False


def test_none_user_id_denied():
    assert is_allowed(None, {12345}) is False


def test_build_allowed_ids_is_the_union_of_both_sets():
    assert build_allowed_ids({1, 2}, {2, 3}) == {1, 2, 3}


def test_build_allowed_ids_with_both_empty_is_empty():
    assert build_allowed_ids(set(), set()) == set()


def _authorized(monkeypatch, user_id, allowed_user_ids="", **numbered):
    # Isolate from any real TELEGRAM_* env vars on the machine running the tests.
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    for n in range(1, 11):
        monkeypatch.delenv(f"TELEGRAM_CHAT_ID_{n}", raising=False)
    settings = Settings(_env_file=None, telegram_allowed_user_ids=allowed_user_ids, **numbered)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id))
    return handlers._is_authorized(update, settings)


def test_is_authorized_user_only_in_allowed_user_ids(monkeypatch):
    assert _authorized(monkeypatch, 10, allowed_user_ids="10,11") is True


def test_is_authorized_user_only_in_numbered_chat_ids(monkeypatch):
    assert _authorized(monkeypatch, 20, allowed_user_ids="10", telegram_chat_id_1="20") is True


def test_is_authorized_user_only_in_numbered_chat_ids_with_no_legacy_allowlist(monkeypatch):
    assert _authorized(monkeypatch, 20, telegram_chat_id_3="20") is True


def test_is_authorized_user_in_neither_set(monkeypatch):
    assert _authorized(monkeypatch, 30, allowed_user_ids="10", telegram_chat_id_1="20") is False


def test_is_authorized_fails_closed_when_both_sets_are_empty(monkeypatch):
    assert _authorized(monkeypatch, 10) is False


def test_is_authorized_denies_when_there_is_no_effective_user(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    settings = Settings(_env_file=None, telegram_allowed_user_ids="10")
    update = SimpleNamespace(effective_user=None)
    assert handlers._is_authorized(update, settings) is False


def test_keyboard_has_one_button_per_platform_plus_all():
    markup = build_platform_keyboard(["digikala"])
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 2
    labels = {b.text for b in buttons}
    assert labels == {"🔴 Digikala", "All Platforms"}


def test_callback_data_resolves_single_platform():
    assert platforms_from_callback_data("update:digikala", ["digikala"]) == ["digikala"]


def test_callback_data_resolves_all_platforms():
    result = platforms_from_callback_data("update:all", ["digikala", "basalam"])
    assert result == ["digikala", "basalam"]


def test_callback_data_rejects_unknown_platform():
    with pytest.raises(ValueError):
        platforms_from_callback_data("update:mystery", ["digikala"])


def test_callback_data_rejects_bad_prefix():
    with pytest.raises(ValueError):
        platforms_from_callback_data("noop", ["digikala"])


# -- Persistent reply-keyboard menu (control-panel UI) ---------------------------


def test_main_menu_keyboard_has_expected_buttons():
    markup = build_main_menu_keyboard()
    labels = {b.text for row in markup.keyboard for b in row}
    assert labels == {
        BTN_SELECT_PLATFORM,
        BTN_LAST_STATUS,
        BTN_CHECK_PRICES,
        BTN_STATUS,
        BTN_STOP,
        BTN_CLEAR_DATABASE,
    }


def test_main_menu_keyboard_puts_clear_database_next_to_stop():
    """Kept in the same row as BTN_STOP so the main menu stays compact (the
    destructive action is still guarded by a yes/no confirmation)."""
    markup = build_main_menu_keyboard()
    stop_row = next(row for row in markup.keyboard if any(b.text == BTN_STOP for b in row))
    assert [b.text for b in stop_row] == [BTN_STOP, BTN_CLEAR_DATABASE]


def test_main_menu_keyboard_is_persistent_and_resized():
    markup = build_main_menu_keyboard()
    assert markup.is_persistent is True
    assert markup.resize_keyboard is True


def test_platform_submenu_keyboard_has_digikala_and_back():
    markup = build_platform_submenu_keyboard(["digikala"])
    labels = {b.text for row in markup.keyboard for b in row}
    assert labels == {"🛒 🔴 Digikala", BTN_BACK}


def test_platform_button_label_round_trips_to_platform_name():
    label = platform_button_label("digikala")
    assert label == "🛒 🔴 Digikala"
    assert platform_from_button_label(label) == "digikala"


def test_platform_from_button_label_unknown_text_returns_none():
    assert platform_from_button_label("not a button") is None


# -- SnappShop Tehran platform button (added alongside the DigiKala ones) -------


def test_snappshop_tehran_button_round_trips_to_platform_name():
    label = platform_button_label("snappshop_tehran")
    assert label == "🛒 🟣 SnappShop Tehran"
    assert platform_from_button_label(label) == "snappshop_tehran"


def test_platform_submenu_keyboard_includes_snappshop_tehran_without_removing_digikala():
    markup = build_platform_submenu_keyboard(["digikala", "digikala2", "snappshop_tehran"])
    labels = {b.text for row in markup.keyboard for b in row}
    assert labels == {"🛒 🔴 Digikala", "🛒 🔴 Digikala Saree", "🛒 🟣 SnappShop Tehran", BTN_ALL_PLATFORMS, BTN_BACK}


# -- SnappShop Isfahan platform button (second SnappShop account) ---------------


def test_snappshop_isfahan_button_round_trips_to_platform_name():
    label = platform_button_label("snappshop_isfahan")
    assert label == "🛒 🟣 SnappShop Isfahan"
    assert platform_from_button_label(label) == "snappshop_isfahan"


def test_snappshop_tehran_and_isfahan_have_distinct_buttons():
    assert platform_button_label("snappshop_tehran") != platform_button_label("snappshop_isfahan")


def test_platform_submenu_keyboard_includes_all_four_platforms():
    markup = build_platform_submenu_keyboard(
        ["digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan"]
    )
    labels = {b.text for row in markup.keyboard for b in row}
    assert labels == {
        "🛒 🔴 Digikala",
        "🛒 🔴 Digikala Saree",
        "🛒 🟣 SnappShop Tehran",
        "🛒 🟣 SnappShop Isfahan",
        BTN_ALL_PLATFORMS,
        BTN_BACK,
    }


# -- TapsiShop platform button ----------------------------------------------------


def test_tapsi_shop_button_round_trips_to_platform_name():
    label = platform_button_label("tapsi_shop")
    assert label == "🛒 🟠 TapsiShop"
    assert platform_from_button_label(label) == "tapsi_shop"


def test_platform_submenu_keyboard_includes_all_five_platforms():
    markup = build_platform_submenu_keyboard(
        ["digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan", "tapsi_shop"]
    )
    labels = {b.text for row in markup.keyboard for b in row}
    assert labels == {
        "🛒 🔴 Digikala",
        "🛒 🔴 Digikala Saree",
        "🛒 🟣 SnappShop Tehran",
        "🛒 🟣 SnappShop Isfahan",
        "🛒 🟠 TapsiShop",
        BTN_ALL_PLATFORMS,
        BTN_BACK,
    }


# -- WooCommerce platform button ---------------------------------------------------


def test_woocommerce_button_round_trips_to_platform_name():
    label = platform_button_label("woocommerce")
    assert label == "🛒 🟢 Site Faraz Honar"
    assert platform_from_button_label(label) == "woocommerce"


def test_platform_submenu_keyboard_includes_all_six_platforms_including_woocommerce():
    markup = build_platform_submenu_keyboard(
        [
            "digikala",
            "digikala2",
            "snappshop_tehran",
            "snappshop_isfahan",
            "tapsi_shop",
            "woocommerce",
        ]
    )
    labels = {b.text for row in markup.keyboard for b in row}
    assert labels == {
        "🛒 🔴 Digikala",
        "🛒 🔴 Digikala Saree",
        "🛒 🟣 SnappShop Tehran",
        "🛒 🟣 SnappShop Isfahan",
        "🛒 🟠 TapsiShop",
        "🛒 🟢 Site Faraz Honar",
        BTN_ALL_PLATFORMS,
        BTN_BACK,
    }


# -- "همه‌ی پلتفرم‌ها" button ---------------------------------------------------------


def test_all_platforms_button_is_the_last_platform_button_just_above_back():
    markup = build_platform_submenu_keyboard(["digikala", "digikala2", "snappshop_tehran"])
    rows = [[b.text for b in row] for row in markup.keyboard]
    assert rows[-2] == [BTN_ALL_PLATFORMS]
    assert rows[-1] == [BTN_BACK]
    # every real platform button comes before it
    # platform buttons are packed two per row to keep the keyboard short
    assert rows[:-2] == [
        ["🛒 🔴 Digikala", "🛒 🔴 Digikala Saree"],
        ["🛒 🟣 SnappShop Tehran"],
    ]


def test_platform_submenu_packs_platform_buttons_two_per_row():
    platforms = ["digikala", "digikala2", "snappshop_tehran", "snappshop_isfahan", "tapsi_shop"]
    markup = build_platform_submenu_keyboard(platforms)
    rows = [[b.text for b in row] for row in markup.keyboard]
    platform_rows = rows[:-2]
    assert [len(r) for r in platform_rows] == [2, 2, 1]
    assert sum(platform_rows, []) == [platform_button_label(p) for p in platforms]
    # the run-everything and back buttons stay alone on their rows (no mis-taps)
    assert rows[-2:] == [[BTN_ALL_PLATFORMS], [BTN_BACK]]


def test_main_menu_keyboard_is_compact_with_at_most_two_buttons_per_row():
    rows = build_main_menu_keyboard().keyboard
    assert len(rows) <= 3
    assert all(len(row) <= 2 for row in rows)


def test_all_platforms_button_is_not_offered_for_a_single_platform():
    markup = build_platform_submenu_keyboard(["digikala"])
    labels = {b.text for row in markup.keyboard for b in row}
    assert BTN_ALL_PLATFORMS not in labels


def test_all_platforms_button_is_not_mistaken_for_a_platform():
    assert platform_from_button_label(BTN_ALL_PLATFORMS) is None