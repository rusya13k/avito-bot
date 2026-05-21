"""
T7 + T8: тесты для stealth-инъекций (без реального Chrome).

Проверяем:
- apply_stealth вызывает execute_cdp_cmd с правильными аргументами;
- скрипт содержит ключевые токены (navigator.webdriver, cdc_);
- на драйверах БЕЗ CDP — graceful False, без raise;
- WebDriverException → False, не падаем;
- verify_stealth собирает диагностику и не падает на пустых ответах.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from selenium.common.exceptions import WebDriverException

from stealth import _STEALTH_JS, apply_stealth, verify_stealth

# ─────────────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────────────


class _FakeChromeDriver:
    """Driver-stub: умеет execute_cdp_cmd + execute_script."""

    def __init__(self, *, cdp_raises: Exception | None = None):
        self.cdp_calls: list[tuple[str, dict]] = []
        self.script_calls: list[str] = []
        self.cdp_raises = cdp_raises
        # default возвращаемые значения для execute_script
        self._script_return = {
            "navigator.webdriver": None,
            "Object.getOwnPropertyNames": [],
            "navigator.userAgent": "Mozilla/5.0 stub",
        }

    def execute_cdp_cmd(self, cmd: str, args: dict):
        self.cdp_calls.append((cmd, args))
        if self.cdp_raises is not None:
            raise self.cdp_raises
        return {}

    def execute_script(self, script: str, *args):
        self.script_calls.append(script)
        for token, value in self._script_return.items():
            if token in script:
                return value
        return None


class _NoCdpDriver:
    """Driver-stub без execute_cdp_cmd (например, Firefox)."""

    def execute_script(self, script: str, *args):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# apply_stealth
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_stealth_calls_cdp_with_expected_command():
    driver = _FakeChromeDriver()
    ok = apply_stealth(driver)
    assert ok is True
    assert len(driver.cdp_calls) == 1
    cmd, args = driver.cdp_calls[0]
    assert cmd == "Page.addScriptToEvaluateOnNewDocument"
    assert "source" in args


def test_apply_stealth_script_contains_webdriver_override():
    """Скрипт должен переопределять navigator.webdriver (T7)."""
    assert "navigator" in _STEALTH_JS
    assert "webdriver" in _STEALTH_JS
    assert "undefined" in _STEALTH_JS
    assert "defineProperty" in _STEALTH_JS


def test_apply_stealth_script_contains_cdc_cleanup():
    """Скрипт должен удалять cdc_* глобалы (T8)."""
    assert "cdc_" in _STEALTH_JS
    assert "delete" in _STEALTH_JS
    assert "startsWith" in _STEALTH_JS


def test_apply_stealth_no_cdp_returns_false():
    """Драйвер без execute_cdp_cmd — graceful False."""
    driver = _NoCdpDriver()
    ok = apply_stealth(driver)
    assert ok is False


def test_apply_stealth_webdriver_exception_returns_false():
    driver = _FakeChromeDriver(cdp_raises=WebDriverException("CDP not supported"))
    ok = apply_stealth(driver)
    assert ok is False
    # Вызов был сделан, но упал.
    assert len(driver.cdp_calls) == 1


def test_apply_stealth_unexpected_exception_returns_false():
    """Любое неожиданное исключение тоже даёт False (а не пробрасывает)."""
    driver = _FakeChromeDriver(cdp_raises=RuntimeError("boom"))
    ok = apply_stealth(driver)
    assert ok is False


def test_apply_stealth_idempotent():
    """Повторный вызов не должен ломаться (просто два раза зарегистрирует)."""
    driver = _FakeChromeDriver()
    assert apply_stealth(driver) is True
    assert apply_stealth(driver) is True
    assert len(driver.cdp_calls) == 2


# ─────────────────────────────────────────────────────────────────────────────
# verify_stealth
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_stealth_clean_browser():
    """Если стелс сработал: webdriver=None, cdc_keys=[]."""
    driver = _FakeChromeDriver()
    diag = verify_stealth(driver)
    assert diag["webdriver"] is None
    assert diag["cdc_keys"] == []
    assert diag["user_agent"] == "Mozilla/5.0 stub"


def test_verify_stealth_dirty_browser():
    """Если стелс НЕ сработал: webdriver=True, cdc_keys содержит ключи."""
    driver = _FakeChromeDriver()
    driver._script_return = {
        "navigator.webdriver": True,
        "Object.getOwnPropertyNames": ["cdc_adoQpoasnfa76pfcZLmcfl_Array"],
        "navigator.userAgent": "Mozilla/5.0 dirty",
    }
    diag = verify_stealth(driver)
    assert diag["webdriver"] is True
    assert diag["cdc_keys"] == ["cdc_adoQpoasnfa76pfcZLmcfl_Array"]


def test_verify_stealth_handles_script_errors():
    """Если execute_script падает — verify не падает наружу, возвращает defaults."""
    driver = MagicMock()
    driver.execute_script.side_effect = WebDriverException("frame detached")
    diag = verify_stealth(driver)
    # webdriver остаётся None, cdc_keys пустой, user_agent None.
    assert diag["webdriver"] is None
    assert diag["cdc_keys"] == []
    assert diag["user_agent"] is None
