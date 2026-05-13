"""
A1: тесты per-account proxy (_apply_account_proxy).

Проверяем:
- account.get("proxy") используется как первый источник прокси.
- При неудаче per-account прокси — fallback на proxies.txt.
- Если ни один прокси не доступен — логируем ERROR и возвращаем None.
- Успешный per-account proxy возвращается без обращения к proxies.txt.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from bot import _apply_account_proxy


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
