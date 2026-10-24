"""Maps a platform name (as used in the sheet's platform_ref_columns, the
Telegram platform-selection buttons, and the state DB) to a constructed
PlatformClient. Add a new platform by writing a client that implements
platforms.base.PlatformClient and registering it here — nothing else in the
update service, Telegram bot, or state layer needs to change (spec 14).
"""
from __future__ import annotations

from faraz_honar.config import Settings
from faraz_honar.platforms.basalam.client import BasalamClient
from faraz_honar.platforms.base import PlatformClient
from faraz_honar.platforms.digikala.auth import TokenManager
from faraz_honar.platforms.digikala.client import DigikalaClient
from faraz_honar.platforms.snappshop.client import SnappShopClient
from faraz_honar.platforms.tapsishop.client import TapsiShopClient
from faraz_honar.platforms.woocommerce.client import WooCommerceClient


def build_platform_registry(settings: Settings) -> dict[str, PlatformClient]:
    # "digikala" is the MAIN DigiKala seller account (sheet column
    # "دیجی کالا اصلی"). Its own TokenManager points at its own tokens
    # file/bootstrap credentials so refreshing one account's token can never
    # touch the other's.
    digikala_token_manager = TokenManager(
        base_url=settings.digikala_base_url,
        tokens_file=settings.digikala_tokens_file,
        bootstrap_access_token=settings.digikala_access_token,
        bootstrap_refresh_token=settings.digikala_refresh_token,
        enable_refresh=settings.digikala_enable_token_refresh,
        user_agent=settings.digikala_user_agent,
    )
    digikala = DigikalaClient(
        name="digikala",
        base_url=settings.digikala_base_url,
        token_manager=digikala_token_manager,
        dry_run=settings.dry_run,
        read_delay_seconds=settings.digikala_read_delay_seconds,
        write_delay_seconds=settings.digikala_write_delay_seconds,
        user_agent=settings.digikala_user_agent,
    )

    # "digikala2" is the second, independent DigiKala seller account (the
    # fast one, sheet column "دیجی کالا سریع") — same DigikalaClient class,
    # same business logic, its own TokenManager/tokens file/credentials.
    digikala2_token_manager = TokenManager(
        base_url=settings.digikala_base_url,
        tokens_file=settings.digikala2_tokens_file,
        bootstrap_access_token=settings.digikala2_access_token,
        bootstrap_refresh_token=settings.digikala2_refresh_token,
        enable_refresh=settings.digikala2_enable_token_refresh,
        user_agent=settings.digikala2_user_agent,
    )
    digikala2 = DigikalaClient(
        name="digikala2",
        base_url=settings.digikala_base_url,
        token_manager=digikala2_token_manager,
        dry_run=settings.dry_run,
        read_delay_seconds=settings.digikala_read_delay_seconds,
        write_delay_seconds=settings.digikala_write_delay_seconds,
        user_agent=settings.digikala2_user_agent,
    )

    # SnappShop Tehran: a different platform entirely (own base URL, own
    # permanent Bearer token, own vendor code, own agent-user identifier —
    # no TokenManager since there's no refresh flow), reusing the same
    # PlatformClient interface.
    snappshop_tehran = SnappShopClient(
        name="snappshop_tehran",
        base_url=settings.snappshop_base_url,
        token=settings.snappshop_token,
        vendor_code=settings.snappshop_vendor_code,
        agent_user=settings.snappshop_agent_user,
        dry_run=settings.dry_run,
        read_delay_seconds=settings.snappshop_read_delay_seconds,
        write_delay_seconds=settings.snappshop_write_delay_seconds,
        proxy_url=settings.snappshop_proxy_url,
    )

    # SnappShop Isfahan: a second, independent SnappShop seller account
    # (اسنپ شاپ اصفهان) — same SnappShopClient class, same business logic
    # and base_url as Tehran above, its own token/vendor code/agent user so
    # the two accounts' credentials can never be confused with each other.
    snappshop_isfahan = SnappShopClient(
        name="snappshop_isfahan",
        display_name="SnappShop Isfahan",
        config_env_hint=(
            "SNAPPSHOP_ISFAHAN_TOKEN, SNAPPSHOP_ISFAHAN_VENDOR_CODE, "
            "and SNAPPSHOP_ISFAHAN_AGENT_USER"
        ),
        base_url=settings.snappshop_base_url,
        token=settings.snappshop_isfahan_token,
        vendor_code=settings.snappshop_isfahan_vendor_code,
        agent_user=settings.snappshop_isfahan_agent_user,
        dry_run=settings.dry_run,
        read_delay_seconds=settings.snappshop_read_delay_seconds,
        write_delay_seconds=settings.snappshop_write_delay_seconds,
        proxy_url=settings.snappshop_proxy_url,
    )

    # TapsiShop: a different platform entirely (own base URL, single static
    # token — no TokenManager since there's no refresh flow, no vendor
    # code/agent-user split like SnappShop), reusing the same
    # PlatformClient interface.
    tapsi_shop = TapsiShopClient(
        name="tapsi_shop",
        base_url=settings.tapsi_base_url,
        token=settings.tapsi_token,
        dry_run=settings.dry_run,
        read_delay_seconds=settings.tapsi_read_delay_seconds,
        write_delay_seconds=settings.tapsi_write_delay_seconds,
    )

    # Basalam: the store "فراز هنر" (own base URL, single static access
    # token — no TokenManager since there's no refresh flow, no vendor
    # code/agent-user split like SnappShop; vendor_id is hardcoded inside
    # BasalamClient itself, not passed in here), reusing the same
    # PlatformClient interface.
    basalam = BasalamClient(
        base_url=settings.basalam_base_url,
        token=settings.basalam_access_token,
        dry_run=settings.dry_run,
        read_delay_seconds=settings.basalam_read_delay_seconds,
        write_delay_seconds=settings.basalam_write_delay_seconds,
    )

    # WooCommerce: the Faraz Honar store at farazhonar.com (own base URL,
    # single static consumer key/secret pair — no TokenManager since
    # there's no refresh flow), reusing the same PlatformClient interface.
    woocommerce = WooCommerceClient(
        base_url=settings.woocommerce_base_url,
        consumer_key=settings.woocommerce_consumer_key,
        consumer_secret=settings.woocommerce_consumer_secret,
        dry_run=settings.dry_run,
        read_delay_seconds=settings.woocommerce_read_delay_seconds,
        write_delay_seconds=settings.woocommerce_write_delay_seconds,
    )

    return {
        digikala.name: digikala,
        digikala2.name: digikala2,
        snappshop_tehran.name: snappshop_tehran,
        snappshop_isfahan.name: snappshop_isfahan,
        tapsi_shop.name: tapsi_shop,
        basalam.name: basalam,
        woocommerce.name: woocommerce,
    }
