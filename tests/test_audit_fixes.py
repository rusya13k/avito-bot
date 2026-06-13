"""Тесты для правок аудита (июнь 2026):

- detect_profile_protection: детект «защиты профиля» по тексту (вкл. shadow-DOM)
  и по кнопке «получить код по СМС».
- _wait_network_ready: ожидание готовности прокси в свежем профиле (ERR_SOCKS).
- yandex-warmup отключаемость через флаг.
"""

from unittest.mock import MagicMock

import pytest

import captcha_detect

# ──────────────────────────────────────────────────────────────────────────────
# detect_profile_protection
# ──────────────────────────────────────────────────────────────────────────────


def _driver_with_text(text: str, buttons=None):
    """Мок driver: execute_script отдаёт text, find_elements — buttons (XPath)."""
    drv = MagicMock()
    drv.execute_script.return_value = text
    drv.find_elements.return_value = buttons or []
    return drv


def test_profile_protection_detected_by_text():
    drv = _driver_with_text("Сработала защита профиля, получите код")
    assert captcha_detect.detect_profile_protection(drv) is True


def test_profile_protection_detected_by_substring_lowercase():
    # «защита профиля» как подстрока в «Сработала защита профиля» (реальный текст Avito)
    drv = _driver_with_text("Сработала защита профиля")
    assert captcha_detect.detect_profile_protection(drv) is True


def test_profile_protection_detected_by_button_when_text_empty():
    # Текст пустой (модалка в shadow-DOM не попала в innerText) — но кнопка видна.
    btn = MagicMock()
    btn.is_displayed.return_value = True
    drv = _driver_with_text("", buttons=[btn])
    assert captcha_detect.detect_profile_protection(drv) is True


def test_profile_protection_button_not_visible_is_negative():
    btn = MagicMock()
    btn.is_displayed.return_value = False
    drv = _driver_with_text("обычная страница", buttons=[btn])
    assert captcha_detect.detect_profile_protection(drv) is False


def test_profile_protection_negative_on_normal_page():
    drv = _driver_with_text("Главная страница Авито, объявления")
    assert captcha_detect.detect_profile_protection(drv) is False


def test_deep_page_text_falls_back_on_error():
    from selenium.common.exceptions import WebDriverException

    drv = MagicMock()
    # Первый execute_script (deep JS) падает, второй (innerText) — тоже.
    drv.execute_script.side_effect = WebDriverException("boom")
    assert captcha_detect._deep_page_text(drv) == ""


# ──────────────────────────────────────────────────────────────────────────────
# _wait_network_ready (bot.py)
# ──────────────────────────────────────────────────────────────────────────────


def test_wait_network_ready_ok_on_first_try():
    import bot

    drv = MagicMock()
    drv.get.return_value = None  # навигация прошла
    assert bot._wait_network_ready(drv, "acc") is True
    assert drv.get.call_count == 1


def test_wait_network_ready_retries_then_succeeds(monkeypatch):
    from selenium.common.exceptions import WebDriverException

    import bot

    monkeypatch.setattr(bot, "hp", lambda *a, **k: None)  # не спим в тесте
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise WebDriverException("unknown error: net::ERR_SOCKS_CONNECTION_FAILED")
        return None

    drv = MagicMock()
    drv.get.side_effect = fake_get
    assert bot._wait_network_ready(drv, "acc", attempts=6) is True
    assert calls["n"] == 3


def test_wait_network_ready_gives_up_after_attempts(monkeypatch):
    from selenium.common.exceptions import WebDriverException

    import bot

    monkeypatch.setattr(bot, "hp", lambda *a, **k: None)
    drv = MagicMock()
    drv.get.side_effect = WebDriverException("net::ERR_SOCKS_CONNECTION_FAILED")
    assert bot._wait_network_ready(drv, "acc", attempts=4) is False
    assert drv.get.call_count == 4


def test_wait_network_ready_reraises_non_proxy_error(monkeypatch):
    from selenium.common.exceptions import WebDriverException

    import bot

    monkeypatch.setattr(bot, "hp", lambda *a, **k: None)
    drv = MagicMock()
    drv.get.side_effect = WebDriverException("session deleted: browser crashed")
    with pytest.raises(WebDriverException):
        bot._wait_network_ready(drv, "acc")


# ──────────────────────────────────────────────────────────────────────────────
# yandex-warmup отключаемость
# ──────────────────────────────────────────────────────────────────────────────


def test_big_warmup_skips_yandex_when_disabled(monkeypatch):
    import warmup

    monkeypatch.setattr(warmup, "_pick_warmup_sites", lambda n: [])
    stats = warmup.big_warmup(MagicMock(), "acc", num_sites=0, with_yandex_search=False)
    assert stats["yandex_ok"] is None  # None == yandex не запускался
