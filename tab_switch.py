"""
T11: Tab switching через Ctrl+Click.

Сейчас навигация в `find_and_view_commercial_listings` работает так:

    safe_get(driver, listing_href)   # открыть листинг в той же вкладке
    extract_listing_data(...)
    driver.back()                    # вернуться к search-page

Это паттерн «навигация по back/forward», который для real-user'а
встречается, но не на 100% случаев. Реальные пользователи часто
открывают интересные листинги в **новой вкладке** через Ctrl+Click,
просматривают их, закрывают вкладку. На fingerprinting'е это видно
по distribution `window.open` events — у бота их 0%.

Этот модуль реализует Ctrl+Click → новая вкладка → switch → ... →
close → switch back. Вызовы прозрачны для caller'а:

    with new_tab_for_listing(driver, link_element) as in_tab:
        if not in_tab:
            # Ctrl+Click не сработал — caller должен использовать
            # обычную навигацию через driver.get / driver.back.
            ...
        # Тут DOM нового листинга, можно работать.
        ...
    # При выходе из контекста: close current tab + switch back.

API:

    open_in_new_tab_via_click(driver, element, *, stop_event=None) -> bool
        Ctrl+Click по element + switch на новую вкладку.

    close_current_tab(driver) -> bool
        Закрыть текущую (активную) вкладку и переключиться обратно
        на ту, что была первой/предыдущей.

    new_tab_for_listing(driver, element, *, stop_event=None)
        Контекст-менеджер: open_in_new_tab_via_click → yield ok →
        close_current_tab.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

logger = logging.getLogger(__name__)


def _window_handles(driver: Any) -> list[str]:
    """Безопасно получить список window handles."""
    try:
        return list(driver.window_handles)
    except WebDriverException:
        return []


def open_in_new_tab_via_click(
    driver: Any,
    element: Any,
    *,
    stop_event: threading.Event | None = None,
) -> bool:
    """T11: Ctrl+Click по элементу → открыть в новой вкладке + switch.

    Реализация:
        1. Запоминаем текущий список handles.
        2. Делаем `key_down(CONTROL).click(element).key_up(CONTROL)` —
           pure-keyboard chain, по виду как реальный Ctrl+Click.
        3. Ждём появления новой handle (max ~3s).
        4. `driver.switch_to.window(new_handle)`.
        5. На любом сбое возвращаем False — caller использует
           обычную навигацию.

    Args:
        driver: Selenium WebDriver.
        element: WebElement (ссылка). Должен иметь href, иначе Ctrl+Click
            работает как обычный click — в новой вкладке ничего не
            откроется и handles не изменится.
        stop_event: если задан и сработал — прерываемся.

    Returns:
        True если новая вкладка открылась и driver на ней.
        False если что-то пошло не так — caller остаётся на той же
        вкладке, можно использовать обычную навигацию.
    """
    if stop_event is not None and stop_event.is_set():
        return False

    before = _window_handles(driver)
    if not before:
        return False

    try:
        actions = ActionChains(driver)
        # Используем CONTROL key (на macOS — COMMAND, но оригинальный
        # код вообще под Windows AdsPower'ом, поэтому CONTROL ок).
        actions.key_down(Keys.CONTROL)
        actions.click(element)
        actions.key_up(Keys.CONTROL)
        actions.perform()
    except WebDriverException as exc:
        logger.debug("open_in_new_tab_via_click: click failed — %s", exc)
        return False

    # Ждём новую handle (короткий retry-loop; полагаемся на implicit-wait
    # JavaScript event-loop — обычно handle появляется за 100-500ms).
    deadline = time.time() + 3.0
    new_handle: str | None = None
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        after = _window_handles(driver)
        diff = [h for h in after if h not in before]
        if diff:
            new_handle = diff[-1]
            break
        time.sleep(0.1)

    if new_handle is None:
        # Ctrl+Click не открыл новую вкладку (возможно, ссылка с target=_self
        # или JS перехватил клик). Fallback на caller.
        logger.debug("open_in_new_tab_via_click: новая вкладка не появилась")
        return False

    try:
        driver.switch_to.window(new_handle)
        return True
    except WebDriverException as exc:
        logger.debug("open_in_new_tab_via_click: switch_to.window failed — %s", exc)
        return False


def close_current_tab(driver: Any) -> bool:
    """Закрыть текущую вкладку и переключиться на ПЕРВУЮ из оставшихся.

    Returns:
        True если успешно закрыли и переключились.
        False если что-то пошло не так. На False caller должен
        проверить driver.window_handles вручную.
    """
    try:
        before = _window_handles(driver)
        if len(before) <= 1:
            # Только одна вкладка — закрытие = закрытие браузера, не делаем.
            logger.debug("close_current_tab: всего 1 handle, skip")
            return False
        current = driver.current_window_handle
        driver.close()
        # Переключаемся на первую из оставшихся (обычно это main tab).
        remaining = [h for h in before if h != current]
        if not remaining:
            return False
        driver.switch_to.window(remaining[0])
        return True
    except WebDriverException as exc:
        logger.debug("close_current_tab: failed — %s", exc)
        return False


@contextmanager
def new_tab_for_listing(
    driver: Any,
    element: Any,
    *,
    stop_event: threading.Event | None = None,
):
    """T11: контекст-менеджер «работа в новой вкладке».

    Использование:
        with new_tab_for_listing(driver, link) as ok:
            if not ok:
                # fallback на safe_get + driver.back
                ...
            else:
                # обработать листинг в новой вкладке
                ...
        # Здесь driver уже снова на оригинальной вкладке.

    Если открытие не удалось (False) — close НЕ вызывается, caller
    остаётся на оригинальной вкладке.
    """
    opened = open_in_new_tab_via_click(driver, element, stop_event=stop_event)
    try:
        yield opened
    finally:
        if opened:
            close_current_tab(driver)
