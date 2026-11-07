from pathlib import Path

from faraz_honar.config import Settings


def test_blank_csv_path_env_var_resolves_to_none(monkeypatch):
    monkeypatch.setenv("PRODUCT_SOURCE_CSV_PATH", "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    settings = Settings(_env_file=None)
    assert settings.product_source_csv_path is None
    assert settings.google_service_account_file is None


def test_unset_env_vars_also_resolve_to_none(monkeypatch):
    monkeypatch.delenv("PRODUCT_SOURCE_CSV_PATH", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    settings = Settings(_env_file=None)
    assert settings.product_source_csv_path is None
    assert settings.google_service_account_file is None


def test_non_blank_csv_path_is_preserved(monkeypatch):
    monkeypatch.setenv("PRODUCT_SOURCE_CSV_PATH", "tests/fixtures/sample_product_sheet.csv")
    settings = Settings(_env_file=None)
    assert settings.product_source_csv_path == Path("tests/fixtures/sample_product_sheet.csv")


def test_digikala_delay_settings_default_to_1_and_2_seconds(monkeypatch):
    monkeypatch.delenv("DIGIKALA_READ_DELAY_SECONDS", raising=False)
    monkeypatch.delenv("DIGIKALA_WRITE_DELAY_SECONDS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.digikala_read_delay_seconds == 1.0
    assert settings.digikala_write_delay_seconds == 2.0


def test_digikala_delay_settings_are_configurable_via_env(monkeypatch):
    monkeypatch.setenv("DIGIKALA_READ_DELAY_SECONDS", "3.5")
    monkeypatch.setenv("DIGIKALA_WRITE_DELAY_SECONDS", "7")
    settings = Settings(_env_file=None)
    assert settings.digikala_read_delay_seconds == 3.5
    assert settings.digikala_write_delay_seconds == 7.0


def test_bale_chat_ids_set_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("BALE_CHAT_IDS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.telegram_chat_ids_set == set()


def test_bale_chat_ids_set_parses_comma_separated_ids(monkeypatch):
    monkeypatch.setenv("BALE_CHAT_IDS", "123456789,987654321,555555555")
    settings = Settings(_env_file=None)
    assert settings.telegram_chat_ids_set == {123456789, 987654321, 555555555}


def test_bale_chat_ids_set_tolerates_whitespace_and_blank_entries(monkeypatch):
    monkeypatch.setenv("BALE_CHAT_IDS", " 111 , , 222 ,333 ")
    settings = Settings(_env_file=None)
    assert settings.telegram_chat_ids_set == {111, 222, 333}


def test_telegram_chat_ids_set_is_independent_of_allowed_user_ids(monkeypatch):
    # TELEGRAM_CHAT_IDS (who receives reports) and TELEGRAM_ALLOWED_USER_IDS
    # (who may send commands) are separate settings.
    monkeypatch.setenv("BALE_ALLOWED_USER_IDS", "1,2,3")
    monkeypatch.setenv("BALE_CHAT_IDS", "999")
    settings = Settings(_env_file=None)
    assert settings.telegram_allowed_user_ids_set == {1, 2, 3}
    assert settings.telegram_chat_ids_set == {999}


# -- Numbered chat slots (TELEGRAM_CHAT_ID_N / TELEGRAM_CHAT_NAME_N) ------------


def _clear_numbered_chat_env(monkeypatch):
    for n in range(1, 11):
        monkeypatch.delenv(f"TELEGRAM_CHAT_ID_{n}", raising=False)
        monkeypatch.delenv(f"TELEGRAM_CHAT_NAME_{n}", raising=False)


def test_numbered_chat_ids_set_defaults_to_empty(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.numbered_chat_ids_set == set()


def test_numbered_chat_ids_set_collects_filled_slots_and_ignores_blank_ones(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_1", "111")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_3", "   ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_10", " 1010 ")
    settings = Settings(_env_file=None)
    assert settings.numbered_chat_ids_set == {111, 1010}


def test_numbered_chat_ids_set_supports_all_ten_slots(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    for n in range(1, 11):
        monkeypatch.setenv(f"TELEGRAM_CHAT_ID_{n}", str(1000 + n))
    settings = Settings(_env_file=None)
    assert settings.numbered_chat_ids_set == {1000 + n for n in range(1, 11)}


def test_numbered_chat_ids_set_ignores_legacy_comma_separated_settings(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2,3")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "999")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_1", "42")
    settings = Settings(_env_file=None)
    assert settings.numbered_chat_ids_set == {42}
    assert settings.telegram_allowed_user_ids_set == {1, 2, 3}
    assert settings.telegram_chat_ids_set == {999}


def test_display_name_returns_paired_name(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_1", "111")
    monkeypatch.setenv("TELEGRAM_CHAT_NAME_1", "آقای اعتصامی")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "222")
    monkeypatch.setenv("TELEGRAM_CHAT_NAME_2", "خانم رضایی")
    settings = Settings(_env_file=None)
    assert settings.display_name(111) == "آقای اعتصامی"
    assert settings.display_name(222) == "خانم رضایی"


def test_display_name_pairs_by_slot_number_not_by_position(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    # Slot 1 is empty; slot 2 must still get NAME_2 (not NAME_1).
    monkeypatch.setenv("TELEGRAM_CHAT_NAME_1", "نام یتیم")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "222")
    monkeypatch.setenv("TELEGRAM_CHAT_NAME_2", "خانم رضایی")
    settings = Settings(_env_file=None)
    assert settings.display_name(222) == "خانم رضایی"


def test_display_name_falls_back_to_chat_id_when_name_missing_or_blank(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_1", "111")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "222")
    monkeypatch.setenv("TELEGRAM_CHAT_NAME_2", "   ")
    settings = Settings(_env_file=None)
    assert settings.display_name(111) == "111"
    assert settings.display_name(222) == "222"


def test_display_name_falls_back_to_chat_id_for_unknown_chat(monkeypatch):
    _clear_numbered_chat_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.display_name(555) == "555"


def test_error_rounds_settings_default_to_enabled_and_60_seconds(monkeypatch):
    monkeypatch.delenv("ERROR_ROUNDS_ENABLED", raising=False)
    monkeypatch.delenv("ERROR_ROUNDS_COOLDOWN_SECONDS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.error_rounds_enabled is True
    assert settings.error_rounds_cooldown_seconds == 60.0


def test_error_rounds_settings_are_configurable_via_env(monkeypatch):
    monkeypatch.setenv("ERROR_ROUNDS_ENABLED", "false")
    monkeypatch.setenv("ERROR_ROUNDS_COOLDOWN_SECONDS", "0")
    settings = Settings(_env_file=None)
    assert settings.error_rounds_enabled is False
    assert settings.error_rounds_cooldown_seconds == 0.0
