"""
A1: тесты per-account proxy (_apply_account_proxy).

Проверяем:
- account.get("proxy") используется как первый источник прокси.
- При неудаче per-account прокси — fallback на proxies.txt.
- Если ни один прокси не доступен — логируем ERROR и возвращаем None.
- Успешный per-account proxy возвращается без обращения к proxies.txt.

L11: тесты для AdsPowerAPI.update_proxy (метод-инкапсуляция URL-сборки).
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from bot import AdsPowerAPI, _apply_account_proxy


@pytest.fixture
def adspower():
    return MagicMock(name="adspower")


def _make_account(proxy=None):
    return {"name": "acc1", "user_id": "u1", "proxy": proxy}


# ── Per-account proxy ──────────────────────────────────────────────────────


def test_per_account_proxy_used_first(adspower):
    """Если account["proxy"] задан и update_profile_proxy успешен — используется он."""
    acc = _make_account(proxy="user:pass@host:1080")
    with (
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
        patch("bot.get_random_proxy") as mock_rnd,
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result == "user:pass@host:1080"
    mock_upd.assert_called_once_with(adspower, "u1", "user:pass@host:1080")
    mock_rnd.assert_not_called()


def test_per_account_proxy_fallback_on_failure(adspower):
    """Per-account proxy не ставится → fallback на proxies.txt."""
    acc = _make_account(proxy="bad:proxy")
    with (
        patch("bot.update_profile_proxy", side_effect=[False, True]) as mock_upd,
        patch("bot.get_random_proxy", return_value="fallback:1234"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result == "fallback:1234"
    assert mock_upd.call_count == 2


def test_no_proxy_at_all_returns_none_and_logs_error(adspower, caplog):
    """Нет per-account прокси и proxies.txt пуст — возвращаем None, ERROR в лог."""
    acc = _make_account(proxy=None)
    with (
        patch("bot.update_profile_proxy") as mock_upd,
        patch("bot.get_random_proxy", return_value=None),
        caplog.at_level(logging.ERROR, logger="bot"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result is None
    mock_upd.assert_not_called()
    assert any("Нет доступного прокси" in r.message for r in caplog.records)


def test_no_per_account_proxy_uses_proxies_txt(adspower):
    """Нет per-account прокси → proxies.txt без обращения к account["proxy"]."""
    acc = _make_account(proxy=None)
    with (
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
        patch("bot.get_random_proxy", return_value="txt:9090"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result == "txt:9090"
    mock_upd.assert_called_once_with(adspower, "u1", "txt:9090")


def test_both_proxies_fail_returns_none(adspower, caplog):
    """Per-account и proxies.txt оба не ставятся → None + ERROR."""
    acc = _make_account(proxy="p:1")
    with (
        patch("bot.update_profile_proxy", return_value=False),
        patch("bot.get_random_proxy", return_value="p:2"),
        caplog.at_level(logging.ERROR, logger="bot"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result is None
    assert any("Нет доступного прокси" in r.message for r in caplog.records)


# ── L11: AdsPowerAPI.update_proxy() ────────────────────────────────────────


def test_update_proxy_success_with_credentials():
    """L11: host:port:user:pass → POST с правильным URL/payload, code=0 → True."""
    api = AdsPowerAPI("http://127.0.0.1:50325/", api_key="secret")
    response = MagicMock()
    response.json.return_value = {"code": 0}

    with patch("bot.requests.post", return_value=response) as mock_post:
        ok = api.update_proxy("u1", "1.2.3.4:1080:alice:pwd")

    assert ok is True
    # base должен быть нормализован (без trailing slash) и URL — собран из _url
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args.args[0] == "http://127.0.0.1:50325/api/v1/user/update"
    payload = call_args.kwargs["json"]
    assert payload["user_id"] == "u1"
    cfg = payload["user_proxy_config"]
    assert cfg["proxy_host"] == "1.2.3.4"
    assert cfg["proxy_port"] == "1080"
    assert cfg["proxy_user"] == "alice"
    assert cfg["proxy_password"] == "pwd"
    # При наличии api_key — Authorization header.
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


def test_update_proxy_returns_false_on_non_zero_code():
    """L11: code != 0 в JSON-ответе → False."""
    api = AdsPowerAPI("http://127.0.0.1:50325")
    response = MagicMock()
    response.json.return_value = {"code": 42, "msg": "boom"}

    with patch("bot.requests.post", return_value=response):
        assert api.update_proxy("u1", "host:1080") is False


def test_update_proxy_returns_false_on_request_exception():
    """L11: сетевая ошибка → False, не пробрасываем наружу."""
    api = AdsPowerAPI("http://127.0.0.1:50325")
    with patch("bot.requests.post", side_effect=requests.ConnectionError("boom")):
        assert api.update_proxy("u1", "host:1080") is False


def test_update_proxy_invalid_format_skips_http():
    """L11: один токен (без порта) → False, без HTTP-вызова."""
    api = AdsPowerAPI("http://127.0.0.1:50325")
    with patch("bot.requests.post") as mock_post:
        assert api.update_proxy("u1", "no_port_here") is False
    mock_post.assert_not_called()


def test_update_profile_proxy_wrapper_delegates_to_method():
    """L11: top-level update_profile_proxy — тонкая обёртка над методом
    (back-compat для tests/test_proxy.py, которые мокают этот символ)."""
    from bot import update_profile_proxy

    api = MagicMock(spec=AdsPowerAPI)
    api.update_proxy.return_value = True

    assert update_profile_proxy(api, "u1", "host:1080") is True
    api.update_proxy.assert_called_once_with("u1", "host:1080")
