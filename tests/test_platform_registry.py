"""Proves the two DigiKala accounts ("digikala" = main, "digikala2" = fast) are wired up as two
independent credential sets sharing the exact same DigikalaClient
implementation — not a second pricing/business-logic engine, and not
sharing any token state.
"""
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from faraz_honar.config import Settings
from faraz_honar.platforms.basalam.client import BasalamClient
from faraz_honar.platforms.base import PlatformClient
from faraz_honar.platforms.digikala.client import DigikalaClient
from faraz_honar.platforms.registry import build_platform_registry
from faraz_honar.platforms.snappshop.client import SnappShopClient
from faraz_honar.platforms.tapsishop.client import TapsiShopClient
from faraz_honar.platforms.woocommerce.client import WooCommerceClient


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        data_dir=tmp_path,
        digikala_access_token="main-access",
        digikala_refresh_token="main-refresh",
        digikala2_access_token="fast-access",
        digikala2_refresh_token="fast-refresh",
    )
    base.update(overrides)
    return Settings(**base)


def _digikala(client: PlatformClient) -> DigikalaClient:
    # White-box test helper: narrows the interface type down to the
    # concrete DigikalaClient so private attributes (token manager, dry
    # run, delays) can be inspected directly, without relying on mypy's
    # unrelated "untyped function bodies aren't checked" default.
    assert isinstance(client, DigikalaClient)
    return client


def _snappshop(client: PlatformClient) -> SnappShopClient:
    # Same white-box narrowing pattern as _digikala above, for SnappShop's
    # private credential attributes (_token/_vendor_code/_agent_user).
    assert isinstance(client, SnappShopClient)
    return client


def test_registry_registers_all_seven_accounts(tmp_path: Path):
    registry = build_platform_registry(_settings(tmp_path))

    # Seven platforms registered: the two DigiKala accounts (DigikalaClient),
    # the two SnappShop accounts (SnappShopClient), TapsiShop
    # (TapsiShopClient), Basalam (BasalamClient), and WooCommerce
    # (WooCommerceClient) — five client classes total.
    assert set(registry.keys()) == {
        "digikala",
        "digikala2",
        "snappshop_tehran",
        "snappshop_isfahan",
        "tapsi_shop",
        "basalam",
        "woocommerce",
    }
    assert isinstance(registry["digikala"], DigikalaClient)
    assert isinstance(registry["digikala2"], DigikalaClient)
    assert isinstance(registry["snappshop_tehran"], SnappShopClient)
    assert isinstance(registry["snappshop_isfahan"], SnappShopClient)
    assert isinstance(registry["tapsi_shop"], TapsiShopClient)
    assert isinstance(registry["basalam"], BasalamClient)
    assert isinstance(registry["woocommerce"], WooCommerceClient)
    assert registry["digikala"] is not registry["digikala2"]
    assert registry["snappshop_tehran"] is not registry["snappshop_isfahan"]
    # Same class reused for both accounts of each multi-account platform —
    # no second implementation for either.
    assert type(registry["digikala"]) is type(registry["digikala2"])
    assert type(registry["snappshop_tehran"]) is type(registry["snappshop_isfahan"])


def test_registry_keeps_the_menu_order_digikala_first_then_digikala2(tmp_path: Path):
    registry = build_platform_registry(_settings(tmp_path))
    assert list(registry)[:2] == ["digikala", "digikala2"]


def test_digikala_client_uses_main_account_credentials(tmp_path: Path):
    registry = build_platform_registry(_settings(tmp_path))
    main = _digikala(registry["digikala"])

    tokens = main._token_manager.current_tokens()  # noqa: SLF001
    assert tokens.access_token == "main-access"
    assert tokens.refresh_token == "main-refresh"


def test_digikala2_client_uses_fast_account_credentials(tmp_path: Path):
    registry = build_platform_registry(_settings(tmp_path))
    fast = _digikala(registry["digikala2"])

    tokens = fast._token_manager.current_tokens()  # noqa: SLF001
    assert tokens.access_token == "fast-access"
    assert tokens.refresh_token == "fast-refresh"


def test_env_variable_names_did_not_change_so_existing_env_files_keep_working(
    tmp_path: Path, monkeypatch
):
    # The platform keys / Settings fields were renamed, but the .env variable
    # NAMES were deliberately kept: DIGIKALA_ASLI_* is still the main account
    # (now platform "digikala") and the plain DIGIKALA_* is still the fast
    # account (now "digikala2"). If this ever flips, an existing .env would
    # silently swap the two accounts' tokens.
    monkeypatch.setenv("DIGIKALA_ASLI_ACCESS_TOKEN", "env-main")
    monkeypatch.setenv("DIGIKALA_ACCESS_TOKEN", "env-fast")
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.digikala_access_token == "env-main"
    assert settings.digikala2_access_token == "env-fast"


def test_digikala_and_digikala2_persist_to_separate_token_files(tmp_path: Path):
    settings = _settings(tmp_path)
    registry = build_platform_registry(settings)

    main_file = _digikala(registry["digikala"])._token_manager._tokens_file  # noqa: SLF001
    fast_file = _digikala(registry["digikala2"])._token_manager._tokens_file  # noqa: SLF001
    assert main_file != fast_file
    assert main_file == settings.digikala_tokens_file
    assert fast_file == settings.digikala2_tokens_file


def test_token_files_still_point_at_the_same_files_as_before_the_rename(tmp_path: Path):
    # Local fallback file names are unchanged (tokens already saved on disk
    # are still found), and with ORDER_SYNC_DATA_DIR set they are
    # order-sync-platform's own files (digikala = its primary account,
    # digikala2 = its second one).
    settings = _settings(tmp_path)
    assert settings.digikala_tokens_file == tmp_path / "tokens_asli.json"
    assert settings.digikala2_tokens_file == tmp_path / "tokens.json"

    shared = _settings(tmp_path, order_sync_data_dir=tmp_path / "order-sync")
    assert shared.digikala_tokens_file == tmp_path / "order-sync" / "digikala_tokens.json"
    assert shared.digikala2_tokens_file == tmp_path / "order-sync" / "digikala2_tokens.json"


@patch("faraz_honar.platforms.digikala.auth.requests.post")
def test_refreshing_digikala_does_not_modify_digikala2_credentials(mock_post: MagicMock, tmp_path: Path):
    settings = _settings(tmp_path, digikala_enable_token_refresh=True)
    registry = build_platform_registry(settings)
    main = _digikala(registry["digikala"])
    fast = _digikala(registry["digikala2"])

    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "data": {"access_token": "main-new-access", "refresh_token": "main-new-refresh"}
        },
    )
    main._token_manager.refresh()  # noqa: SLF001

    main_tokens = main._token_manager.current_tokens()  # noqa: SLF001
    assert main_tokens.access_token == "main-new-access"

    # digikala2's tokens file was never touched, and its in-memory tokens are
    # still exactly the original bootstrap values.
    assert not settings.digikala2_tokens_file.exists()
    fast_tokens = fast._token_manager.current_tokens()  # noqa: SLF001
    assert fast_tokens.access_token == "fast-access"
    assert fast_tokens.refresh_token == "fast-refresh"


@patch("faraz_honar.platforms.digikala.auth.requests.post")
def test_refreshing_digikala2_does_not_modify_digikala_credentials(mock_post: MagicMock, tmp_path: Path):
    settings = _settings(tmp_path, digikala2_enable_token_refresh=True)
    registry = build_platform_registry(settings)
    main = _digikala(registry["digikala"])
    fast = _digikala(registry["digikala2"])

    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "data": {"access_token": "fast-new-access", "refresh_token": "fast-new-refresh"}
        },
    )
    fast._token_manager.refresh()  # noqa: SLF001

    fast_tokens = fast._token_manager.current_tokens()  # noqa: SLF001
    assert fast_tokens.access_token == "fast-new-access"

    assert not settings.digikala_tokens_file.exists()
    main_tokens = main._token_manager.current_tokens()  # noqa: SLF001
    assert main_tokens.access_token == "main-access"
    assert main_tokens.refresh_token == "main-refresh"


def test_both_accounts_share_dry_run_and_rate_limit_settings(tmp_path: Path):
    # Only credentials differ between the two DigiKala accounts — everything
    # else (DRY_RUN, throttling) is the same global configuration, reused
    # as-is. SnappShop has its own separate delay settings (see
    # test_snappshop_client.py) so it's excluded from this loop.
    settings = _settings(
        tmp_path, dry_run=False, digikala_read_delay_seconds=3.0, digikala_write_delay_seconds=6.0
    )
    registry = build_platform_registry(settings)

    for name in ("digikala", "digikala2"):
        digikala_client = _digikala(registry[name])
        assert digikala_client._dry_run is False  # noqa: SLF001
        assert digikala_client._read_delay_seconds == 3.0  # noqa: SLF001
        assert digikala_client._write_delay_seconds == 6.0  # noqa: SLF001


# -- SnappShop Tehran vs. Isfahan credential isolation -------------------------------


def test_snappshop_tehran_client_uses_tehran_credentials(tmp_path: Path):
    settings = _settings(
        tmp_path,
        snappshop_token="tehran-token",
        snappshop_vendor_code="TEHRAN1",
        snappshop_agent_user="SNV_tehran-agent",
        snappshop_isfahan_token="isfahan-token",
        snappshop_isfahan_vendor_code="ISFHN1",
        snappshop_isfahan_agent_user="SNV_isfahan-agent",
    )
    registry = build_platform_registry(settings)
    tehran = _snappshop(registry["snappshop_tehran"])

    assert tehran._token == "tehran-token"  # noqa: SLF001
    assert tehran._vendor_code == "TEHRAN1"  # noqa: SLF001
    assert tehran._agent_user == "SNV_tehran-agent"  # noqa: SLF001


def test_snappshop_isfahan_client_uses_isfahan_credentials(tmp_path: Path):
    settings = _settings(
        tmp_path,
        snappshop_token="tehran-token",
        snappshop_vendor_code="TEHRAN1",
        snappshop_agent_user="SNV_tehran-agent",
        snappshop_isfahan_token="isfahan-token",
        snappshop_isfahan_vendor_code="ISFHN1",
        snappshop_isfahan_agent_user="SNV_isfahan-agent",
    )
    registry = build_platform_registry(settings)
    isfahan = _snappshop(registry["snappshop_isfahan"])

    assert isfahan._token == "isfahan-token"  # noqa: SLF001
    assert isfahan._vendor_code == "ISFHN1"  # noqa: SLF001
    assert isfahan._agent_user == "SNV_isfahan-agent"  # noqa: SLF001


def test_snappshop_isfahan_does_not_accidentally_use_tehran_credentials(tmp_path: Path):
    settings = _settings(
        tmp_path,
        snappshop_token="tehran-token",
        snappshop_vendor_code="TEHRAN1",
        snappshop_agent_user="SNV_tehran-agent",
        snappshop_isfahan_token="isfahan-token",
        snappshop_isfahan_vendor_code="ISFHN1",
        snappshop_isfahan_agent_user="SNV_isfahan-agent",
    )
    registry = build_platform_registry(settings)
    tehran = _snappshop(registry["snappshop_tehran"])
    isfahan = _snappshop(registry["snappshop_isfahan"])

    assert isfahan._token != tehran._token  # noqa: SLF001
    assert isfahan._vendor_code != tehran._vendor_code  # noqa: SLF001
    assert isfahan._agent_user != tehran._agent_user  # noqa: SLF001


def test_snappshop_isfahan_error_messages_reference_its_own_env_vars(tmp_path: Path):
    # Regression check: both clients used to hardcode "SnappShop Tehran"
    # and Tehran's env var names in every error message, which would have
    # misled operators debugging an Isfahan-only failure.
    settings = _settings(tmp_path)  # no SnappShop credentials -> not configured
    registry = build_platform_registry(settings)
    isfahan = _snappshop(registry["snappshop_isfahan"])
    tehran = _snappshop(registry["snappshop_tehran"])

    assert isfahan._display_name == "SnappShop Isfahan"  # noqa: SLF001
    assert "SNAPPSHOP_ISFAHAN_TOKEN" in isfahan._config_env_hint  # noqa: SLF001
    assert tehran._display_name == "SnappShop Tehran"  # noqa: SLF001
    assert "SNAPPSHOP_ISFAHAN" not in tehran._config_env_hint  # noqa: SLF001


def test_snappshop_tehran_and_isfahan_share_base_url_and_rate_limits(tmp_path: Path):
    # Same official API, same throttle configuration — only credentials
    # differ, exactly like the digikala/digikala2 pattern above.
    settings = _settings(
        tmp_path,
        dry_run=False,
        snappshop_read_delay_seconds=4.0,
        snappshop_write_delay_seconds=8.0,
    )
    registry = build_platform_registry(settings)

    for name in ("snappshop_tehran", "snappshop_isfahan"):
        client = _snappshop(registry[name])
        assert client._base_url == settings.snappshop_base_url.rstrip("/")  # noqa: SLF001
        assert client._dry_run is False  # noqa: SLF001
        assert client._read_delay_seconds == 4.0  # noqa: SLF001
        assert client._write_delay_seconds == 8.0  # noqa: SLF001


# -- TapsiShop wiring -----------------------------------------------------------------


def test_tapsi_shop_client_uses_its_own_credentials_and_settings(tmp_path: Path):
    settings = _settings(
        tmp_path,
        dry_run=False,
        tapsi_token="tapsi-secret",
        tapsi_base_url="https://vendorgw.tapsi.shop",
        tapsi_read_delay_seconds=3.0,
        tapsi_write_delay_seconds=6.0,
    )
    registry = build_platform_registry(settings)
    tapsi = registry["tapsi_shop"]

    assert isinstance(tapsi, TapsiShopClient)
    assert tapsi._token == "tapsi-secret"  # noqa: SLF001
    assert tapsi._base_url == "https://vendorgw.tapsi.shop"  # noqa: SLF001
    assert tapsi._dry_run is False  # noqa: SLF001
    assert tapsi._read_delay_seconds == 3.0  # noqa: SLF001
    assert tapsi._write_delay_seconds == 6.0  # noqa: SLF001


# -- WooCommerce wiring ---------------------------------------------------------------


def test_woocommerce_client_uses_its_own_credentials_and_settings(tmp_path: Path):
    settings = _settings(
        tmp_path,
        dry_run=False,
        woocommerce_consumer_key="wc-key",
        woocommerce_consumer_secret="wc-secret",
        woocommerce_base_url="https://farazhonar.com/wp-json/wc/v3",
        woocommerce_read_delay_seconds=3.0,
        woocommerce_write_delay_seconds=6.0,
    )
    registry = build_platform_registry(settings)
    woocommerce = registry["woocommerce"]

    assert isinstance(woocommerce, WooCommerceClient)
    assert woocommerce._consumer_key == "wc-key"  # noqa: SLF001
    assert woocommerce._consumer_secret == "wc-secret"  # noqa: SLF001
    assert woocommerce._base_url == "https://farazhonar.com/wp-json/wc/v3"  # noqa: SLF001
    assert woocommerce._dry_run is False  # noqa: SLF001
    assert woocommerce._read_delay_seconds == 3.0  # noqa: SLF001
    assert woocommerce._write_delay_seconds == 6.0  # noqa: SLF001


def test_woocommerce_registration_does_not_affect_other_platforms(tmp_path: Path):
    # Registering a seventh platform must never change any existing
    # platform's construction — same credentials/settings as before.
    settings = _settings(
        tmp_path,
        tapsi_token="tapsi-secret",
        basalam_access_token="basalam-secret",
        woocommerce_consumer_key="wc-key",
        woocommerce_consumer_secret="wc-secret",
    )
    registry = build_platform_registry(settings)

    assert isinstance(registry["tapsi_shop"], TapsiShopClient)
    assert registry["tapsi_shop"]._token == "tapsi-secret"  # noqa: SLF001
    assert isinstance(registry["basalam"], BasalamClient)
    assert registry["basalam"]._token == "basalam-secret"  # noqa: SLF001
    assert isinstance(registry["digikala"], DigikalaClient)
