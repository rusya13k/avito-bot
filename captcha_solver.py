"""
Бесплатный солвер Yandex SmartCaptcha (checkbox).

Yandex SmartCaptcha checkbox — поведенческая проверка. При чистом fingerprint
(AdsPower) и human-like mouse movement решается одним кликом по чекбоксу.

Алгоритм:
1. Найти iframe SmartCaptcha или кнопку на странице /showcaptcha
2. Переключиться в iframe (если есть)
3. Найти чекбокс "Я не робот" / кнопку подтверждения
4. Human-like клик с предварительным движением мыши
5. Подождать подтверждение (редирект / исчезновение капчи)
6. Вернуться в основной контент

Yandex /showcaptcha — отдельная страница с кнопкой "Я не робот".
Не iframe, а полноценная страница. Клик по кнопке → JS-проверка → редирект.

Если checkbox не решает (challenge / slider) — возвращает False.
"""

import logging
import random
import time

from selenium.common.exceptions import (
    WebDriverException,
)
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)

# Селекторы iframe SmartCaptcha
_SMARTCAPTCHA_IFRAME_SELECTORS = [
    "iframe[src*='smartcaptcha.yandexcloud.net']",
    "iframe[src*='captcha-api.yandex']",
    "iframe[src*='smartcaptcha']",
]

# Селекторы чекбокса внутри iframe
_CHECKBOX_SELECTORS = [
    ".CheckboxCaptcha-Button",
    "input[type='checkbox']",
    ".CheckboxCaptcha-Checkbox",
    "[class*='CheckboxCaptcha'] input",
    "[class*='Checkbox'] button",
    ".smart-captcha__checkbox",
]

# Селекторы на странице /showcaptcha (не iframe — полная страница)
_SHOWCAPTCHA_BUTTON_SELECTORS = [
    ".CheckboxCaptcha-Button",
    "button.CheckboxCaptcha-Button",
    ".SmartCaptcha-Button",
    "form .Button2",
    "input[type='submit']",
    "button[type='submit']",
    ".AdvancedCaptcha-Button",
]

# Маркеры что капча решена (URL изменился / элемент исчез)
_CAPTCHA_URL_MARKERS = ("/showcaptcha", "captcha=")


def _find_smartcaptcha_iframe(driver):
    """Ищет iframe SmartCaptcha на странице. Возвращает element или None."""
    for sel in _SMARTCAPTCHA_IFRAME_SELECTORS:
        try:
            iframes = driver.find_elements(By.CSS_SELECTOR, sel)
            for iframe in iframes:
                if iframe.is_displayed():
                    return iframe
        except WebDriverException:
            continue
    return None


def _find_checkbox_in_frame(driver):
    """Ищет кликабельный чекбокс внутри текущего контекста (iframe или page)."""
    for sel in _CHECKBOX_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    return el
        except WebDriverException:
            continue
    return None


def _find_showcaptcha_button(driver):
    """Ищет кнопку на странице /showcaptcha (полная страница, не iframe)."""
    for sel in _SHOWCAPTCHA_BUTTON_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    return el
        except WebDriverException:
            continue
    return None


def _human_pre_move(driver, element):
    """Имитация движения мыши к элементу перед кликом."""
    try:
        from selenium.webdriver.common.action_chains import ActionChains

        actions = ActionChains(driver)
        # Сдвиг к элементу с небольшим offset (не точно в центр)
        offset_x = random.randint(-3, 3)
        offset_y = random.randint(-3, 3)
        actions.move_to_element_with_offset(element, offset_x, offset_y)
        actions.pause(random.uniform(0.1, 0.4))
        actions.perform()
    except WebDriverException:
        pass  # fallback — кликнем без pre-move


def _wait_captcha_resolved(driver, timeout=10) -> bool:
    """Ждёт что капча решена: URL изменился или элементы капчи исчезли."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            url = (driver.current_url or "").lower()
            # Если URL больше не содержит маркеры капчи — решена
            if not any(m in url for m in _CAPTCHA_URL_MARKERS):
                return True
            # Проверяем исчезновение iframe
            iframe = _find_smartcaptcha_iframe(driver)
            if iframe is None:
                btn = _find_showcaptcha_button(driver)
                if btn is None:
                    return True
        except WebDriverException:
            pass
        time.sleep(0.5)
    return False


def solve_yandex_smartcaptcha(driver, account_name="", log_func=None) -> bool:
    """Пытается решить Yandex SmartCaptcha checkbox бесплатно.

    Стратегия:
    1. Если на /showcaptcha — кликаем кнопку на странице
    2. Если iframe SmartCaptcha — переключаемся и кликаем чекбокс
    3. Ждём подтверждение (редирект / исчезновение)

    Returns:
        True — капча решена, можно продолжать
        False — не удалось (challenge/slider/timeout)
    """

    def _log(msg):
        if log_func:
            log_func(account_name, msg)
        else:
            logger.info("[%s] %s", account_name, msg)

    _log("SmartCaptcha solver: attempting free solve...")

    # Пауза перед действием — имитация "человек увидел капчу, подумал"
    time.sleep(random.uniform(1.5, 3.5))

    try:
        url = (driver.current_url or "").lower()
    except WebDriverException:
        url = ""

    solved = False

    # Стратегия 1: страница /showcaptcha (полная страница с кнопкой)
    if "/showcaptcha" in url:
        _log("  Strategy 1: /showcaptcha page — looking for button...")
        btn = _find_showcaptcha_button(driver)
        if btn:
            _human_pre_move(driver, btn)
            time.sleep(random.uniform(0.3, 0.8))
            try:
                btn.click()
                _log("  Clicked showcaptcha button. Waiting for resolve...")
                time.sleep(random.uniform(1.0, 2.5))
                solved = _wait_captcha_resolved(driver, timeout=15)
            except WebDriverException as e:
                _log(f"  Button click failed: {e}")
        else:
            _log("  No button found on /showcaptcha page.")

    # Стратегия 2: iframe SmartCaptcha (встроенная капча)
    if not solved:
        _log("  Strategy 2: looking for SmartCaptcha iframe...")
        iframe = _find_smartcaptcha_iframe(driver)
        if iframe:
            try:
                driver.switch_to.frame(iframe)
                _log("  Switched to SmartCaptcha iframe.")

                checkbox = _find_checkbox_in_frame(driver)
                if checkbox:
                    _human_pre_move(driver, checkbox)
                    time.sleep(random.uniform(0.3, 0.8))
                    checkbox.click()
                    _log("  Clicked checkbox. Waiting for resolve...")
                    time.sleep(random.uniform(1.5, 3.0))

                    # Проверяем внутри iframe — появился ли challenge
                    try:
                        challenge = driver.find_elements(
                            By.CSS_SELECTOR,
                            "[class*='Task'], [class*='Challenge'], "
                            "[class*='AdvancedCaptcha'], img[class*='Image']",
                        )
                        if challenge:
                            _log("  Challenge detected (image/slider) — cannot solve free.")
                            driver.switch_to.default_content()
                            return False
                    except WebDriverException:
                        pass

                    solved = True
                else:
                    _log("  No checkbox found in iframe.")
            except WebDriverException as e:
                _log(f"  iframe interaction failed: {e}")
            finally:
                try:
                    driver.switch_to.default_content()
                except WebDriverException:
                    pass

    # Стратегия 3: кнопка на текущей странице (без iframe)
    if not solved:
        _log("  Strategy 3: looking for button on current page...")
        btn = _find_showcaptcha_button(driver)
        if btn:
            _human_pre_move(driver, btn)
            time.sleep(random.uniform(0.3, 0.8))
            try:
                btn.click()
                _log("  Clicked page button. Waiting...")
                time.sleep(random.uniform(1.5, 3.0))
                solved = _wait_captcha_resolved(driver, timeout=10)
            except WebDriverException as e:
                _log(f"  Page button click failed: {e}")

    if solved:
        # Финальная проверка — URL действительно изменился
        time.sleep(random.uniform(0.5, 1.5))
        try:
            final_url = (driver.current_url or "").lower()
            if any(m in final_url for m in _CAPTCHA_URL_MARKERS):
                _log("  URL still contains captcha markers — solve may have failed.")
                return False
        except WebDriverException:
            pass
        _log("  SmartCaptcha SOLVED successfully!")
        return True

    _log("  SmartCaptcha solve FAILED — no clickable element found.")
    return False
