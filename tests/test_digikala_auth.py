import base64
import json
import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faraz_honar.core.exceptions import ConfigurationError, PlatformAuthError
from faraz_honar.platforms.digikala.auth import TokenManager


def _fake_jwt(exp: float | None) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header = b64({"typ": "JWT", "alg": "HS256"})
    payload_obj = {"token_id": 1} if exp is None else {"token_id": 1, "exp": exp}
    payload = b64(payload_obj)
    return f"{header}.{payload}.fake-signature"


def _manager(
    tmp_path: Path, *, enable_refresh: bool = False, bootstrap=True, access_token: str | None = None
) -> TokenManager:
    return TokenManager(
        base_url="https://seller.digikala.com",
        tokens_file=tmp_path / "tokens.json",
        bootstrap_access_token=(access_token or "boot-access") if bootstrap else None,
        bootstrap_refresh_token="boot-refresh" if bootstrap else None,
        enable_refresh=enable_refresh,
    )


def test_uses_bootstrap_tokens_when_no_file_exists(tmp_path: Path):
    manager = _manager(tmp_path)
    tokens = manager.current_tokens()
    assert tokens.access_token == "boot-access"


def test_raises_configuration_error_with_nothing_available(tmp_path: Path):
    manager = _manager(tmp_path, bootstrap=False)
    with pytest.raises(ConfigurationError):
        manager.current_tokens()


def test_prefers_persisted_file_over_bootstrap(tmp_path: Path):
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(
        json.dumps({"access_token": "file-access", "refresh_token": "file-refresh"})
    )
    manager = _manager(tmp_path)
    tokens = manager.current_tokens()
    assert tokens.access_token == "file-access"


@patch("faraz_honar.platforms.digikala.auth.requests.post")
def test_refresh_when_disabled_never_calls_network(mock_post: MagicMock, tmp_path: Path):
    # Refreshing is order-sync-platform's job when this manager is disabled
    # (see TokenManager.refresh()'s docstring) — this manager must never
    # call the real refresh endpoint itself.
    manager = _manager(tmp_path, enable_refresh=False)
    manager.current_tokens()  # populate the cache, as a real 401 flow would
    with pytest.raises(ConfigurationError):
        manager.refresh()
    mock_post.assert_not_called()


def test_refresh_when_disabled_picks_up_newer_shared_file(tmp_path: Path):
    # order-sync-platform refreshed and wrote a newer pair to the shared
    # tokens_file since we last read it — refresh() should pick that up
    # with no network call, instead of raising.
    tokens_file = tmp_path / "tokens.json"
    manager = _manager(tmp_path, enable_refresh=False)
    manager.current_tokens()  # caches the bootstrap pair
    tokens_file.write_text(
        json.dumps({"access_token": "fresher-from-order-sync", "refresh_token": "boot-refresh"})
    )
    tokens = manager.refresh()
    assert tokens.access_token == "fresher-from-order-sync"


def test_refresh_when_disabled_raises_if_shared_file_still_stale(tmp_path: Path):
    # No newer pair is available anywhere (order-sync-platform hasn't
    # refreshed yet either) — refresh() must not pretend to succeed.
    manager = _manager(tmp_path, enable_refresh=False)
    manager.current_tokens()  # caches the bootstrap pair, nothing else available
    with pytest.raises(ConfigurationError, match="order-sync-platform"):
        manager.refresh()


@patch("faraz_honar.platforms.digikala.auth.requests.post")
def test_refresh_when_enabled_persists_new_tokens(mock_post: MagicMock, tmp_path: Path):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"data": {"access_token": "new-access", "refresh_token": "new-refresh"}},
    )
    manager = _manager(tmp_path, enable_refresh=True)
    tokens = manager.refresh()

    assert tokens.access_token == "new-access"
    mock_post.assert_called_once()
    called_url = mock_post.call_args.args[0]
    assert called_url == "https://seller.digikala.com/open-api/v1/auth/refresh-token"

    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved["access_token"] == "new-access"


@patch("faraz_honar.platforms.digikala.auth.requests.post")
def test_refresh_failure_raises_platform_auth_error(mock_post: MagicMock, tmp_path: Path):
    mock_post.return_value = MagicMock(status_code=401, text="invalid refresh token")
    manager = _manager(tmp_path, enable_refresh=True)
    with pytest.raises(PlatformAuthError):
        manager.refresh()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permission bits (0600) don't exist on Windows: os.chmod() there only "
    "toggles the read-only flag, so the mode always reads back as 0o666.",
)
def test_saved_tokens_file_has_restrictive_permissions(tmp_path: Path):
    with patch("faraz_honar.platforms.digikala.auth.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": {"access_token": "new-access", "refresh_token": "new-refresh"}},
        )
        manager = _manager(tmp_path, enable_refresh=True)
        manager.refresh()

    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600


# -- is_expiring() (proactive, local-only JWT exp check) ----------------------------


def test_is_expiring_true_for_already_expired_token(tmp_path: Path):
    expired = _fake_jwt(exp=time.time() - 3600)
    manager = _manager(tmp_path, access_token=expired)
    assert manager.is_expiring() is True


def test_is_expiring_false_for_token_with_plenty_of_time_left(tmp_path: Path):
    fresh = _fake_jwt(exp=time.time() + 3600)
    manager = _manager(tmp_path, access_token=fresh)
    assert manager.is_expiring() is False


def test_is_expiring_true_within_buffer_window(tmp_path: Path):
    almost_expired = _fake_jwt(exp=time.time() + 10)
    manager = _manager(tmp_path, access_token=almost_expired)
    assert manager.is_expiring(buffer_seconds=60) is True


def test_is_expiring_false_when_no_token_configured(tmp_path: Path):
    manager = _manager(tmp_path, bootstrap=False)
    assert manager.is_expiring() is False  # never raises; caller falls back to reactive 401 handling


def test_is_expiring_false_for_non_jwt_token(tmp_path: Path):
    manager = _manager(tmp_path, access_token="not-a-jwt-at-all")
    assert manager.is_expiring() is False


def test_is_expiring_false_for_jwt_without_exp_claim(tmp_path: Path):
    manager = _manager(tmp_path, access_token=_fake_jwt(exp=None))
    assert manager.is_expiring() is False


# -- Tokens must never leak into logs -------------------------------------------------


@patch("faraz_honar.platforms.digikala.auth.requests.post")
def test_refresh_never_logs_token_values(
    mock_post: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "data": {"access_token": "super-secret-access", "refresh_token": "super-secret-refresh"}
        },
    )
    manager = _manager(tmp_path, enable_refresh=True, access_token="old-secret-access-value")

    with caplog.at_level("DEBUG"):
        manager.refresh()

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "super-secret-access" not in all_log_text
    assert "super-secret-refresh" not in all_log_text
    assert "old-secret-access-value" not in all_log_text
