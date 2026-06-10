"""
chat_sender — универсальная отправка первого сообщения в Avito-чат.

Вынесено из bot.py (_try_write_to_owner) и outbound_messenger.py
(_type_and_send / _open_chat_overlay) для устранения дублирования
(~250 строк идентичной логики). Оба модуля теперь вызывают эти функции.

Вся физическая работа с Avito-чатом (поиск input, клик, typing, send)
сосредоточена здесь. При изменении селекторов или поведения Avito
править нужно только этот файл.
"""

from __future__ import annotations

import logging

from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import tg_bot as _tg
from human_delay import human_delay as _human_delay
from human_mouse import human_click as _human_click
from human_typing import length_speed_multiplier, persona_speed_multiplier
from human_typing import type_human as _type_human

logger = logging.getLogger(__name__)


def _stopping(account_name: str) -> bool:
    return _tg.is_stop_requested(account_name)


# ── Селекторы (единый источник правды) ────────────────────────────────────

_CHAT_BUTTON_SELECTORS = [
    "//a[@data-marker='messenger-button/link']",
    "//*[@data-marker='messenger-button/link']",
    "//a[@id='bx_contact_button_messenger']",
    "//button[@data-marker='item-contact-bar/message-button']",
    "//a[@data-marker='item-contact-bar/message-button']",
    "//*[@data-marker='item-contact-bar/message-button']",
    "//a[contains(., 'Написать сообщение')]",
    "//button[contains(., 'Написать сообщение')]",
    "//a[contains(., 'Написать')]",
    "//button[contains(., 'Написать')]",
]

_INPUT_SELECTORS = [
    "//textarea[@data-marker='icebreakers/textarea']",
    "//textarea[@data-marker='reply/input']",
    "//textarea[@data-marker='message-input']",
    "//*[@data-marker='message-input']//textarea",
    "//textarea[contains(@placeholder, 'Сообщение')]",
    "//textarea[contains(@placeholder, 'сообщение')]",
    "//div[@contenteditable='true' and (@role='textbox' or contains(@class, 'input'))]",
    "//textarea",
]


# ── Публичное API ─────────────────────────────────────────────────────────


def open_chat_overlay(driver, account_name: str) -> bool:
    """Клик «Написать сообщение» на странице листинга.
    Пробует селекторы по очереди, human_click с Bezier-курсором.

    Returns: True если оверлей открыт, False если кнопка не найдена.
    """
    for xpath in _CHAT_BUTTON_SELECTORS:
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            _human_delay(0.5, 1.5, stop_event=_tg.get_account_stop_event(account_name))
            _human_click(driver, btn, stop_event=_tg.get_account_stop_event(account_name))
            _human_delay(2, 4, stop_event=_tg.get_account_stop_event(account_name))
            return True
        except TimeoutException:
            continue
        except Exception as exc:
            logger.debug("open_chat_overlay candidate %s failed: %s", xpath, exc)

    # Fallback: JS-поиск
    try:
        btn = driver.execute_script(
            """
            var el = document.querySelector('a[data-marker="messenger-button/link"]');
            if (el) return el;
            var btns = document.querySelectorAll('a, button');
            for (var i = 0; i < btns.length; i++) {
                var t = btns[i].textContent.trim();
                if (t.indexOf('Написать') === 0) return btns[i];
            }
            return null;
        """
        )
        if btn is not None:
            _human_delay(0.5, 1.5, stop_event=_tg.get_account_stop_event(account_name))
            _human_click(driver, btn, stop_event=_tg.get_account_stop_event(account_name))
            _human_delay(2, 4, stop_event=_tg.get_account_stop_event(account_name))
            return True
    except Exception:
        pass

    logger.info("open_chat_overlay: кнопка «Написать» не найдена (%s)", account_name)
    return False


def type_and_send(driver, account_name: str, text: str, persona_id: str | None = None) -> bool:
    """Найти chat-input, ввести текст человеческим typing'ом, отправить.

    Args:
        driver: Selenium WebDriver.
        account_name: имя аккаунта (для логов и stop_event).
        text: текст для отправки.
        persona_id: ID персоны (опционально, для speed_multiplier).

    Returns: True если сообщение отправлено, False при ошибке.
    """
    input_el = None
    for xpath in _INPUT_SELECTORS:
        try:
            input_el = WebDriverWait(driver, 4).until(
                EC.visibility_of_element_located((By.XPATH, xpath))
            )
            break
        except TimeoutException:
            continue

    if input_el is None:
        # JS-fallback
        try:
            input_el = driver.execute_script(
                """
                var candidates = [
                    'textarea[data-marker="icebreakers/textarea"]',
                    'textarea[data-marker="reply/input"]',
                    'textarea[data-marker="message-input"]'
                ];
                for (var i = 0; i < candidates.length; i++) {
                    var el = document.querySelector(candidates[i]);
                    if (el && el.offsetParent !== null) return el;
                }
                var ce = document.querySelector(
                    'div[contenteditable="true"][role="textbox"]'
                );
                if (ce && ce.offsetParent !== null) return ce;
                var tas = document.querySelectorAll('textarea');
                for (var i = 0; i < tas.length; i++) {
                    if (tas[i].offsetParent !== null) return tas[i];
                }
                return null;
            """
            )
            if input_el is not None:
                logger.debug(
                    "type_and_send: chat-input найден через JS-fallback (%s)", account_name
                )
        except Exception:
            pass

    if input_el is None:
        logger.info("type_and_send: chat-input не найден (%s)", account_name)
        return False

    _human_click(driver, input_el, stop_event=_tg.get_account_stop_event(account_name))
    _human_delay(0.4, 1.2, stop_event=_tg.get_account_stop_event(account_name))

    # Очистка поля: Avito предзаполняет icebreakers/textarea
    try:
        input_el.send_keys(Keys.CONTROL + "a")
        input_el.send_keys(Keys.DELETE)
        _human_delay(0.2, 0.4, stop_event=_tg.get_account_stop_event(account_name))
    except Exception:
        pass

    # Человеческий typing
    try:
        speed_mul = persona_speed_multiplier(persona_id or "") * length_speed_multiplier(text)
        if not _type_human(
            input_el,
            text,
            speed_range=(0.05, 0.20),
            speed_multiplier=speed_mul,
            enable_typos=True,
            stop_event=_tg.get_account_stop_event(account_name),
        ):
            return False
    except StaleElementReferenceException:
        logger.info("type_and_send: input стал stale во время ввода (%s)", account_name)
        return False
    except Exception as exc:
        logger.info("type_and_send: ошибка ввода текста %s: %s", account_name, exc)
        return False

    _human_delay(1.5, 3.5, stop_event=_tg.get_account_stop_event(account_name))

    # Отправка: Enter > SVG dispatchEvent > move_click
    try:
        input_el.send_keys(Keys.ENTER)
        logger.debug("type_and_send: отправлено Enter (%s)", account_name)
    except Exception:
        logger.debug("type_and_send: Enter не удался (%s), пробуем SVG...", account_name)
        try:
            clicked = driver.execute_script(
                """
                var svg = document.querySelector(
                    '[data-marker="icebreakers/send-message"]'
                ) || document.querySelector('[data-marker="send-message-button"]');
                if (!svg) return false;
                svg.dispatchEvent(new MouseEvent('click', {
                    bubbles: true, cancelable: true, view: window
                }));
                return true;
            """
            )
            if not clicked:
                # Способ 3: move_click
                send_el = driver.find_element(
                    By.CSS_SELECTOR, "[data-marker='icebreakers/send-message']"
                )
                _human_click(driver, send_el, stop_event=_tg.get_account_stop_event(account_name))
        except Exception:
            return False

    _human_delay(1, 3, stop_event=_tg.get_account_stop_event(account_name))

    # Закрываем чат-оверлей Escape (чтобы не ломал driver.back())
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        _human_delay(0.5, 1.5, stop_event=_tg.get_account_stop_event(account_name))
    except Exception:
        pass

    return True
