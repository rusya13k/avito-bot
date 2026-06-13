"""
Детектор капчи на странице объявления Avito.

Цель: вовремя поймать момент, когда Avito показывает Yandex.SmartCaptcha
(чаще всего — после клика на «Показать телефон»). Если этого не делать,
бот продолжит работать, выбьет несколько повторных попыток и попадёт в бан.

API:
    detect_captcha(driver) -> bool
        Универсальная проверка. Возвращает True, если на странице (или во
        фреймах верхнего уровня) видны признаки капчи.

    detect_phone_captcha(driver) -> bool
        Алиас для detect_captcha — отдельное имя для семантики «клик по
        Показать телефон».

Селекторы подобраны на основе:
    - Yandex SmartCaptcha (iframe https://smartcaptcha.yandexcloud.net, div.SmartCaptcha)
    - Avito-специфичные оверлеи / модалки
    - title страницы и URL-редиректы (firewall / captcha.html / blocked)
    - текстовые признаки на странице (введите символы / подтвердите …)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from selenium.common.exceptions import (
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)


# CSS-селекторы видимых элементов капчи. Достаточно одного совпадения.
_CAPTCHA_CSS_SELECTORS: tuple[str, ...] = (
    # Yandex SmartCaptcha
    "iframe[src*='smartcaptcha.yandexcloud.net']",
    "iframe[src*='captcha-api.yandex']",
    "div.SmartCaptcha",
    "div.CheckboxCaptcha",
    "div[class*='SmartCaptcha']",
    "div[class*='CheckboxCaptcha']",
    "div[data-testid='smart-captcha']",
    # Generic
    "iframe[src*='captcha']",
    "form[action*='captcha']",
    "input[name='captcha']",
    # Avito-конкретно (если появятся свои контейнеры)
    "[data-marker*='captcha']",
)

# Текстовые маркеры — ищем в page_source. Совпадение по подстроке.
# Подобраны так, чтобы не ловить ложные срабатывания на словах
# «captcha» в служебных скриптах.
_CAPTCHA_TEXT_PATTERNS: tuple[str, ...] = (
    "Подтвердите, что вы не робот",
    "Подтвердите что вы не робот",
    "введите символы с картинки",
    "введите код с картинки",
    "Введите символы с картинки",
    "Не похож на робота",
    "Я не робот",
    "smartcaptcha",  # обычно встречается в src iframe — но достаточно надёжно
)

# Признаки в заголовке страницы / URL.
_CAPTCHA_TITLE_MARKERS: tuple[str, ...] = ("Ой!", "Captcha", "Капча")
_CAPTCHA_URL_MARKERS: tuple[str, ...] = (
    "/firewall/",
    "/captcha",
    "captcha.html",
    "/blocked",
)


def _any_visible(driver, selectors: Iterable[str]) -> str | None:
    """
    Возвращает селектор, по которому найден элемент капчи (если найден),
    иначе None. Считает iframe видимым, даже если visibility checks ломаются.
    """
    for sel in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
        except WebDriverException as exc:
            logger.debug("captcha selector %s raised: %s", sel, exc)
            continue
        for el in elements:
            try:
                # Для iframe is_displayed() обычно True, если он в DOM.
                # Не настаиваем на is_displayed — некоторые контейнеры
                # появляются off-screen перед анимацией.
                if el.is_displayed() or el.tag_name == "iframe":
                    return sel
            except StaleElementReferenceException:
                continue
            except WebDriverException:
                # Если is_displayed внезапно бросает — считаем элемент за факт
                # присутствия капчи.
                return sel
    return None


def _matches_url(driver) -> str | None:
    try:
        url = driver.current_url or ""
    except WebDriverException:
        return None
    low = url.lower()
    for marker in _CAPTCHA_URL_MARKERS:
        if marker in low:
            return f"url:{marker}"
    return None


def _matches_title(driver) -> str | None:
    try:
        title = driver.title or ""
    except WebDriverException:
        return None
    for marker in _CAPTCHA_TITLE_MARKERS:
        if marker in title:
            return f"title:{marker}"
    return None


def _matches_text(driver) -> str | None:
    """
    Проверка по page_source. Делаем именно после CSS-проверок:
    page_source — дорогая операция (десериализация всего DOM).
    """
    try:
        src = driver.page_source or ""
    except WebDriverException:
        return None
    for pattern in _CAPTCHA_TEXT_PATTERNS:
        if pattern in src:
            return f"text:{pattern}"
    return None


def detect_captcha(driver, log_func=None, account_name: str = "") -> bool:
    """
    Универсальный детектор капчи.

    Args:
        driver: selenium WebDriver
        log_func: опциональный (account_name, message) -> None
        account_name: для логирования

    Returns:
        True, если на странице видны признаки капчи.
    """
    matched = (
        _any_visible(driver, _CAPTCHA_CSS_SELECTORS)
        or _matches_url(driver)
        or _matches_title(driver)
        or _matches_text(driver)
    )

    if matched:
        msg = f"!!! CAPTCHA DETECTED (matched: {matched}) !!!"
        if log_func is not None:
            try:
                log_func(account_name, msg)
            except Exception:  # лог не должен ронять детектор
                logger.exception("log_func raised in detect_captcha")
        else:
            logger.warning("[%s] %s", account_name or "?", msg)
        return True

    return False


# Алиас для семантики «клик по Показать телефон»
def detect_phone_captcha(driver, log_func=None, account_name: str = "") -> bool:
    return detect_captcha(driver, log_func=log_func, account_name=account_name)


# ──────────────────────────────────────────────────────────────────────────────
# B1: SMS-форма на стадии login
# ──────────────────────────────────────────────────────────────────────────────

# Селекторы input'а для ввода SMS-кода. Avito может использовать разные:
# - input[name='code'] / [name='confirmation_code'] / [name='sms']
# - autocomplete='one-time-code' (стандарт OWASP / browser autofill)
# - data-marker, содержащий 'code' / 'sms'
_SMS_INPUT_CSS_SELECTORS: tuple[str, ...] = (
    "input[name='code']",
    "input[name='sms']",
    "input[name='confirmation_code']",
    "input[name='one_time_password']",
    "input[autocomplete='one-time-code']",
    "input[data-marker*='code']",
    "input[data-marker*='sms']",
    "[data-marker='login-form/code']",
    "[data-marker='login-form/sms-code']",
)

# Текстовые признаки, что Avito показал SMS-форму.
_SMS_TEXT_PATTERNS: tuple[str, ...] = (
    "Введите код из СМС",
    "Введите код из SMS",
    "Введите код из смс",
    "Введите код подтверждения",
    "На ваш номер отправлен код",
    "Мы отправили код",
    "Подтверждение по СМС",
    "Подтверждение по телефону",
    "Код подтверждения",
)


def detect_sms_form(driver, log_func=None, account_name: str = "") -> bool:
    """
    True, если на странице видна форма ввода SMS-кода.
    Используется на стадии login (B1).
    """
    matched = _any_visible(driver, _SMS_INPUT_CSS_SELECTORS) or _matches_sms_text(driver)
    if matched:
        msg = f"!!! SMS FORM DETECTED (matched: {matched}) !!!"
        if log_func is not None:
            try:
                log_func(account_name, msg)
            except Exception:
                logger.exception("log_func raised in detect_sms_form")
        else:
            logger.warning("[%s] %s", account_name or "?", msg)
        return True
    return False


def _matches_sms_text(driver) -> str | None:
    try:
        src = driver.execute_script("return document.body.innerText;") or ""
    except WebDriverException:
        return None
    for pattern in _SMS_TEXT_PATTERNS:
        if pattern in src:
            return f"text:{pattern}"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# B1.5: Экран «сработала защита профиля» — Avito блокирует логин,
#       предлагает получить код по СМС. Нужно детектить и кликать кнопку.
# ──────────────────────────────────────────────────────────────────────────────

_PROTECTION_TEXT_PATTERNS: tuple[str, ...] = (
    "сработала защита профиля",
    "защита профиля",
    "Получить код по СМС",
    "получить код по смс",
    "получить код в СМС",
    "получить код в смс",
)

_GET_SMS_BUTTON_XPATHS: tuple[str, ...] = (
    "//button[contains(text(), 'Получить код')]",
    "//button[contains(text(), 'получить код')]",
    "//button[contains(., 'код по СМС')]",
    "//button[contains(., 'код по смс')]",
    "//button[contains(., 'код в СМС')]",
    "//button[contains(., 'код в смс')]",
    "//button[@data-marker='login-form/request-sms']",
    "//button[@data-marker='login-form/get-sms-code']",
    "//button[@data-marker='login-form/get-code']",
    "//button[@data-marker='login-form/request-code']",
)


# JS: собирает текст страницы ВКЛЮЧАЯ shadow-DOM. Avito рендерит часть модалок
# (в т.ч. «защита профиля») в web-components с shadowRoot, поэтому обычный
# document.body.innerText их НЕ видит — из-за этого detect промахивался.
_DEEP_TEXT_JS = """
return (function () {
  var txt = document.body ? document.body.innerText : '';
  try {
    var all = document.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      if (all[i].shadowRoot) { txt += ' ' + (all[i].shadowRoot.textContent || ''); }
    }
  } catch (e) {}
  return txt;
})();
"""


def _deep_page_text(driver) -> str:
    """Текст страницы с учётом shadow-DOM. Пустая строка при ошибке."""
    try:
        return driver.execute_script(_DEEP_TEXT_JS) or ""
    except WebDriverException:
        try:
            return driver.execute_script("return document.body.innerText;") or ""
        except WebDriverException:
            return ""


def _has_get_sms_button(driver) -> bool:
    """True, если на странице есть видимая кнопка «получить код по СМС»."""
    for sel in _GET_SMS_BUTTON_XPATHS:
        try:
            for el in driver.find_elements(By.XPATH, sel):
                try:
                    if el.is_displayed():
                        return True
                except (StaleElementReferenceException, WebDriverException):
                    continue
        except WebDriverException:
            continue
    return False


def detect_profile_protection(driver, log_func=None, account_name: str = "") -> bool:
    """
    True, если Avito показывает экран «сработала защита профиля»
    с предложением получить код по СМС.

    Детект двухуровневый (Avito рендерит модалку в shadow-DOM, поэтому одного
    innerText мало): (1) текст страницы с обходом shadow-DOM по паттернам,
    (2) наличие видимой кнопки «получить код по СМС».
    """
    src = _deep_page_text(driver)
    matched: str | None = None
    for pattern in _PROTECTION_TEXT_PATTERNS:
        if pattern in src:
            matched = f"text:{pattern}"
            break
    if matched is None and _has_get_sms_button(driver):
        matched = "button:get-sms"

    if matched is not None:
        if log_func is not None:
            try:
                log_func(
                    account_name,
                    f"!!! PROFILE PROTECTION DETECTED ({matched}) !!!",
                )
            except Exception:
                pass
        return True
    return False


def click_get_sms_button(driver, log_func=None, account_name: str = "") -> bool:
    """
    Кликает кнопку «получить код по смс» на экране защиты профиля.
    Возвращает True, если кнопка найдена и кликнута.
    """
    for sel in _GET_SMS_BUTTON_XPATHS:
        try:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                try:
                    if el.is_displayed() and el.is_enabled():
                        el.click()
                        if log_func is not None:
                            try:
                                log_func(
                                    account_name,
                                    "  Clicked 'получить код по смс' button",
                                )
                            except Exception:
                                pass
                        return True
                except (StaleElementReferenceException, WebDriverException):
                    continue
        except WebDriverException:
            continue
    return False
