"""
T11: тесты для tab_switch — Ctrl+Click → новая вкладка → close.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import WebDriverException

import tab_switch
from tab_switch import close_current_tab, new_tab_for_listing, open_in_new_tab_via_click

# ─────────────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────────────


class _FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle not in self.driver.window_handles:
            raise WebDriverException(f"unknown handle {handle}")
        self.driver.current_window_handle = handle


class _FakeActionChain:
    """Stub ActionChain: записывает все операции в driver._action_log,
    при perform() дополнительно «открывает новую вкладку» (имитация
    Ctrl+Click → новая handle добавляется к driver.window_handles)."""

    open_new_tab_on_perform = True  # default: Ctrl+Click работает
    raise_on_perform = False

    def __init__(self, driver, *args, **kwargs):
        self.driver = driver
        if not hasattr(driver, "_action_log"):
            driver._action_log = []

    def key_down(self, key):
        self.driver._action_log.append(("key_down", key))
        return self

    def click(self, element=None):
        self.driver._action_log.append(("click", element))
        return self

    def key_up(self, key):
        self.driver._action_log.append(("key_up", key))
        return self

    def perform(self):
        if _FakeActionChain.raise_on_perform:
            raise WebDriverException("perform failure")
        if _FakeActionChain.open_new_tab_on_perform:
            new_handle = f"tab-{len(self.driver.window_handles) + 1}"
            self.driver.window_handles.append(new_handle)


class _FakeDriver:
    def __init__(self):
        self.window_handles = ["main"]
        self.current_window_handle = "main"
        self.switch_to = _FakeSwitchTo(self)
        self._action_log: list = []
        self._closed_handles: list[str] = []

    def close(self):
        # Удаляем текущую handle.
        self._closed_handles.append(self.current_window_handle)
        self.window_handles = [h for h in self.window_handles if h != self.current_window_handle]
        # current_window_handle становится «никаким» до switch_to.window()
        # — что соответствует поведению реального Selenium.


@pytest.fixture
def patch_actionchain(monkeypatch):
    monkeypatch.setattr(tab_switch, "ActionChains", _FakeActionChain)
    _FakeActionChain.open_new_tab_on_perform = True
    _FakeActionChain.raise_on_perform = False
    yield
    _FakeActionChain.open_new_tab_on_perform = True
    _FakeActionChain.raise_on_perform = False


@pytest.fixture
def patch_sleep(monkeypatch):
    """Подменяем time.sleep внутри tab_switch — тесты быстрые."""
    monkeypatch.setattr("tab_switch.time.sleep", lambda _: None)


# ─────────────────────────────────────────────────────────────────────────────
# open_in_new_tab_via_click
# ─────────────────────────────────────────────────────────────────────────────


def test_open_in_new_tab_success(patch_actionchain, patch_sleep):
    """Happy path: Ctrl+Click → новая handle → switch."""
    driver = _FakeDriver()
    element = MagicMock()
    ok = open_in_new_tab_via_click(driver, element)
    assert ok is True
    # Должны были послать key_down(CONTROL), click(element), key_up(CONTROL).
    keys = [op for op in driver._action_log if op[0] in ("key_down", "key_up")]
    assert ("key_down", "\ue009") in keys or any(  # CONTROL key code
        k[0] == "key_down" for k in keys
    )
    # Текущая вкладка — новая.
    assert driver.current_window_handle != "main"
    assert driver.current_window_handle in driver.window_handles


def test_open_in_new_tab_no_new_handle_returns_false(patch_actionchain, patch_sleep):
    """Если Ctrl+Click не открыл новую вкладку — False, без switch."""
    _FakeActionChain.open_new_tab_on_perform = False
    driver = _FakeDriver()
    element = MagicMock()
    ok = open_in_new_tab_via_click(driver, element)
    assert ok is False
    # Driver остаётся на оригинальной вкладке.
    assert driver.current_window_handle == "main"


def test_open_in_new_tab_perform_failure(patch_actionchain, patch_sleep):
    """ActionChains.perform() падает → False, без crash."""
    _FakeActionChain.raise_on_perform = True
    driver = _FakeDriver()
    element = MagicMock()
    ok = open_in_new_tab_via_click(driver, element)
    assert ok is False
    # Никаких новых handles.
    assert driver.window_handles == ["main"]


def test_open_in_new_tab_stop_event_aborts_immediately(patch_actionchain, patch_sleep):
    driver = _FakeDriver()
    element = MagicMock()
    ev = threading.Event()
    ev.set()
    ok = open_in_new_tab_via_click(driver, element, stop_event=ev)
    assert ok is False
    # Click даже не был сделан.
    assert driver._action_log == []


def test_open_in_new_tab_no_handles_returns_false(patch_actionchain, patch_sleep):
    """Если у driver нет handles вообще (поломан) — False."""
    driver = _FakeDriver()
    driver.window_handles = []
    element = MagicMock()
    ok = open_in_new_tab_via_click(driver, element)
    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# close_current_tab
# ─────────────────────────────────────────────────────────────────────────────


def test_close_current_tab_switches_to_remaining(patch_actionchain, patch_sleep):
    driver = _FakeDriver()
    # Симулируем что у нас 2 вкладки: main и tab-2 (текущая).
    driver.window_handles = ["main", "tab-2"]
    driver.current_window_handle = "tab-2"

    ok = close_current_tab(driver)
    assert ok is True
    # tab-2 закрыт, переключились на main.
    assert "tab-2" not in driver.window_handles
    assert driver.current_window_handle == "main"
    assert "tab-2" in driver._closed_handles


def test_close_current_tab_with_only_one_handle_skips(patch_actionchain, patch_sleep):
    """Только одна вкладка → close(=закрытие браузера) — пропускаем."""
    driver = _FakeDriver()
    ok = close_current_tab(driver)
    assert ok is False
    assert driver._closed_handles == []


def test_close_current_tab_handles_failure_gracefully(patch_actionchain, patch_sleep):
    """Если driver.close() кинет — возвращаем False, без crash."""
    driver = _FakeDriver()
    driver.window_handles = ["main", "tab-2"]
    driver.current_window_handle = "tab-2"

    def bad_close():
        raise WebDriverException("close failed")

    driver.close = bad_close  # type: ignore
    ok = close_current_tab(driver)
    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# new_tab_for_listing — context manager
# ─────────────────────────────────────────────────────────────────────────────


def test_new_tab_for_listing_happy_path(patch_actionchain, patch_sleep):
    """Open + yield True + close, после контекста — обратно на main."""
    driver = _FakeDriver()
    element = MagicMock()

    with new_tab_for_listing(driver, element) as ok:
        assert ok is True
        # Внутри контекста — на новой вкладке.
        assert driver.current_window_handle != "main"

    # После выхода — обратно на main, новая вкладка закрыта.
    assert driver.current_window_handle == "main"
    assert len(driver.window_handles) == 1


def test_new_tab_for_listing_open_fails_no_close(patch_actionchain, patch_sleep):
    """Если open вернул False — close НЕ вызывается."""
    _FakeActionChain.open_new_tab_on_perform = False
    driver = _FakeDriver()
    element = MagicMock()

    with new_tab_for_listing(driver, element) as ok:
        assert ok is False

    # Driver не трогали — single tab.
    assert driver._closed_handles == []
    assert driver.current_window_handle == "main"


def test_new_tab_for_listing_exception_inside_still_closes(patch_actionchain, patch_sleep):
    """Если внутри контекста было исключение — вкладка всё равно закрывается."""
    driver = _FakeDriver()
    element = MagicMock()

    with pytest.raises(RuntimeError, match="boom"):
        with new_tab_for_listing(driver, element) as ok:
            assert ok is True
            raise RuntimeError("boom")

    # finally: вкладка закрыта, мы на main.
    assert driver.current_window_handle == "main"
    assert len(driver.window_handles) == 1
