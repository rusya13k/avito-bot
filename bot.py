"""
Avito Commercial Real Estate Parser Bot
"""

import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

import tg_bot as _tg
from account_state import account_state

# G1: AvitoMessenger больше не импортируется напрямую — она инкапсулирована
# в AvitoClient.process_messages.
from commercial_parser import (
    extract_listing_data,
    normalize_listing_url,
    save_listing_to_db,
)
from commercial_realestate_config import (
    AVITO_COMMERCIAL_CATEGORIES,
    COMMERCIAL_SEARCH_FILTERS,
    MILLION_CITIES,
)
from cycle_pause import pick_cycle_pause
from database import DatabaseManager
from human_delay import human_delay as _human_delay
from human_mouse import human_click as _human_click
from human_scroll import compute_reading_dwell as _compute_reading_dwell
from human_scroll import human_scroll as _human_scroll
from human_typing import type_human as _type_human
from llm_classifier import LLMClassifier
from logging_setup import (
    get_account_logger,
    install_tg_alert_handler,
    install_tg_buffer_handler,
    setup_logging,
)
from stealth import apply_stealth as _apply_stealth
from tab_switch import new_tab_for_listing as _new_tab_for_listing

# Random queries for Yandex warmup
YANDEX_QUERIES = [
    "погода в москве на неделю",
    "рецепт борща классический",
    "как похудеть в домашних условиях",
    "купить ноутбук недорого",
    "новости сегодня россия",
    "курс доллара к рублю сегодня",
    "ремонт квартиры своими руками",
    "отдых в крыму 2024",
    "что посмотреть из фильмов",
    "авиабилеты дешево",
    "как приготовить пиццу дома",
    "лучшие сериалы 2024",
    "купить машину б у",
    "упражнения для спины дома",
    "как сэкономить на продуктах",
]

# E1: главный логгер бота. Каждый поток-аккаунт получает свой адаптер
# через get_account_logger(...) с пристёгнутым account_id, поэтому в
# каждом сообщении видно, какой аккаунт его породил.
_bot_logger = logging.getLogger("bot")


def log(account_name, msg):
    """
    E1: совместимый wrapper. Для нового кода предпочтителен
    get_account_logger(__name__, account_id).info(...).

    Раньше log() писал через print + _tg.add_log(). Теперь — через
    стандартный logging; TGBufferHandler доставляет строки в TG-буфер,
    а HumanFormatter печатает их в stderr.
    """
    get_account_logger(_bot_logger.name, account_name).info(msg)


# ══════════════════════════════════════════════════════════════════════════════
# AdsPower API
# ══════════════════════════════════════════════════════════════════════════════


class AdsPowerAPI:
    """Wrapper over local REST API AdsPower.
    Proxies are configured directly in AdsPower - not needed here.
    Profiles are created manually in AdsPower; the bot only starts and stops them.
    """

    def __init__(self, base_url: str, api_key: str | None = None):
        # L3: implicit Optional → explicit str | None (PEP 604).
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def start_profile(self, user_id: str) -> int:
        """Starts the AdsPower profile. Returns the remote debug port."""
        params = {"user_id": user_id}
        headers = {}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            # Masked log for debugging
            key_hint = (
                f"{self.api_key[:4]}...{self.api_key[-4:]}" if len(self.api_key) > 8 else "****"
            )
            # L1: print → logger.debug (debug-level чтобы не шуметь в обычных логах)
            _bot_logger.debug("[AdsPower] Using API Key: %s", key_hint)
            # Warn if API key sent over non-localhost connection (MITM risk)
            if not self.base.startswith(("http://localhost", "http://127.0.0.1")):
                _bot_logger.warning(
                    "[AdsPower] API key передаётся по не-localhost URL %s — MITM риск!",
                    self.base,
                )

        r = requests.get(
            self._url("/api/v1/browser/start"), params=params, headers=headers, timeout=60
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"AdsPower: {data.get('msg', 'unknown error')}")
        info = data["data"]
        # AdsPower returns debug_port or ws.selenium = "127.0.0.1:PORT"
        port = info.get("debug_port")
        if not port and "ws" in info:
            selenium_addr = info["ws"].get("selenium", "")
            if ":" in selenium_addr:
                port = selenium_addr.split(":")[-1]
        return int(port)

    def stop_profile(self, user_id: str):
        try:
            params = {"user_id": user_id}
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            requests.get(
                self._url("/api/v1/browser/stop"), params=params, headers=headers, timeout=15
            )
        except (requests.RequestException, ValueError) as exc:
            # L12: bare → конкретные. RequestException — все сетевые/HTTP ошибки
            # requests, ValueError — на случай invalid params/url. Stop — fire-and-
            # forget, но логируем для диагностики.
            _bot_logger.debug("stop_profile(%s) failed: %s", user_id, exc)

    def update_proxy(self, user_id: str, proxy_str: str) -> bool:
        """L11: устанавливает прокси для AdsPower-профиля через REST API.

        proxy_str format: ``host:port[:user:pass]`` (host:port минимум,
        опционально логин/пароль). Кодирование в `user_proxy_config` —
        как требует AdsPower (proxy_soft=other, proxy_type=socks5).

        Returns:
            True при успехе (`code == 0`), False иначе (включая сетевые
            ошибки и невалидный JSON в ответе). Ошибки молча подавляются —
            вызывающий код имеет fallback (см. `_apply_account_proxy`).
        """
        # Парсинг прокси: поддерживаем IPv6 ([::1]:8080:user:pass) и
        # обычный формат (host:port[:user:pass]), а также user:pass@host:port
        proxy_config: dict[str, str] = {
            "proxy_soft": "other",
            "proxy_type": "socks5h",
        }

        # Формат user:pass@host:port
        if "@" in proxy_str:
            creds_part, host_part = proxy_str.rsplit("@", 1)
            # host_part может быть IPv6: [::1]:8080
            if host_part.startswith("["):
                bracket_end = host_part.index("]")
                proxy_config["proxy_host"] = host_part[1:bracket_end]
                proxy_config["proxy_port"] = host_part[bracket_end + 2 :]  # skip ]:
            else:
                hp = host_part.split(":")
                if len(hp) < 2:
                    return False
                proxy_config["proxy_host"] = hp[0]
                proxy_config["proxy_port"] = hp[1]
            # creds_part: user:pass
            creds = creds_part.split(":", 1)
            proxy_config["proxy_user"] = creds[0]
            if len(creds) > 1:
                proxy_config["proxy_password"] = creds[1]
        else:
            parts = proxy_str.split(":")
            if len(parts) < 2:
                return False
            # IPv6: [::1]:8080
            if proxy_str.startswith("["):
                bracket_end = proxy_str.index("]")
                proxy_config["proxy_host"] = proxy_str[1:bracket_end]
                rest = proxy_str[bracket_end + 2 :].split(":")  # skip ]:
                proxy_config["proxy_port"] = rest[0]
                if len(rest) >= 3:
                    proxy_config["proxy_user"] = rest[1]
                    proxy_config["proxy_password"] = rest[2]
            else:
                proxy_config["proxy_host"] = parts[0]
                proxy_config["proxy_port"] = parts[1]
                if len(parts) >= 4:
                    proxy_config["proxy_user"] = parts[2]
                    proxy_config["proxy_password"] = parts[3]

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {"user_id": user_id, "user_proxy_config": proxy_config}

        try:
            r = requests.post(
                self._url("/api/v1/user/update"),
                json=payload,
                headers=headers,
                timeout=15,
            )
            return r.json().get("code") == 0
        except (requests.RequestException, ValueError):
            # RequestException — сетевые ошибки, ValueError — если AdsPower
            # вернул не-JSON.
            return False

    def is_profile_running(self, user_id: str) -> bool:
        """Проверяет, запущен ли профиль в AdsPower прямо сейчас."""
        try:
            params = {"user_id": user_id}
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            r = requests.get(
                self._url("/api/v1/browser/active"),
                params=params,
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                return False
            return data.get("data", {}).get("status") == "Active"
        except (requests.RequestException, ValueError):
            return False


# ══════════════════════════════════════════════════════════════════════════════
# Selenium helpers
# ══════════════════════════════════════════════════════════════════════════════


def _detect_chromium_version(debug_port: int) -> str | None:
    """
    Спрашиваем у Chrome его собственную версию через DevTools-протокол
    (endpoint /json/version). Нужно для пина chromedriver — AdsPower обычно
    отстаёт от latest stable, и ChromeDriverManager без подсказки качает
    chromedriver под latest, который не работает с более старым Chromium.

    Возвращает строку вида "147.0.7727.56" или None если не удалось.
    """
    try:
        resp = requests.get(f"http://127.0.0.1:{debug_port}/json/version", timeout=10)
        # "Browser": "Chrome/147.0.7727.56"
        browser = resp.json().get("Browser", "")
        if "/" in browser:
            return browser.split("/", 1)[1].strip() or None
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def connect_to_sphere(debug_port: int) -> webdriver.Chrome:
    """Connect Selenium to already running AdsPower profile.

    AdsPower упаковывает свой Chromium, который часто отстаёт от latest
    stable. ChromeDriverManager без подсказки качает chromedriver под latest
    и ломается с "This version of ChromeDriver only supports Chrome version N".
    Поэтому сначала спрашиваем у самого Chrome его версию и просим
    chromedriver-manager скачать именно её.
    """
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")

    browser_version = _detect_chromium_version(debug_port)
    if browser_version:
        _bot_logger.info("AdsPower Chromium version: %s", browser_version)

    try:
        if browser_version:
            service = Service(ChromeDriverManager(driver_version=browser_version).install())
        else:
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        # L1: print → logger.warning (заметно в логах, но не критично)
        _bot_logger.warning("Standard driver install failed: %s. Trying fallback...", e)
        driver = webdriver.Chrome(options=options)

    # T7 + T8: stealth-скрипт через CDP — маскируем navigator.webdriver и
    # чистим cdc_* глобалы. Регистрируется ДО первой driver.get(), чтобы
    # сработал на всех будущих страницах.
    if not _apply_stealth(driver):
        _bot_logger.warning(
            "T7/T8: stealth-инъекция не применилась (CDP недоступен?) — "
            "AdsPower обязан замаскировать navigator.webdriver сам."
        )

    return driver


def load_cookies(driver: webdriver.Chrome, cookies_path: str, domain: str):
    """Loads cookies from JSON file to the required domain."""
    path = Path(cookies_path)
    if not path.exists():
        return

    try:
        # Navigate to a subpage first to set context without fully loading home page
        driver.get(f"https://{domain}/robots.txt")
        time.sleep(2)
    except Exception:
        pass

    try:
        with open(path, encoding="utf-8") as f:
            cookies = json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        _bot_logger.warning("load_cookies: corrupt file %s: %s — skipping", path, e)
        return

    for c in cookies:
        cookie_dict = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
        }

        # Handle expiry correctly
        expiry = c.get("expiry") or c.get("expirationDate")
        if expiry:
            cookie_dict["expiry"] = int(expiry)

        try:
            driver.add_cookie(cookie_dict)
        except Exception:
            _bot_logger.debug("load_cookies: failed to add cookie %s", cookie_dict.get("name"))

    try:
        driver.get(f"https://{domain}")
        time.sleep(3)
    except Exception:
        pass


# ── Behavioral primitives ──────────────────────────────────────────────────

# B2: human-like delays — нормальное распределение вместо плоского uniform,
# с прерыванием по stop_event. Реализация в human_delay.py (импортирован выше).


def hp(lo=0.5, hi=1.5, *, distribution="normal", stop_event=None):
    """
    Backward-compatible wrapper: старые вызовы hp(lo, hi) теперь идут через
    human_delay c distribution='normal' и поддержкой раннего выхода по
    stop_event (TG /stop).
    """
    return _human_delay(lo, hi, distribution=distribution, stop_event=stop_event)


def human_type(
    element,
    text,
    speed_range=(0.05, 0.25),
    *,
    speed_multiplier=1.0,
    enable_typos=True,
    stop_event=None,
):
    """T5: делегируется в human_typing.type_human (бёрсты + опечатки).

    Сохраняет backward-compatible сигнатуру (element, text, speed_range=...).
    Новые опциональные kwargs:
        speed_multiplier — общий множитель задержек (persona-driven).
        enable_typos — для логин-форм лучше False (backspace может ломать
            валидаторы).

    Возвращает True если допечатало до конца, False если прервано stop_event
    (TG /stop).
    """
    return _type_human(
        element,
        text,
        speed_range=speed_range,
        speed_multiplier=speed_multiplier,
        enable_typos=enable_typos,
        stop_event=stop_event,
    )


def random_mouse_move(driver):
    """Simulates random mouse movements across the page."""
    try:
        actions = ActionChains(driver)
        width = driver.execute_script("return window.innerWidth;")
        height = driver.execute_script("return window.innerHeight;")

        for _ in range(random.randint(2, 5)):
            x = random.randint(0, width - 1)
            y = random.randint(0, height - 1)
            actions.move_by_offset(x - width // 2, y - height // 2)  # Approximate
            actions.pause(random.uniform(0.1, 0.4))
            # Reset to center or just use move_to_element on random elements
            elements = driver.find_elements(By.TAG_NAME, "a")[:10]
            if elements:
                actions.move_to_element(random.choice(elements))
        actions.perform()
    except Exception:
        pass


def move_click(driver, element, stop_event=None):
    """T6: «человеческий» клик через Bezier-движение курсора + jitter.

    Раньше: один move_to_element_with_offset (telepport-style move) + click.
    Теперь: 15-30 промежуточных mousemove'ов по Bezier-кривой, ±3px
    jitter вокруг центра, изредка overshoot/correction. Fallback на
    native click и JS click при ошибках.

    Сохранена сигнатура и семантика «никогда не raise» — все вызовы
    места `move_click(driver, element)` продолжают работать.
    """
    _human_click(driver, element, stop_event=stop_event)


def human_scroll(driver, direction="down", iters=None, stop_event=None):
    """T9: тонкая обёртка над human_scroll.human_scroll.

    Раньше: ровные scrollBy-jump'ы по 150-500px каждый, пауза 0.3-1.1s.
    Теперь: inertia-свайпы 300-900px по cubic ease-out, reading-паузы
    адаптивные по объёму видимого текста, 15% шанс back-scroll.

    Сохраняем старую сигнатуру (direction, iters) для обратной
    совместимости — параметр `iters` маппится на `swipes`.
    """
    _human_scroll(
        driver,
        direction=direction,
        swipes=iters,
        stop_event=stop_event,
    )


def slow_scroll_to(driver, element):
    current_y = driver.execute_script("return window.pageYOffset;")
    target_y = driver.execute_script(
        "return arguments[0].getBoundingClientRect().top + window.pageYOffset - 120;", element
    )
    steps = random.randint(12, 22)
    delta = (target_y - current_y) / max(steps, 1)
    for _ in range(steps):
        current_y += delta
        driver.execute_script(f"window.scrollTo(0, {current_y});")
        time.sleep(random.uniform(0.04, 0.13))


# ── Gallery ──────────────────────────────────────────────────────────────────


def scroll_gallery(driver, wait, stop_event=None):
    try:
        wait.until(EC.presence_of_element_located((By.XPATH, "//*[@data-marker='image-frame']")))
    except Exception:
        return
    hp(3, 7, stop_event=stop_event)
    # F10: число пролистываний — weighted distribution вместо uniform.
    # Реальный пользователь чаще смотрит 1-3 фото (60%), реже 4-7 (30%),
    # редко листает все (10%). Uniform randint(1,12) — паттерн бота.
    _GALLERY_WEIGHTS = [30, 20, 15, 10, 8, 5, 4, 3, 2, 1, 1, 1]  # 1..12
    iters = random.choices(range(1, 13), weights=_GALLERY_WEIGHTS, k=1)[0]
    for _ in range(iters):
        btns = driver.find_elements(
            By.XPATH,
            "//*[@data-marker='image-frame/right-button'] | "
            "//button[contains(@class,'gallery-next')] | "
            "//button[contains(@aria-label,'следующ')] | "
            "//button[contains(@aria-label,'Next')]",
        )
        if not btns or not btns[0].is_displayed():
            break
        # T6: human-like click вместо JS-телепорта.
        if not _human_click(driver, btns[0], stop_event=stop_event):
            break
        hp(3, 7, stop_event=stop_event)


# ── View listing ──────────────────────────────────────────────────────────────


def check_block(driver, account_name):
    """Checks if the page shows an IP block or captcha with improved accuracy."""
    # Specific phrases that indicate a block, not just presence of the word 'captcha' in scripts
    block_patterns = [
        "Доступ ограничен",
        "проблема с IP",
        "нажмите на кнопку Продолжить",
        "подтвердите, что вы не робот",
        "checkbox-captcha",
        "verify your identity",
        "Ваш IP временно заблокирован",
    ]

    # Check URL first — cheapest operation (no DOM access)
    try:
        url = driver.current_url or ""
        url_lower = url.lower()
        for marker in ("/firewall/", "/captcha", "captcha.html", "/blocked"):
            if marker in url_lower:
                log(account_name, f"!!! ALERT: BLOCK DETECTED BY URL ({marker}) !!!")
                return True
    except Exception:
        pass

    # Check page title — cheap operation
    try:
        title = driver.title
        if "Ой!" in title or "Captcha" in title:
            log(account_name, f"!!! ALERT: BLOCK DETECTED BY TITLE ({title}) !!!")
            return True
    except Exception:
        pass

    # page_source — expensive, only after cheap checks
    try:
        page_source = driver.page_source
    except Exception:
        log(account_name, "!!! ALERT: driver disconnected in check_block !!!")
        return True
    for pattern in block_patterns:
        if pattern in page_source:
            log(account_name, f"!!! ALERT: IP BLOCK DETECTED BY PATTERN ({pattern}) !!!")
            return True

    # Check for empty or tiny response (ERR_EMPTY_RESPONSE or failed load)
    if len(page_source) < 200:
        log(account_name, "!!! ALERT: EMPTY OR MALFORMED PAGE DETECTED !!!")
        return True

    return False


def _read_listing_meta(driver) -> tuple[str, int]:
    """T10: достать description-text и image_count из текущего листинга.

    Возвращает (description, image_count). На любую ошибку → ("", 0)
    — caller получит «короткий» dwell. Эта функция is best-effort: DOM
    Авито меняется, и если оба маркера не найдены — мы просто
    подставляем небольшой default, не падаем.
    """
    description = ""
    image_count = 0
    try:
        desc_el = driver.find_element(By.XPATH, "//div[@data-marker='item-view/item-description']")
        description = (desc_el.text or "").strip()
    except Exception:
        pass
    try:
        # Каждое изображение в галерее имеет data-marker='image-frame'
        # или image-frame/N. Считаем штук.
        imgs = driver.find_elements(By.XPATH, "//*[@data-marker='image-frame']")
        image_count = len(imgs)
    except Exception:
        pass
    return description, image_count


def _try_write_to_owner(
    driver,
    account_name,
    *,
    llm_classifier=None,
    db_manager=None,
    account=None,
) -> None:
    """Нажать «Написать» на странице листинга, сгенерировать первое сообщение
    через LLM и отправить собственнику. Best-effort: не валит view_listing
    при ошибке.

    Пишем собственнику (outbound-канал, без message_rate). Dedup через
    was_owner_contacted — если уже писали этому profile_id, скипаем.
    После успешной отправки — record_outbound для аналитики.
    """
    # 0. Dedup: если у нас нет db_manager — пишем без проверки (best-effort).
    profile_id = None
    listing_url = None

    # Извлекаем profile_id из DOM (селекторы из commercial_parser._extract_seller_info).
    # Извлекаем profile_id из DOM — несколько способов:
    # 1. Из seller-info (ссылка на профиль продавца)
    # 2. Из href кнопки «Написать» (содержит путь /user/<id>/)
    # 3. Из data-marker seller-link
    try:
        # Способ 1: seller-info links
        profile_links = driver.find_elements(
            By.XPATH,
            "//*[@data-marker='item-view/item-view-contacts']"
            "//a[contains(@href,'/user/') or contains(@href,'/brands/')]",
        )
        if not profile_links:
            # Способ 2: seller-link data-marker
            profile_links = driver.find_elements(By.CSS_SELECTOR, "a[data-marker*='seller-link']")
        if not profile_links:
            # Способ 3: любые ссылки на /user/ или /brands/
            profile_links = driver.find_elements(
                By.XPATH,
                "//a[contains(@href,'/user/') or contains(@href,'/brands/')]"
                "[not(contains(@href,'#login'))]",
            )
        if profile_links:
            profile_url = profile_links[0].get_attribute("href") or ""
            m = re.search(r"/user/([^/?]+)", profile_url)
            if not m:
                m = re.search(r"/brands/([^/?]+)", profile_url)
            profile_id = m.group(1) if m else None
    except Exception:
        pass

    # Текущий URL листинга.
    try:
        listing_url = driver.current_url
    except Exception:
        pass

    # Фильтр: не пишем агентствам. Проверяем прямо на странице Avito —
    # он показывает «Агентство» или «Собственник» в seller-info.
    # Это точнее чем эвристическая классификация из БД.
    try:
        seller_type = driver.execute_script(
            """
            // Avito seller-info labels
            var labels = document.querySelectorAll('[data-marker="seller-info/label"]');
            for (var i = 0; i < labels.length; i++) {
                var t = labels[i].textContent.trim().toLowerCase();
                if (t === 'агентство' || t === 'agent') return 'agent';
                if (t === 'собственник' || t === 'owner') return 'owner';
            }
            // Fallback: искать текст в seller-info секции
            var info = document.querySelector('[data-marker="seller-info"]');
            if (info) {
                var txt = info.textContent.toLowerCase();
                if (txt.indexOf('агентство') !== -1) return 'agent';
                if (txt.indexOf('собственник') !== -1) return 'owner';
            }
            // Fallback: весь текст страницы
            var body = document.body.innerText;
            var hasAgent = body.indexOf('Агентство') !== -1;
            var hasOwner = body.indexOf('Собственник') !== -1;
            if (hasAgent && !hasOwner) return 'agent';
            if (hasOwner && !hasAgent) return 'owner';
            return 'unknown';
        """
        )
        if seller_type == "agent":
            log(account_name, "  Продавец = «Агентство» (Avito) — скип.")
            return
        log(account_name, f"  Продавец тип: {seller_type} (Avito)")
    except Exception:
        pass

    # Если profile_id неизвестен — всё равно пишем (без dedup — не знаем кому).
    if not profile_id:
        log(account_name, "  profile_id не извлечён — пишем без dedup-проверки.")
    else:
        log(account_name, f"  profile_id={profile_id}, listing_url={listing_url}")

    if db_manager is not None and profile_id:
        if db_manager.was_owner_contacted(profile_id):
            log(account_name, f"  Собственник {profile_id} уже контактирован — скип.")
            return

    log(account_name, "  Пишем собственнику...")

    # 1. Клик «Написать» — селекторы Avito 2025-2026.
    # Важно: используем presence_of_element_located (не element_to_be_clickable),
    # потому что Avito часто рендерит кнопку за sticky-header или за overlay —
    # Selenium считает её «not clickable», но JS-click работает.
    # После нахождения — scrollIntoView + JS click.
    chat_btn_selectors = [
        (By.XPATH, "//a[@data-marker='messenger-button/link']"),
        (By.CSS_SELECTOR, "a[data-marker='messenger-button/link']"),
        (By.ID, "bx_contact-button_messenger"),
        (By.XPATH, "//a[@id='bx_contact-button_messenger']"),
        (By.XPATH, "//button[@data-marker='item-contact-bar/message-button']"),
        (By.XPATH, "//a[@data-marker='item-contact-bar/message-button']"),
        (By.XPATH, "//*[@data-marker='item-contact-bar/message-button']"),
        (By.XPATH, "//a[contains(., 'Написать сообщение')]"),
        (By.XPATH, "//button[contains(., 'Написать сообщение')]"),
        (By.XPATH, "//a[contains(., 'Написать')]"),
        (By.XPATH, "//button[contains(., 'Написать')]"),
    ]
    chat_btn = None
    for by, selector in chat_btn_selectors:
        try:
            chat_btn = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((by, selector))
            )
            break
        except Exception:
            continue

    # Fallback: JS-поиск по data-marker или тексту.
    if chat_btn is None:
        try:
            chat_btn = driver.execute_script(
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
            if chat_btn is not None:
                log(account_name, "  Кнопка «Написать» найдена через JS-fallback.")
        except Exception:
            pass

    if chat_btn is None:
        # Debug: какие data-marker есть на странице.
        try:
            markers = driver.execute_script(
                """
                var result = [];
                document.querySelectorAll('[data-marker]').forEach(function(el) {
                    var m = el.getAttribute('data-marker');
                    if (m.indexOf('message') !== -1 || m.indexOf('messenger') !== -1
                        || m.indexOf('contact') !== -1) {
                        result.push(m + '|' + el.tagName);
                    }
                });
                return result.join('; ') || '(none)';
            """
            )
            log(
                account_name,
                f"  Кнопка «Написать» не найдена. data-markers: {markers}",
            )
        except Exception:
            log(account_name, "  Кнопка «Написать» не найдена (debug failed).")
        return

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", chat_btn)
        hp(1.0, 2.5)
        # JS click — надёжнее чем Selenium click, обходит
        # ElementClickInterceptedException от sticky-header/overlay.
        driver.execute_script("arguments[0].click();", chat_btn)
        log(account_name, "  Кнопка «Написать» нажата (JS click).")
        hp(2, 4)
    except Exception:
        # Fallback: попробовать move_click если JS click не сработал.
        try:
            move_click(driver, chat_btn)
            log(account_name, "  Кнопка «Написать» нажата (move_click).")
            hp(2, 4)
        except Exception:
            log(account_name, "  Не удалось кликнуть «Написать».")
            return

    # 2. Текст сообщения — вариации от заказчика.
    # Один смысл, разные формулировки — чтобы Avito не детектил спам
    # и не давал теневой бан за идентичные сообщения.
    text = random.choice(
        [
            # Вариант 1 — исходный
            (
                "Здравствуйте. Занимаюсь строительством и сдачей небольших торговых "
                "центров, есть уже 15 готовых арендных бизнесов по Татарстану и сейчас "
                "нахожусь в поиске финансовых партнеров для строительства новых объектов. "
                "Средняя окупаемость объекта для инвестора от 6 до 8 лет.\n"
                "Интересно обсудить?"
            ),
            # Вариант 2 — другой порядок предложений
            (
                "Добрый день! Строю и сдаю небольшие торговые центры — на данный "
                "момент 15 арендных бизнесов работают по Татарстану. Сейчас ищу "
                "финансовых партнёров для новых объектов. Окупаемость для инвестора "
                "6-8 лет. Было бы интересно поговорить?"
            ),
            # Вариант 3 — короче, разговорнее
            (
                "Здравствуйте! У меня 15 действующих арендных бизнесов в Татарстане "
                "(торговые центры). Сейчас запускаю новые объекты и ищу инвесторов-"
                "партнёров. Срок окупаемости 6-8 лет. Подскажите, вам было бы "
                "интересно рассмотреть?"
            ),
            # Вариант 4 — с вопроса
            (
                "Здравствуйте, подскажите, рассматриваете варианты инвестиций в "
                "коммерческую недвижимость? Я занимаюсь строительством торговых "
                "центров — 15 объектов уже работают по Татарстану. Сейчас набираю "
                "партнёров на новые проекты, окупаемость 6-8 лет."
            ),
            # Вариант 5 — от первого лица, другой акцент
            (
                "Приветствую! Строю торговые центры и сдаю в аренду — 15 объектов "
                "по Татарстану уже приносят доход. Расширяюсь и ищу финансового "
                "партнёра на новые площадки. Инвестор выходит в плюс за 6-8 лет. "
                "Готов обсудить детали, если интересно."
            ),
            # Вариант 6 — более деловой
            (
                "Здравствуйте. Мы строим и управляем небольшими торговыми центрами "
                "в Татарстане — в портфолио 15 работающих арендных бизнесов. На "
                "данный момент привлекаем соинвесторов на новые проекты. Средний "
                "срок возврата инвестиций — от 6 до 8 лет. Есть ли интерес обсудить?"
            ),
            # Вариант 7 — короткий и прямой
            (
                "Добрый день! 15 арендных бизнесов в Татарстане (ТЦ) — мои. Ищу "
                "инвесторов на новые объекты. Окупаемость 6-8 лет. Интересно?"
            ),
            # Вариант 8 — с упоминанием региона гибко
            (
                "Здравствуйте! Занимаюсь коммерческой недвижимостью — 15 торговых "
                "центров уже сданы в аренду в Республике Татарстан. Сейчас ищу "
                "партнёров-инвесторов для строительства следующих объектов. "
                "Окупаемость порядка 6-8 лет. Хотелось бы обсудить, если актуально."
            ),
            # Вариант 9 — мягкий заход
            (
                "Здравствуйте! Увидел ваше объявление — интересно. Сам занимаюсь "
                "торговыми центрами, 15 объектов по Татарстану уже работают. Ищу "
                "финансовых партнёров для новых строек, окупаемость 6-8 лет. "
                "Может быть рассмотрим варианты?"
            ),
            # Вариант 10 — акцент на цифрах
            (
                "Добрый день! 15 арендных бизнесов, 6-8 лет окупаемость, Татарстан — "
                "это мои текущие объекты. Расширяюсь и ищу инвесторов-партнёров на "
                "новые торговые центры. Рассмотрите предложение?"
            ),
        ]
    )

    # 3. Найти chat-input и ввести текст.
    # Avito mini-messenger popup имеет ДВА textarea:
    #   icebreakers/textarea — предзаполнен «Здравствуйте! », основной видимый
    #   reply/input          — пустой, маленький, внизу (НЕ основной)
    # Пишем в icebreakers/textarea (предпочтительный) или reply/input,
    # но ВСЕГДА сначала очищаем Ctrl+A → Delete, иначе текст смешивается
    # с предзаполненным айсбрейкером и сообщение уходит обрезанным.
    input_selectors = [
        # Приоритет 1: icebreakers/textarea — основной видимый input Avito
        (By.CSS_SELECTOR, "textarea[data-marker='icebreakers/textarea']"),
        (By.XPATH, "//textarea[@data-marker='icebreakers/textarea']"),
        # Приоритет 2: reply/input — запасной
        (By.CSS_SELECTOR, "textarea[data-marker='reply/input']"),
        (By.XPATH, "//textarea[@data-marker='reply/input']"),
        # Приоритет 3: другие
        (By.XPATH, "//textarea[@data-marker='message-input']"),
        (By.CSS_SELECTOR, "textarea[data-marker='message-input']"),
        (By.XPATH, "//textarea[contains(@placeholder, 'Сообщ')]"),
        (By.XPATH, "//textarea[contains(@placeholder, 'сообщ')]"),
        (By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']"),
        (By.XPATH, "//textarea"),
    ]
    input_el = None
    for by, selector in input_selectors:
        try:
            # visibility_of — ждём что элемент реально видимый, не display:none
            input_el = WebDriverWait(driver, 3).until(
                EC.visibility_of_element_located((by, selector))
            )
            break
        except Exception:
            continue

    # JS-fallback: найти ВИДИМЫЙ input через JS если Selenium не видит.
    if input_el is None:
        try:
            input_el = driver.execute_script(
                """
                // Приоритет: icebreakers/textarea > reply/input > любой textarea
                var candidates = [
                    'textarea[data-marker="icebreakers/textarea"]',
                    'textarea[data-marker="reply/input"]',
                    'textarea[data-marker="message-input"]'
                ];
                for (var i = 0; i < candidates.length; i++) {
                    var el = document.querySelector(candidates[i]);
                    if (el && el.offsetParent !== null) return el;
                }
                var ce = document.querySelector('div[contenteditable="true"][role="textbox"]');
                if (ce && ce.offsetParent !== null) return ce;
                var tas = document.querySelectorAll('textarea');
                for (var i = 0; i < tas.length; i++) {
                    if (tas[i].offsetParent !== null) return tas[i];
                }
                return null;
                """
            )
            if input_el is not None:
                log(account_name, "  Chat-input найден через JS-fallback (visibility).")
        except Exception:
            pass

    if input_el is None:
        log(account_name, "  Chat-input не найден после клика «Написать».")
        return

    # Debug: показать tag + data-marker найденного элемента.
    try:
        tag = input_el.tag_name
        marker = input_el.get_attribute("data-marker") or ""
        log(account_name, f"  Chat-input найден: <{tag}> marker={marker}")
    except Exception:
        pass

    try:
        move_click(driver, input_el)
        hp(0.4, 1.2)
        # Очистка поля: Avito предзаполняет icebreakers/textarea текстом
        # вроде «Здравствуйте! ». Если не очистить — наше сообщение
        # смешивается с айсбрейкером и уходит обрезанным.
        try:
            input_el.send_keys(Keys.CONTROL + "a")
            input_el.send_keys(Keys.DELETE)
            hp(0.2, 0.4)
        except Exception:
            pass
        # Реалистичный typing через human_typing.
        from human_typing import length_speed_multiplier, persona_speed_multiplier, type_human

        persona_id = (account or {}).get("persona")
        speed_mul = persona_speed_multiplier(persona_id) * length_speed_multiplier(text)
        if not type_human(
            input_el,
            text,
            speed_range=(0.05, 0.20),
            speed_multiplier=speed_mul,
            enable_typos=True,
            stop_event=_tg.get_account_stop_event(account_name),
        ):
            log(account_name, "  Ввод прерван (stop_event).")
            return
    except Exception as exc:
        log(account_name, f"  Ошибка ввода текста: {exc}")
        return

    hp(1.5, 3.5)

    # 4. Нажать Send.
    # Avito 2026: кнопка отправки — <svg role="button" data-marker="icebreakers/send-message">.
    # arguments[0].click() на SVG не работает (React не ловит программный клик).
    # Надёжный способ: Enter в textarea (нативный для web-чатов).
    # Запасной: dispatchEvent(MouseEvent) на SVG-элемент.
    from selenium.webdriver.common.keys import Keys

    # Способ 1: Enter в textarea — самый надёжный для Avito
    try:
        input_el.send_keys(Keys.ENTER)
        log(account_name, "  Сообщение отправлено (Enter в textarea).")
    except Exception:
        log(account_name, "  Enter в textarea не удался, пробуем SVG-кнопку...")

        # Способ 2: dispatchEvent на SVG[role="button"]
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
            if clicked:
                log(account_name, "  Сообщение отправлено (SVG dispatchEvent).")
            else:
                raise RuntimeError("SVG not found")
        except Exception:
            # Способ 3: move_click на найденный элемент
            try:
                send_el = driver.find_element(
                    By.CSS_SELECTOR, "[data-marker='icebreakers/send-message']"
                )
                if move_click(driver, send_el):
                    log(account_name, "  Сообщение отправлено (move_click на SVG).")
                else:
                    log(account_name, "  Все способы отправки упали.")
                    return
            except Exception:
                log(account_name, "  Все способы отправки упали.")
                return

    hp(2, 5)

    # 5. Закрываем чат-оверлей — после отправки Avito может оставить overlay
    # открытым, что ломает driver.back() в browse/find_and_view.
    try:
        # Пробуем нажать Escape (закрывает большинство overlay-окон Avito).
        from selenium.webdriver.common.keys import Keys

        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        hp(1, 2)
    except Exception:
        pass

    # 6. Post-send: детект капчи + запись в БД.
    from captcha_detect import detect_phone_captcha

    if detect_phone_captcha(driver, log_func=log, account_name=account_name):
        log(account_name, "  КАПЧА после отправки сообщения — записываем.")
        account_state.mark_captcha(account_name, captcha_type="avito_message_send")
        return

    log(
        account_name,
        f"  ✉ Сообщение отправлено собственнику: {text[:80]}{'...' if len(text) > 80 else ''}",
    )

    # Записываем outbound-контакт + метрики.
    # Запись только если profile_id извлечён — чтобы не дедупить "unknown"
    # друг против друга (два разных собственника без profile_id != один и тот же).
    if db_manager is not None and profile_id:
        try:
            persona_label = (account or {}).get("persona") or ""
            with db_manager.transaction() as cur:
                db_manager.record_outbound(
                    account_name=account_name,
                    profile_id=profile_id,
                    listing_url=listing_url,
                    status="sent",
                    persona=persona_label,
                    message_text=text,
                    cursor=cur,
                )
                db_manager.incr_metric(account_name, "messages_sent", cursor=cur)
                db_manager.incr_metric(account_name, "outbound_initiated", cursor=cur)
        except Exception:
            pass
    elif db_manager is not None:
        # profile_id неизвестен — только метрику, без record_outbound.
        try:
            db_manager.incr_metric(account_name, "messages_sent")
        except Exception:
            pass


def _generate_view_listing_message(
    driver,
    llm_classifier,
    account_name: str,
    account: dict | None,
) -> str | None:
    """Генерация первого сообщения собственнику прямо из view_listing.
    Извлекает данные листинга из DOM и вызывает LLM через outbound-промпты.
    Возвращает sanitized текст или None.
    """
    # Извлекаем данные листинга из текущей страницы.
    listing_info = {}
    try:
        title_el = driver.find_element(By.XPATH, "//h1[@data-marker='item-view/title-info']")
        listing_info["title"] = title_el.text or ""
    except Exception:
        listing_info["title"] = ""
    try:
        price_el = driver.find_element(
            By.XPATH,
            "//span[@data-marker='item-view/item-price'] | "
            "//div[contains(@class,'price-value')] | "
            "//span[contains(@class,'price')]",
        )
        listing_info["price"] = price_el.text or "не указана"
    except Exception:
        listing_info["price"] = "не указана"
    try:
        desc_el = driver.find_element(By.XPATH, "//div[@data-marker='item-view/item-description']")
        listing_info["description"] = (desc_el.text or "")[:600]
    except Exception:
        listing_info["description"] = ""
    try:
        loc_el = driver.find_element(
            By.XPATH,
            "//span[@data-marker='item-view/item-location'] | //div[contains(@class,'geo')]",
        )
        listing_info["location"] = loc_el.text or "—"
    except Exception:
        listing_info["location"] = "—"
    listing_info["category"] = "коммерческая недвижимость"
    listing_info["area"] = "—"

    # Выбираем персону.
    persona_id = (account or {}).get("persona") or ""
    from outbound_messenger import (
        _APPROACH_HINTS,
        _FORMALITY_LEVELS,
        _LENGTH_HINTS,
        _PITCH_APPROACH_HINTS,
        _PITCH_FORMALITY_LEVELS,
        _PITCH_LENGTH_HINTS,
        PERSONAS,
        _is_pitch_persona,
    )

    persona_description = PERSONAS.get(persona_id, "")
    is_pitch = _is_pitch_persona(persona_id)

    if is_pitch:
        formality = random.choice(_PITCH_FORMALITY_LEVELS)
        length = random.choice(_PITCH_LENGTH_HINTS)
        approach = random.choice(_PITCH_APPROACH_HINTS)
        system_prompt_name = "outbound_first_message_pitch.system.txt"
        user_prompt_name = "outbound_first_message_pitch.user.txt"
    else:
        formality = random.choice(_FORMALITY_LEVELS)
        length = random.choice(_LENGTH_HINTS)
        approach = random.choice(_APPROACH_HINTS)
        system_prompt_name = "outbound_first_message.system.txt"
        user_prompt_name = "outbound_first_message.user.txt"

    try:
        from llm_classifier import _load_prompt
        from llm_sanitizer import sanitize_llm_reply

        system_message = _load_prompt(system_prompt_name)
        user_prompt = _load_prompt(user_prompt_name).format(
            title=listing_info.get("title", "—"),
            category=listing_info.get("category", "коммерческая недвижимость"),
            location=listing_info.get("location", "—"),
            area=listing_info.get("area", "—"),
            price=listing_info.get("price", "не указана"),
            description=listing_info.get("description", "")[:600],
            persona_description=persona_description,
            formality=formality,
            length=length,
            approach=approach,
        )
        raw = llm_classifier._call_llm(
            system_message=system_message,
            user_message=user_prompt,
            temperature=0.95,
        )
        if not raw:
            return None
        raw = raw.strip('"').strip("'").strip()
        clean, reason = sanitize_llm_reply(raw)
        if clean is None:
            log(account_name, f"  LLM sanitizer отбросил ({reason}): {raw[:60]}")
            return None
        return clean
    except Exception as exc:
        log(account_name, f"  Ошибка генерации сообщения: {exc}")
        return None


def view_listing(
    driver,
    wait,
    account_name,
    *,
    favorite_rate=0.08,
    call_rate=None,
    message_rate=0.05,
    db_manager=None,
    llm_classifier=None,
    account=None,
):
    """
    F1: favorite_rate — вероятность «Добавить в избранное» (default 8%).
    F1: message_rate — вероятность нажать «Написать» и отправить сообщение (default 5%).
    Пишем не каждому собственнику — только тем, кто прошёл вероятностный фильтр,
    dedup через was_owner_contacted (если уже писали — скип).
    Оба параметра конфигурируются через config.json / accounts.json
    (ключи view_listing_favorite_rate / view_listing_message_rate).

    call_rate — устаревший алиас для message_rate (back-compat).
    Если передан call_rate и message_rate не задан — используем call_rate.

    T10: dwell_time теперь зависит от контента —
    `compute_reading_dwell(description, image_count, interest)`. Раньше
    F9 lognormal не учитывал, что на странице (пустой список или
    1500-символьное описание + 15 фото). Теперь:
      • interest ∈ [0, 1] выбирается рандомно: 20% «совсем не моё» (≤0.2),
        15% «очень интересно» (≥0.7), остальные 65% — нейтрально (0.3-0.7).
      • dwell ∝ base × log(text+1) × (1 + 0.04·images) × (0.5 + interest).
      • Клампим в [5, 300] сек.
      • 20% «совсем не моё» — возвращаемся рано (без scroll/favorite/call).

    T20: db_manager (опциональный) — если передан, записывается sample
    `dwell_sec` для percentile-аудита в /health. Не передан → no-op
    (back-compat для тестов).
    """
    if check_block(driver, account_name):
        return False

    try:
        wait.until(
            EC.presence_of_element_located((By.XPATH, "//h1[@data-marker='item-view/title-info']"))
        )
    except Exception:
        return True

    # T10: достаём контент перед dwell calculation.
    description, image_count = _read_listing_meta(driver)

    # T10: interest score — ширина 3-уровневая, чтобы было разнообразие.
    interest_roll = random.random()
    if interest_roll < 0.20:
        interest = random.uniform(0.0, 0.20)  # совсем не моё
    elif interest_roll < 0.35:
        interest = random.uniform(0.70, 1.00)  # очень интересно
    else:
        interest = random.uniform(0.30, 0.70)  # нейтрально

    dwell_time = _compute_reading_dwell(
        description=description,
        image_count=image_count,
        interest=interest,
    )
    log(
        account_name,
        f"  Viewing listing (dwell={dwell_time:.1f}s, "
        f"text={len(description)} chars, imgs={image_count}, "
        f"interest={interest:.2f})...",
    )

    # T20: записываем sample для percentile-аудита.
    if db_manager is not None:
        try:
            db_manager.record_behavioral_sample(account_name, "dwell_sec", float(dwell_time))
        except Exception as e:
            log(account_name, f"  T20: не смог записать dwell sample: {e}")

    # F1: «Написать» — инициируем диалог с собственником.
    # ВЫЗЫВАЕТСЯ ДО ранней остановки interest < 0.10 — мы должны написать
    # каждому собственнику, даже если объявление «неинтересно» для browsing.
    effective_message_rate = call_rate if call_rate is not None else message_rate
    if random.random() < effective_message_rate:
        _try_write_to_owner(
            driver,
            account_name,
            llm_classifier=llm_classifier,
            db_manager=db_manager,
            account=account,
        )

    # T10/F9: совсем неинтересно → закрываем рано (после отправки сообщения).
    if interest < 0.10:
        log(account_name, "  T10: неинтересно, закрываем")
        return True

    # T10: общий dwell-time распределяем по dwell_time, не одной паузой —
    # внутри будут ещё scroll/mouse_move/pause-фазы. На стартовый «осмотр»
    # тратим 30% от плана.
    initial_glance = max(2.0, dwell_time * 0.3)
    hp(initial_glance, initial_glance)

    # F10: галерея смотрится не каждый раз (60% probability). Реальный
    # пользователь часто смотрит только первое фото и идёт дальше.
    if random.random() < 0.60:
        scroll_gallery(driver, wait)

    # Random sequence of interactions
    actions = ["scroll", "mouse_move", "pause", "scroll_to_desc"]
    random.shuffle(actions)

    for action in actions:
        if action == "scroll":
            human_scroll(driver, "down", iters=random.randint(1, 3))
        elif action == "mouse_move":
            random_mouse_move(driver)
        elif action == "pause":
            hp(2, 6)
        elif action == "scroll_to_desc":
            try:
                desc = driver.find_element(
                    By.XPATH, "//div[@data-marker='item-view/item-description']"
                )
                slow_scroll_to(driver, desc)
                # T10: чтение описания пропорционально его длине + interest.
                # Берём 50% от рассчитанного dwell — это «активное» чтение
                # описания (остальные 50% уходят на initial_glance + scroll
                # фазы).
                reading_share = max(3.0, dwell_time * 0.5)
                hp(reading_share, reading_share)
            except Exception:
                pass
        hp(1, 3)

    # Added more natural pauses and random behaviors
    if random.random() < 0.4:
        random_mouse_move(driver)
        hp(1, 4)

    # F1: «Добавить в избранное» — реалистичная вероятность 8% вместо 70%.
    # Конфигурируется через view_listing_favorite_rate в config.json/accounts.json.
    if random.random() < favorite_rate:
        try:
            fav_btn = WebDriverWait(driver, 6).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[@data-marker='item-view/favorite-button'] | "
                        "//button[contains(@class,'favorites-button')] | "
                        "//button[contains(@aria-label,'избранн')] | "
                        "//button[contains(@aria-label,'Добавить в избранное')]",
                    )
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", fav_btn)
            hp(1.5, 3)
            move_click(driver, fav_btn)
            log(account_name, "  Added to favorites.")
            hp(2, 5)
        except Exception:
            pass

    # Final dwell before leaving
    hp(2, 5)
    return True


# ── F2: Variable batch sizes ──────────────────────────────────────────────────

# Весовое распределение для числа листингов за один запрос/категорию.
# Индекс = количество листингов; 0 исключён — всегда ≥ 1.
# Пик на 2-4, длинный хвост до max_n. Реальный пользователь:
#   иногда смотрит 1-2 (надоело), чаще 2-4, изредка 6-7 (заинтересован).
_LISTING_COUNT_WEIGHTS = [0, 0.10, 0.25, 0.30, 0.20, 0.10, 0.04, 0.01]  # idx 0..7


def _weighted_listing_count(max_n: int = 7) -> int:
    """
    F2: возвращает случайное число листингов от 1 до max_n включительно,
    взвешенное по _LISTING_COUNT_WEIGHTS. Результат всегда ≥ 1.

    max_n > 7 округляется до 7 (длина таблицы весов).
    """
    effective_max = min(max_n, len(_LISTING_COUNT_WEIGHTS) - 1)
    effective_max = max(1, effective_max)
    weights = _LISTING_COUNT_WEIGHTS[: effective_max + 1]
    # random.choices нормализует веса, поэтому weights[0]=0 — не проблема.
    result = random.choices(range(len(weights)), weights=weights)[0]
    return max(1, result)  # гарантируем ≥ 1


# ══════════════════════════════════════════════════════════════════════════════
# Stage 0 - Yandex warmup
# ══════════════════════════════════════════════════════════════════════════════


def safe_get(driver, url, account_name, retries=2):
    """Safely navigate to a URL with retries for empty responses."""
    for i in range(retries + 1):
        try:
            driver.get(url)
            hp(2, 4)
            if not check_block(driver, account_name):
                return True
            log(account_name, f"  Navigation to {url} blocked or empty. Retry {i + 1}/{retries}...")
        except Exception as e:
            log(account_name, f"  Navigation error: {str(e)[:50]}. Retry {i + 1}/{retries}...")
        hp(3, 7)
    return False


# F4: тематические запросы для Yandex warmup (коммерческая недвижимость).
# Расширены с 8 до 28 — разные формулировки, регионы, типы объектов.
# Цель: не повторять один и тот же запрос каждый день → меньше паттерна.
THEMATIC_QUERIES = [
    # Утвердительные — «купить/арендовать»
    "коммерческая недвижимость в москве купить",
    "аренда офиса от собственника",
    "купить офис в центре москвы",
    "склады и производства продажа",
    "торговые площади в аренду миллионники",
    "купить готовый бизнес в россии",
    "инвестиции в коммерческую недвижимость",
    "авито коммерческая недвижимость",
    "помещение свободного назначения купить",
    "аренда склада от собственника московская область",
    "купить торговое помещение в спб",
    "офис в аренду без посредников",
    # Вопросительные — «сколько стоит / как»
    "сколько стоит арендовать офис в москве",
    "как купить коммерческую недвижимость",
    "сколько стоит аренда склада в подмосковье",
    "как оформить аренду коммерческой недвижимости",
    # С регионом
    "коммерческая недвижимость казань 2024",
    "аренда офиса екатеринбург от собственника",
    "торговые помещения краснодар продажа",
    "офисы новосибирск аренда недорого",
    "коммерческая недвижимость ростов-на-дону",
    "склад аренда самара от собственника",
    # С характеристиками
    "офис 200 м2 аренда москва центр",
    "торговое помещение 100 квм первый этаж",
    "склад 1000 кв м ответственное хранение",
    # Сделочные / информационные
    "договор аренды коммерческой недвижимости образец",
    "налог при продаже коммерческой недвижимости",
    "оценка коммерческой недвижимости онлайн",
]


def _pick_queries(num: int) -> list[str]:
    """
    F4: формирует список запросов для Yandex warmup с распределением:
      70% — тематические (THEMATIC_QUERIES, коммерческая недвижимость)
      25% — общие (YANDEX_QUERIES, чтобы не выглядеть «маньяком одной темы»)
       5% — пропуск (аккаунт открыл Yandex, но ничего не искал)

    num — сколько запросов хотим, возвращаем список фактически выбранных.
    Может быть короче num, если часть позиций попала в «пропуск».
    """
    pool = []
    for _ in range(num):
        r = random.random()
        if r < 0.70:
            pool.append(random.choice(THEMATIC_QUERIES))
        elif r < 0.95:
            pool.append(random.choice(YANDEX_QUERIES))
        # else: 5% — пропуск (idle warmup, просто открыли Яндекс)
    return pool


def update_profile_proxy(adspower_api, user_id, proxy_str):
    """L11: тонкая обёртка над `AdsPowerAPI.update_proxy` (back-compat).

    Раньше функция сама лезла в `adspower_api.base` / `adspower_api.api_key`,
    дублируя URL-сборку. Теперь это инкапсулировано в методе. Wrapper
    оставлен для обратной совместимости с существующими тестами
    (`tests/test_proxy.py`), которые мокают `bot.update_profile_proxy`.
    """
    return adspower_api.update_proxy(user_id, proxy_str)


def _mask_proxy(proxy_str: str) -> str:
    """H4-fix: маскирует credentials в proxy строке для безопасного логирования.

    Форматы: host:port, host:port:user:pass, user:pass@host:port, [::1]:8080
    Возвращает только host:port.
    """
    if not proxy_str:
        return "<empty>"
    # user:pass@host:port — берём часть после @
    if "@" in proxy_str:
        return _mask_proxy(proxy_str.split("@", 1)[1])
    # IPv6: [::1]:8080 или [::1]:8080:user:pass
    if proxy_str.startswith("["):
        bracket_end = proxy_str.index("]")
        host = proxy_str[: bracket_end + 1]  # [::1]
        rest = proxy_str[bracket_end + 2 :].split(":")  # skip ]:
        port = rest[0] if rest else ""
        return f"{host}:{port}" if port else host
    parts = proxy_str.split(":")
    if len(parts) >= 4:
        # host:port:user:pass
        return f"{parts[0]}:{parts[1]}"
    # host:port (уже безопасно)
    return proxy_str


# F8: idle cycles — иногда только messenger или только профиль.
# Default-распределение типов цикла. Реальный пользователь открывает Авито
# с разными целями: проверить мессенджер, полистать главную, просто зайти
# в профиль. Бот не должен ВСЕГДА делать «полный цикл» (browse + parse +
# messenger) — это сильнейший behavioral fingerprint.
_CYCLE_KINDS_DEFAULT: dict[str, float] = {
    "full": 0.60,  # warmup → browse → find_and_view → messenger
    "messenger_only": 0.10,  # только мессенджер (reactive replies)
    "browse_only": 0.15,  # browse + find_and_view, без messenger
    "profile_check": 0.05,  # просто заходим в /profile
    "outbound_only": 0.10,
}

# Распределение для warmup-режима (B1): аккаунт пока не должен слать
# сообщений (messenger_only=0) и стараемся НЕ парсить много — больше
# спокойных profile_check / browse_only. outbound тоже выключен.
_CYCLE_KINDS_WARMUP: dict[str, float] = {
    "full": 0.20,
    "messenger_only": 0.0,
    "browse_only": 0.40,
    "profile_check": 0.40,
    "outbound_only": 0.0,  # H1: в warmup-режим outbound заблокирован
}


def _pick_cycle_kind(
    account: dict,
    cfg: dict,
    *,
    is_warmup: bool = False,
    outbound_disabled: bool = False,
) -> str:
    """F8: выбирает тип сегодняшнего цикла.

    Чистая функция (без обращения к account_state) — для лёгкой
    тестируемости. Флаги `is_warmup` / `outbound_disabled` пробрасываются
    из run_thread, который сам опрашивает account_state.is_in_warmup() /
    account_state.is_outbound_disabled().

    Override для НЕ-warmup-режима:
        1. account["cycle_distribution"]   (per-account, accounts.json)
        2. cfg["cycle_distribution"]       (глобальный, config.json)
        3. _CYCLE_KINDS_DEFAULT            (хард-кодед)

    T17: если `outbound_disabled=True` — вес outbound_only зануляется,
    его «масса» перераспределяется на остальные kinds пропорционально.
    Так бот после Avito-капчи 24h НЕ пишет первым.

    Returns:
        Один из ключей словаря весов: "full" / "messenger_only" /
        "browse_only" / "profile_check" / "outbound_only".
    """
    if is_warmup:
        weights = _CYCLE_KINDS_WARMUP
    else:
        weights = (
            account.get("cycle_distribution")
            or cfg.get("cycle_distribution")
            or _CYCLE_KINDS_DEFAULT
        )
    # T17: после Avito-капчи outbound запрещён на 24h.
    if outbound_disabled and weights.get("outbound_only", 0):
        weights = {k: (0.0 if k == "outbound_only" else float(v)) for k, v in weights.items()}
    kinds = list(weights.keys())
    probs = [float(weights[k]) for k in kinds]
    # Guard: NaN/inf/отрицательные — недопустимые веса, зануляем
    probs = [p if p > 0 and p == p else 0.0 for p in probs]  # p==p исключает NaN
    # Guard: если все веса нулевые (кривой config) — fallback на "full".
    if sum(probs) <= 0:
        return "full"
    # random.choices гарантирует выбор хотя бы одного, если суммa > 0.
    # _CYCLE_KINDS_WARMUP содержит 0 для messenger_only — это нормально.
    return random.choices(kinds, weights=probs, k=1)[0]


def _do_profile_check(driver, account_name: str) -> None:
    """F8: «короткий цикл» — заходим в профиль, листаем 30-60 секунд.

    Не парсим, не отвечаем — просто визит. Имитирует «зашёл проверить
    своё». В 30% случаев заглядываем ещё и в /profile/favorites.
    """
    stop_ev = _tg.get_account_stop_event(account_name)
    safe_get(driver, "https://www.avito.ru/profile", account_name)
    hp(30, 60, stop_event=stop_ev)
    if random.random() < 0.3:
        safe_get(driver, "https://www.avito.ru/profile/favorites", account_name)
        hp(20, 40)


# T2: актуальные селекторы результатов Yandex SERP. Список из нескольких
# вариантов на случай smooth-rollouts разметки (Yandex иногда раскатывает
# A/B по разным регионам/устройствам). Достаточно совпадения ОДНОГО из них.
_YANDEX_RESULT_SELECTORS = [
    "li.serp-item",  # классический контейнер органики
    "[data-fast-name='organic']",  # новый data-атрибут для блоков выдачи
    "[data-fast-tag*='organic']",
    ".OrganicTitle",  # титлы органических результатов
    ".OrganicTitle-LinkText",
    ".organic__url-text",  # домены под заголовком
    "ul.serp-list > li",
    "[role='main'] [role='listitem']",
]

_YANDEX_REGION_MOSCOW = 213


def _yandex_search_url(query: str, region_id: int = _YANDEX_REGION_MOSCOW) -> str:
    """T2: формирует прямой URL поисковой выдачи Yandex (минуя homepage).

    homepage ya.ru на коммерческих прокси часто отдаёт капчу ДО ввода запроса.
    Direct-search (`/search/?text=...&lr=...`) на 100% обходит этот шаг.
    Параметр lr=213 — Москва, что согласуется с типовыми RU-прокси.
    """
    from urllib.parse import quote_plus

    return f"https://yandex.ru/search/?text={quote_plus(query)}&lr={region_id}"


def _is_yandex_captcha(driver) -> bool:
    """T2: рано детектим капчу Yandex (URL/title) до того, как _wait_yandex_results
    выбросит TimeoutException через 15s.

    Распространённые маркеры:
    - URL содержит `/showcaptcha` или `captcha=` (SmartCaptcha)
    - title содержит «Ой!» / «captcha» / «Подтвердите»
    """
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""
    if "/showcaptcha" in url or "captcha=" in url:
        return True
    try:
        title = (driver.title or "").lower()
    except Exception:
        title = ""
    if any(m in title for m in ("ой!", "captcha", "подтвердите")):
        return True
    return False


def _wait_yandex_results(driver, timeout: int = 15) -> bool:
    """T2: ждёт появления хотя бы одного элемента из _YANDEX_RESULT_SELECTORS.

    Возвращает True если результаты появились, False — иначе (не бросает
    TimeoutException — yandex_warmup сам решит, считать ли это провалом).
    """
    selector = ", ".join(_YANDEX_RESULT_SELECTORS)
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
        return True
    except TimeoutException:
        return False


def yandex_warmup(driver, wait, account_name, num_queries=2):
    """T2: direct-search вместо homepage-flow.

    Раньше: ya.ru → найти search box → typing-loop → submit → ждать SERP.
    Проблемы:
      1. ya.ru на коммерческих прокси отдаёт /showcaptcha сразу.
      2. Селекторы `serp-item`/`organic` устарели — Yandex поменял разметку.

    Теперь: GET yandex.ru/search/?text=...&lr=213 → детект капчи →
    список актуальных селекторов → scroll. Поведение проще, надёжнее,
    обходит homepage-captcha.

    Параметр `wait` оставлен для обратной совместимости (используется
    извне через `client.warmup_yandex`).
    """
    _ = wait  # обратная совместимость API

    log(account_name, "=== Enhanced Thematic Yandex Warmup (direct-search) ===")

    # F4: смешиваем тематические (70%), общие (25%) и пропуски (5%).
    queries = _pick_queries(num_queries)

    # F4: если queries пустой (все слоты попали в 5%-пропуск) — это нормально:
    # аккаунт просто «не искал ничего» в этом цикле. Не считается провалом.
    if not queries:
        log(account_name, "Warmup completed (idle — no queries this time).")
        return True

    success_count = 0
    captcha_count = 0

    for q_idx, query in enumerate(queries, 1):
        log(account_name, f"  Query {q_idx}: «{query}»")

        try:
            url = _yandex_search_url(query)
            if not safe_get(driver, url, account_name):
                log(account_name, "    safe_get failed (block/empty/timeout).")
                continue

            # T2: рано детектим капчу (ещё до wait_yandex_results timeout 15s).
            if _is_yandex_captcha(driver):
                # Пробуем решить капчу бесплатно (checkbox click)
                from captcha_solver import solve_yandex_smartcaptcha

                solved = solve_yandex_smartcaptcha(driver, account_name, log_func=log)
                if solved:
                    log(account_name, "    Captcha solved! Retrying query...")
                    # После решения капчи — повторяем навигацию
                    if not safe_get(driver, url, account_name):
                        continue
                    if _is_yandex_captcha(driver):
                        captcha_count += 1
                        log(account_name, "    Captcha returned after solve — пропускаю.")
                        continue
                else:
                    captcha_count += 1
                    log(account_name, "    Captcha solve failed — пропускаю query.")
                    continue

            if not _wait_yandex_results(driver, timeout=15):
                # На SERP не появились результаты — либо разметка опять
                # поменялась, либо это была капча (детект мог не сработать
                # на новых маркерах). Перепроверяем капчу ещё раз.
                if _is_yandex_captcha(driver):
                    from captcha_solver import solve_yandex_smartcaptcha

                    solved = solve_yandex_smartcaptcha(driver, account_name, log_func=log)
                    if solved:
                        log(account_name, "    Captcha (post-wait) solved! Retrying...")
                        if safe_get(driver, url, account_name) and _wait_yandex_results(
                            driver, timeout=15
                        ):
                            success_count += 1
                            hp(5, 10)
                            human_scroll(driver, "down", iters=random.randint(2, 4))
                        else:
                            captcha_count += 1
                    else:
                        captcha_count += 1
                        log(account_name, "    Captcha (post-wait) solve failed — пропускаю.")
                else:
                    log(account_name, "    SERP results not found (markup changed?).")
                continue

            success_count += 1

            # Behavioural: scroll по результатам, как живой пользователь.
            hp(5, 10)
            human_scroll(driver, "down", iters=random.randint(2, 4))

        except Exception as e:
            # Расширенный лог: тип, текст (до 200), title и url Chrome — нужно
            # чтобы отличать капчу/блок/изменение DOM/проблему сети друг от друга.
            try:
                page_title = driver.title[:80]
            except Exception:
                page_title = "<no title>"
            try:
                page_url = driver.current_url[:120]
            except Exception:
                page_url = "<no url>"
            log(
                account_name,
                f"    Query failed: {type(e).__name__}: {str(e)[:200]} "
                f"| url={page_url} | title={page_title}",
            )

    if success_count == 0:
        if captcha_count > 0:
            log(account_name, f"  Warmup FAILED: все {captcha_count} запросов → captcha.")
        else:
            log(account_name, "  Warmup FAILED: No successful queries.")
        return False

    log(
        account_name,
        f"Warmup completed ({success_count} queries successful, {captcha_count} captcha).",
    )
    return True


def browse_commercial_categories(
    driver,
    wait,
    account_name,
    num_categories=None,
    ads_per_category=None,
    search_filters=None,
    favorite_rate=0.08,
    call_rate=None,
    message_rate=0.05,
    max_categories_per_browse=4,
    max_listings_per_browse=4,
    db_manager=None,
    llm_classifier=None,
    account=None,
):
    """
    F2: num_categories и ads_per_category по умолчанию None — тогда используется
    _weighted_listing_count() для случайного числа в диапазоне [1, max_*].
    Явная передача числа переопределяет случайное поведение (для тестов).
    """
    # F2: случайное число категорий и листингов за категорию
    n_cats = (
        num_categories
        if num_categories is not None
        else _weighted_listing_count(max_n=max_categories_per_browse)
    )
    n_ads = (
        ads_per_category
        if ads_per_category is not None
        else _weighted_listing_count(max_n=max_listings_per_browse)
    )
    log(account_name, "=== Browsing commercial real estate categories on Avito ===")
    log(account_name, f"  F2: категорий={n_cats}, листингов/кат={n_ads}")
    chosen = random.sample(
        AVITO_COMMERCIAL_CATEGORIES, min(n_cats, len(AVITO_COMMERCIAL_CATEGORIES))
    )

    # E2: per-account city filter — если задано, вставляем в URL.
    _sf = search_filters or {}
    _cities = _sf.get("cities")
    _city_prefix = ""
    # При browse по городу Avito требует тип сделки в URL, иначе 404.
    _deal_suffix = ""
    _deal_type = "rent"
    _pmin = None
    if _cities:
        _city_prefix = "/" + random.choice(_cities)
        # Поддержка deal_types (список) и deal_type (строка).
        _deal_types_list = _sf.get("deal_types") or (
            [_sf["deal_type"]] if _sf.get("deal_type") else ["rent", "sale"]
        )
        _deal_type = random.choice(_deal_types_list)
        _deal_suffix = (
            "/sdam-ASgBAgICAUSwCMpB" if _deal_type == "rent" else "/prodam-ASgBAgICAUSwCMpB"
        )
        # Минимальная цена из конфига search_filters или COMMERCIAL_SEARCH_FILTERS.
        _pmin = _sf.get("price_min")
        if _pmin is None:
            from commercial_realestate_config import COMMERCIAL_SEARCH_FILTERS

            _pmin = COMMERCIAL_SEARCH_FILTERS.get(_deal_type, {}).get("min_price")
        _pmax = _sf.get("price_max")

    for cat in chosen:
        url = f"https://www.avito.ru{_city_prefix}{cat}{_deal_suffix}"
        if _pmin:
            url += f"?pmin={_pmin}"
            if _pmax:
                url += f"&pmax={_pmax}"
        log(account_name, f"  Category: {cat} (deal={_deal_type}, pmin={_pmin})")
        if not safe_get(driver, url, account_name):
            continue

        random_mouse_move(driver)
        human_scroll(driver, "down", iters=random.randint(2, 4))

        links = driver.find_elements(By.XPATH, "//a[@data-marker='item-title']")
        if not links:
            links = driver.find_elements(By.CSS_SELECTOR, "a[data-marker='item-title']")

        if not links:
            continue

        # Фильтр: только коммерческая недвижимость. Avito подмешивает квартиры,
        # дома и участки через localPriority (рекомендации). Фильтруем ДО
        # сэмплирования, чтобы random.sample всегда выбирал из релевантных.
        commercial_links = [
            lnk
            for lnk in links
            if "/kommercheskaya_nedvizhimost/" in (lnk.get_attribute("href") or "")
        ]
        non_commercial_count = len(links) - len(commercial_links)
        if non_commercial_count:
            log(
                account_name,
                f"    Отфильтровано {non_commercial_count} некоммерческих (рекомендации). "
                f"Осталось {len(commercial_links)} коммерческих.",
            )
        if not commercial_links:
            continue

        # D6: фильтруем уже просмотренные листинги.
        if db_manager is not None:
            before_dedup = len(commercial_links)
            commercial_links = [
                lnk
                for lnk in commercial_links
                if not db_manager.is_listing_url_seen(
                    normalize_listing_url(lnk.get_attribute("href") or "")
                )
            ]
            dup_count = before_dedup - len(commercial_links)
            if dup_count:
                log(
                    account_name,
                    f"    D6: {dup_count} уже просмотренных — пропускаем. Новых: {len(commercial_links)}",
                )
        if not commercial_links:
            continue

        hrefs = [
            h
            for lnk in random.sample(commercial_links, min(n_ads, len(commercial_links)))
            if (h := lnk.get_attribute("href"))
        ]

        for i, href in enumerate(hrefs, 1):
            log(account_name, f"    Listing {i}/{len(hrefs)}")
            if not safe_get(driver, href, account_name):
                break
            if not view_listing(
                driver,
                wait,
                account_name,
                favorite_rate=favorite_rate,
                call_rate=call_rate,
                message_rate=message_rate,
                db_manager=db_manager,
                llm_classifier=llm_classifier,
                account=account,
            ):
                break
            driver.back()
            hp(2, 4)


def _process_listing(driver, wait, account_name, log, db_manager, llm_classifier, account, is_new):
    """Общая логика обработки листинга: extract → save → write to owner.
    Возвращает listing_data (может быть None если extract упал).
    """
    listing_data = extract_listing_data(driver, wait, account_name, log)
    save_listing_to_db(listing_data, db_manager, log, account_name)
    # Пишем собственнику (outbound-канал).
    _try_write_to_owner(
        driver,
        account_name,
        llm_classifier=llm_classifier,
        db_manager=db_manager,
        account=account,
    )
    return listing_data


def find_and_view_commercial_listings(
    driver,
    wait,
    account_name,
    db_manager,
    search_filters=None,
    max_listings_per_search=7,
    llm_classifier=None,
    account=None,
):
    """Search for commercial real estate listings in million-plus cities with price filters.

    F2: max_listings_per_search — верхняя граница весового распределения
    числа листингов за один вызов. Default 7.
    """
    log(account_name, "=== Commercial real estate search (Million Cities + Price Filters) ===")

    # A3: если аккаунт недавно попал на капчу — не дёргаем Avito.
    if account_state.is_cooled_down(account_name):
        remaining = account_state.cooldown_remaining_seconds(account_name)
        log(account_name, f"!! Skip search: аккаунт в captcha-cooldown ещё {remaining}s")
        return 0, 0, 0

    processed_count = 0
    new_listings_count = 0
    error_count = 0

    # E2: per-account search_filters переопределяют глобальные константы.
    _sf = search_filters or {}

    # Тип сделки: "deal_type" (строка) или "deal_types" (список); иначе random.
    _deal_types = _sf.get("deal_types") or (
        [_sf["deal_type"]] if _sf.get("deal_type") else ["sale", "rent"]
    )
    deal_type = random.choice(_deal_types)

    config = COMMERCIAL_SEARCH_FILTERS[deal_type]

    # Города: per-account список или глобальный MILLION_CITIES.
    _cities = _sf.get("cities") or MILLION_CITIES
    city = random.choice(_cities)

    category_path = random.choice(config["paths"])

    # Цена: per-account переопределяет config["min_price"]; pmax — опционально.
    min_price = _sf.get("price_min") if _sf.get("price_min") is not None else config["min_price"]
    max_price = _sf.get("price_max")

    url = f"https://www.avito.ru/{city}{category_path}?pmin={min_price}"
    if max_price is not None:
        url += f"&pmax={max_price}"

    log(account_name, f"  Searching in {city} for {deal_type} (min {min_price} RUB)")
    log(account_name, f"  URL: {url}")

    if not safe_get(driver, url, account_name):
        return 0, 0, 1

    human_scroll(driver, "down", iters=random.randint(3, 6))

    links = driver.find_elements(By.XPATH, "//a[@data-marker='item-title']")
    if not links:
        log(account_name, f"  No listings found in {city} for this category.")
        return 0, 0, 0

    # Фильтр: только коммерческая недвижимость. Avito подмешивает квартиры,
    # дома и участки через localPriority (рекомендации) — убираем всё
    # кроме kommercheskaya_nedvizhimost. Фильтруем ДО сэмплирования,
    # чтобы random.sample всегда выбирал из релевантных объявлений.
    commercial_links = [
        lnk for lnk in links if "/kommercheskaya_nedvizhimost/" in (lnk.get_attribute("href") or "")
    ]
    non_commercial_count = len(links) - len(commercial_links)
    if non_commercial_count:
        log(
            account_name,
            f"  Отфильтровано {non_commercial_count} некоммерческих листингов "
            f"(квартиры/гаражи/рекомендации). Осталось {len(commercial_links)} коммерческих.",
        )
    if not commercial_links:
        log(account_name, f"  Нет коммерческих листингов в выдаче {city}.")
        return 0, 0, 0

    # D6: фильтруем уже просмотренные листинги — не тратим Selenium-транзит
    # и бюджет на повторные заходы. Сравниваем по нормализованному URL
    # (без query/fragment), как в is_new_listing.
    if db_manager is not None:
        before_dedup = len(commercial_links)
        commercial_links = [
            lnk
            for lnk in commercial_links
            if not db_manager.is_listing_url_seen(
                normalize_listing_url(lnk.get_attribute("href") or "")
            )
        ]
        dup_count = before_dedup - len(commercial_links)
        if dup_count:
            log(
                account_name,
                f"  D6: {dup_count} уже просмотренных — пропускаем. Новых: {len(commercial_links)}",
            )
    if not commercial_links:
        log(account_name, f"  Все коммерческие листинги уже просмотрены в {city}.")
        return 0, 0, 0

    # F2: случайное число листингов [1, max_listings_per_search] с весами.
    _n = _weighted_listing_count(max_n=max_listings_per_search)
    _n = min(_n, len(commercial_links))
    log(account_name, f"  F2: листингов в этом запросе={_n}")
    # T11: сохраняем только href — element'ы станут stale после первой навигации.
    # Ctrl+Click path (30%) будет re-find'ить элемент по href перед кликом.
    sampled = random.sample(commercial_links, _n)
    targets = [lnk.get_attribute("href") for lnk in sampled if lnk.get_attribute("href")]

    for i, href in enumerate(targets, 1):
        if _tg.is_stop_requested(account_name):
            break
        # A3: если внутри предыдущей итерации словили капчу — выходим.
        if account_state.is_cooled_down(account_name):
            remaining = account_state.cooldown_remaining_seconds(account_name)
            log(
                account_name,
                f"  !! Captcha cooldown активен ({remaining}s) — стоп после текущего листинга",
            )
            break
        try:
            log(account_name, f"  Processing listing {i}/{len(targets)}: {href}")

            # C3-bridge + D4: считаем новым только если в БД его действительно
            # ещё нет, причём сравниваем по нормализованному URL (без query/utm).
            is_new = db_manager.is_new_listing(normalize_listing_url(href))

            # T11: ~30% — Ctrl+Click → новая вкладка → читаем → закрываем.
            # 70% — обычная навигация safe_get + driver.back. Это даёт
            # естественное распределение «открыл в новой вкладке vs тут же».
            use_new_tab = random.random() < 0.30
            opened_in_tab = False
            if use_new_tab:
                # Re-find element by href — previous references are stale after navigation.
                # XPath: если href содержит и ' и ", простой escaping невозможен —
                # используем concat() для надёжной сборки строки.
                if "'" not in href:
                    xpath = f"//a[@data-marker='item-title'][@href='{href}']"
                elif '"' not in href:
                    xpath = f'//a[@data-marker="item-title"][@href="{href}"]'
                else:
                    # Оба типа кавычек — собираем через concat()
                    parts = href.split("'")
                    concat_parts = ",".join(f"'{p}'" for p in parts)
                    concat_expr = f"concat({concat_parts})"
                    xpath = f"//a[@data-marker='item-title'][@href={concat_expr}]"
                link_elems = driver.find_elements(By.XPATH, xpath)
                link_elem = link_elems[0] if link_elems else None
                if link_elem is None:
                    use_new_tab = False  # fallback to safe_get
                else:
                    with _new_tab_for_listing(
                        driver, link_elem, stop_event=_tg.get_account_stop_event(account_name)
                    ) as ok:
                        if ok:
                            opened_in_tab = True
                            log(account_name, "  T11: открыли в новой вкладке (Ctrl+Click)")
                            ldata = _process_listing(
                                driver,
                                wait,
                                account_name,
                                log,
                                db_manager,
                                llm_classifier,
                                account,
                                is_new,
                            )
                            processed_count += 1
                            if ldata and is_new:
                                new_listings_count += 1
                    # context exit: вкладка закрыта, мы снова на search-page.
                    hp(1.5, 3.5)

            if not opened_in_tab:
                # Fallback (или 70% базовый flow): safe_get + driver.back.
                if not safe_get(driver, href, account_name):
                    break

                ldata = _process_listing(
                    driver,
                    wait,
                    account_name,
                    log,
                    db_manager,
                    llm_classifier,
                    account,
                    is_new,
                )

                processed_count += 1
                if ldata and is_new:
                    new_listings_count += 1

                driver.back()
                hp(1.5, 3.5)
        except Exception as e:
            error_count += 1
            log(account_name, f"  Error: {str(e)[:50]}")

    return processed_count, new_listings_count, error_count


def _is_logged_in_url(driver) -> bool:
    """True, если по текущему URL не похоже, что мы всё ещё на login-форме."""
    try:
        url = driver.current_url or ""
    except Exception:
        return False
    return "login" not in url.lower()


def is_session_authenticated(driver, account_name: str) -> bool:
    """
    B4: проверяет, есть ли в текущем профиле AdsPower живая Avito-сессия.

    Логика:
        1. Идём на https://www.avito.ru/profile.
        2. Если Avito редиректит на /login → не залогинены.
        3. Если URL остался /profile (или /profile/...) и есть marker
           header/user-menu — залогинены.

    Эта проверка устойчивее, чем ловить только селектор header/messenger:
    Avito может поменять селектор, но логика «непустая сессия = /profile
    не редиректит» сохраняется.
    """
    try:
        driver.get("https://www.avito.ru/profile")
    except Exception as e:
        log(account_name, f"  is_session_authenticated: get(/profile) failed: {e}")
        return False

    hp(2.5, 4.5)
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""

    # Если редирект — точно не залогинены.
    if "login" in url:
        log(account_name, "  Session check: redirected to /login")
        return False

    # Не редирект — попытаемся подтвердить markers (но это soft-check).
    try:
        WebDriverWait(driver, 4).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//a[@data-marker='header/messenger'] | "
                    "//div[@data-marker='header/user-menu'] | "
                    "//*[@data-marker='profile/avatar']",
                )
            )
        )
        log(account_name, "  Session check: authenticated (markers + /profile OK)")
        return True
    except Exception:
        # Markers не нашлись, но и редиректа на login не было — Avito мог
        # отрендерить лёгкую версию страницы. Считаем session valid.
        log(account_name, "  Session check: /profile OK, markers absent — assume authenticated")
        return True


def _wait_user_resume_for_login(
    account_name: str, kind: str, prompt: str, timeout_seconds: float = 600.0
) -> str:
    """
    B1: уведомляет TG-админа и блокирует поток до его ответа (или таймаута).

    Returns: "continue" | "cancel" | "timeout".
    """
    from account_state import account_state as _astate

    req = _astate.create_user_resume_request(account_name, kind, prompt)
    delivered = _tg.send_user_action_request(account_name, req.request_id, prompt)
    if not delivered:
        # TG не настроен — без интерактива пытаться нет смысла, аккаунт встаёт.
        log(account_name, "  TG-controller недоступен — login interactive невозможен")
        return "cancel"

    log(account_name, f"  Жду ответа админа в TG (до {int(timeout_seconds)}s)...")
    response = _astate.wait_user_resume(account_name, req.request_id, timeout=timeout_seconds)
    if response is None:
        log(account_name, "  Таймаут ожидания ответа админа.")
        return "timeout"
    log(account_name, f"  Ответ админа: {response}")
    return response


def perform_login(driver, wait, account_name, phone, password):
    """
    Performs manual login on Avito using phone and password.

    B1: при детекте SMS-формы или капчи — НЕ ретраим в цикле, а отправляем
    запрос админу в TG ("введи код / реши капчу и нажми Продолжить") и ждём.
    После ответа админа проверяем, что мы прошли login (URL ушёл с /login).
    """
    # Импорты лежат внутри функции, чтобы избежать циклической загрузки
    # (captcha_detect не должен импортироваться в module-load time bot.py).
    from captcha_detect import detect_captcha, detect_sms_form

    log(
        account_name,
        f"=== Manual Login Attempt for {phone[:3]}****{phone[-2:] if len(phone) > 5 else ''} ===",
    )
    try:
        # Navigate to login page
        driver.get("https://www.avito.ru/#login?next=%2F")
        hp(4, 7)

        # 1. Enter phone
        log(account_name, "  Entering phone number (slow mode)...")
        phone_input = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@name='login']"))
        )
        # T6: human-like focus вместо «click без mousemove».
        _human_click(driver, phone_input, stop_event=_tg.get_account_stop_event(account_name))
        hp(0.8, 1.5)

        phone_input.send_keys(Keys.CONTROL + "a")
        phone_input.send_keys(Keys.DELETE)
        hp(0.5, 1.0)

        # Логин-инпуты обычно вводят аккуратнее — чуть медленнее (~100-350ms/char).
        # T5: enable_typos=False — backspace может ломать валидаторы phone-input'а.
        human_type(phone_input, phone, speed_range=(0.10, 0.35), enable_typos=False)
        hp(1.5, 2.5)

        # 2. Submit phone (на этом шаге Avito может попросить SMS/captcha
        # ещё ДО ввода пароля)
        try:
            submit_btn = driver.find_element(By.XPATH, "//button[@data-marker='login-form/submit']")
            move_click(driver, submit_btn)
            hp(3, 5)
        except Exception:
            pass

        # B1: после первого submit — проверяем не появилась ли SMS/captcha.
        if detect_captcha(driver, log_func=log, account_name=account_name):
            resp = _wait_user_resume_for_login(
                account_name,
                "login_captcha",
                "Avito показал капчу на этапе ввода телефона. Реши её в браузере "
                "(AdsPower), затем нажми «Продолжить».",
            )
            if resp != "continue":
                return False
            hp(2, 4)
            if not _is_logged_in_url(driver):
                # После капчи Avito может либо пустить дальше, либо запросить SMS — продолжаем flow ниже.
                pass

        if detect_sms_form(driver, log_func=log, account_name=account_name):
            resp = _wait_user_resume_for_login(
                account_name,
                "login_sms",
                "Avito прислал SMS-код после телефона. Введи код в браузере "
                "(AdsPower), нажми submit и затем «Продолжить».",
            )
            if resp != "continue":
                return False
            hp(2, 4)
            # Если уже залогинились — выходим успехом.
            if _is_logged_in_url(driver):
                log(account_name, "  Logged in via SMS step (no password needed).")
                return True

        # 3. Enter password (если поле появилось)
        try:
            password_input = wait.until(
                EC.presence_of_element_located((By.XPATH, "//input[@name='password']"))
            )
        except TimeoutException:
            # Иногда после успешного SMS пароль не запрашивают вовсе.
            if _is_logged_in_url(driver):
                log(account_name, "  Logged in без пароля (SMS-only flow).")
                return True
            log(account_name, "  Password field not found — login flow stuck.")
            return False

        log(account_name, "  Entering password (slow mode)...")
        # T6: human-like focus вместо «click без mousemove».
        _human_click(driver, password_input, stop_event=_tg.get_account_stop_event(account_name))
        hp(0.8, 1.5)
        # Пароль вводят чуть медленнее обычного текста (~100-350ms/char).
        # T5: enable_typos=False — для пароля лишний BACKSPACE рискован
        # (форма может тригнуть валидацию / показать «неверный пароль»).
        human_type(password_input, password, speed_range=(0.10, 0.35), enable_typos=False)
        hp(1.5, 2.5)

        # 4. Final submit
        submit_btn = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//button[@data-marker='login-form/submit']"))
        )
        move_click(driver, submit_btn)
        log(account_name, "  Login form submitted. Waiting for results...")
        hp(8, 12)

        # B1: post-submit — проверяем SMS/captcha повторно.
        if detect_captcha(driver, log_func=log, account_name=account_name):
            resp = _wait_user_resume_for_login(
                account_name,
                "login_captcha",
                "Avito показал капчу после ввода пароля. Реши её и нажми «Продолжить».",
            )
            if resp != "continue":
                return False
            hp(2, 4)

        if detect_sms_form(driver, log_func=log, account_name=account_name):
            resp = _wait_user_resume_for_login(
                account_name,
                "login_sms",
                "Avito прислал SMS-код после ввода пароля. Введи код, submit и нажми «Продолжить».",
            )
            if resp != "continue":
                return False
            hp(2, 4)

        # Финальная проверка login.
        if _is_logged_in_url(driver):
            log(account_name, "  Successfully bypassed login page.")
            return True
        log(account_name, "  Still on login page after flow — login failed.")
        return False

    except Exception as e:
        # Маскируем credentials: телефон/пароль могут быть в str(e)
        err_msg = str(e)[:200]
        err_msg = err_msg.replace(phone or "", "<phone>") if phone else err_msg
        err_msg = err_msg.replace(password or "", "<password>") if password else err_msg
        log(account_name, f"  Manual login failed: {err_msg}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Thread logic
# ══════════════════════════════════════════════════════════════════════════════


def get_random_proxy():
    """Reads proxies.txt and returns a list of proxy strings."""
    try:
        # Сначала ищем рядом с bot.py (работает при запуске из любого каталога).
        path = Path(__file__).resolve().parent / "proxies.txt"
        if not path.exists():
            # Fallback: CWD (для тестов и legacy-запуска).
            path = Path("proxies.txt")

        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            proxies = [line.strip() for line in f if line.strip()]
        return random.choice(proxies) if proxies else None
    except OSError:
        # L2: bare except → OSError (PermissionError, IsADirectoryError и т.п.).
        return None


def _load_proxies_pool() -> list[str]:
    """T18: читает proxies.txt и возвращает список (без рандомизации).

    Используется в `_apply_account_proxy` для probe-rotation: сначала
    шаффлим, потом перебираем кандидатов через probe. Сохранили также
    `get_random_proxy` (backward-compat для legacy-пути / тестов).
    """
    try:
        path = Path(__file__).resolve().parent / "proxies.txt"
        if not path.exists():
            # Fallback: CWD (для тестов и legacy-запуска).
            path = Path("proxies.txt")
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    except OSError:
        return []


def _apply_account_proxy(
    adspower: "AdsPowerAPI",
    user_id: str,
    account: dict,
    account_name: str,
    cfg: dict | None = None,
) -> str | None:
    """
    A1 + T18: Выбирает и применяет прокси для AdsPower-профиля.

    Порядок:
      1. Поле "proxy" из accounts.json (per-account, приоритет).
      2. Случайный прокси из proxies.txt (глобальный fallback).
      3. Если ничего нет — спрашивает админа в TG: продолжить без прокси?
         Ответ "Продолжить" → возвращает "__no_proxy__", "Отмена" → None.

    T18 (если `cfg.proxy_probe_enabled=True`, default): перед применением
    через AdsPower делается HTTP-probe (api.ipify.org через прокси) с
    auto-rotation — мёртвые/чужие прокси отбрасываются и пробуем следующий.
    Это спасает от запуска профиля через нерабочий прокси и от длинных
    AdsPower-таймаутов.

    Args:
        cfg: глобальный config.json. Если None — probe выключен (legacy
            путь, используется в существующих тестах test_proxy.py).

    Returns:
        Строку прокси, который удалось применить, "__no_proxy__" если админ
        разрешил без прокси, или None.
    """
    # cfg=None — legacy-путь для back-compat (используется в тестах
    # test_proxy.py + в местах, которые ещё не передают cfg).
    # cfg=dict (даже пустой) → probe ON по дефолту.
    if cfg is not None and bool(cfg.get("proxy_probe_enabled", True)):
        result = _apply_account_proxy_with_probe(adspower, user_id, account, account_name, cfg)
        if result is not None:
            return result
        # probe-путь вернул None — все кандидаты провалились.
        # Падаем в TG-опрос ниже (no_proxy_result).
        return _ask_admin_no_proxy(account_name)

    # Legacy path (probe disabled): прежнее поведение для back-compat.
    account_proxy = account.get("proxy")

    if account_proxy:
        if update_profile_proxy(adspower, user_id, account_proxy):
            log(account_name, "A1: Per-account proxy установлен")
            return account_proxy
        log(account_name, "A1: Не удалось установить per-account proxy — пробую proxies.txt")

    fallback = get_random_proxy()
    if fallback:
        if update_profile_proxy(adspower, user_id, fallback):
            log(account_name, "A1: Fallback proxy из proxies.txt установлен")
            return fallback
        log(account_name, "A1: Fallback proxy из proxies.txt не удалось установить")

    return _ask_admin_no_proxy(account_name)


def _ask_admin_no_proxy(account_name: str) -> str | None:
    """
    Спрашивает админа в TG: продолжить без прокси или остановить аккаунт.

    Returns "__no_proxy__" если админ нажал Продолжить, None если Отмена/таймаут.
    """
    _bot_logger.warning(
        "[%s] A1: Нет доступного прокси — ни per-account, ни в proxies.txt. "
        "Запрос админу в TG: продолжить без прокси?",
        account_name,
    )
    response = _wait_user_resume_for_login(
        account_name,
        "no_proxy_warning",
        (
            "У аккаунта нет прокси (ни в accounts.json, ни в proxies.txt).\n"
            "Без прокси аккаунт будет работать с IP сервера — риск блокировки "
            "выше обычного.\n\n"
            "Продолжить без прокси?"
        ),
        timeout_seconds=600.0,
    )
    if response == "continue":
        log(account_name, "A1: Админ разрешил продолжить БЕЗ прокси.")
        return "__no_proxy__"
    log(account_name, "A1: Аккаунт остановлен админом (нет прокси).")
    return None


def _apply_account_proxy_with_probe(
    adspower: "AdsPowerAPI",
    user_id: str,
    account: dict,
    account_name: str,
    cfg: dict,
) -> str | None:
    """T18: версия `_apply_account_proxy` с probe + auto-rotation.

    1. Собираем кандидатов: per-account proxy → шаффленный proxies.txt.
    2. `pick_healthy_proxy` пробует probe по очереди до `max_attempts`.
    3. Первый прошедший probe — применяем через AdsPower.
    4. Все провалились → ERROR + return None.
    """
    from proxy_health import pick_healthy_proxy

    timeout = float(cfg.get("proxy_probe_timeout_sec", 10.0))
    probe_url = cfg.get("proxy_probe_url", "https://api.ipify.org?format=json")
    expected_country = cfg.get("proxy_expected_country") or None
    max_attempts = int(cfg.get("proxy_max_probe_attempts", 5))
    scheme = cfg.get("proxy_probe_scheme", "socks5h")

    # Собираем кандидатов: per-account первым, потом proxies.txt.
    candidates: list[str] = []
    seen: set[str] = set()
    account_proxy = account.get("proxy")
    if account_proxy and account_proxy not in seen:
        candidates.append(account_proxy)
        seen.add(account_proxy)

    pool = _load_proxies_pool()
    random.shuffle(pool)
    for p in pool:
        if p not in seen:
            candidates.append(p)
            seen.add(p)

    if not candidates:
        return _ask_admin_no_proxy(account_name)

    chosen, result = pick_healthy_proxy(
        candidates,
        timeout=timeout,
        probe_url=probe_url,
        expected_country=expected_country,
        max_attempts=max_attempts,
        scheme=scheme,
        log_prefix=f"[{account_name}] A1: ",
        cache_get=account_state.get_cached_probe,
        cache_set=account_state.set_cached_probe,
    )

    if chosen is None:
        last_err = result.error if result else "no_candidates"
        _bot_logger.error(
            "[%s] A1: Все %d попыток probe прокси провалились. Последняя ошибка: %s.",
            account_name,
            min(max_attempts, len(candidates)),
            last_err,
        )
        return _ask_admin_no_proxy(account_name)

    if update_profile_proxy(adspower, user_id, chosen):
        log(
            account_name,
            f"A1: Healthy proxy applied (ip={result.ip}, "
            f"latency={result.latency_ms:.0f}ms, country={result.country or '?'})",
        )
        return chosen

    log(account_name, "A1: Probe прошёл, но AdsPower update_proxy failed")
    return None


# ── B2: Активное окно времени ─────────────────────────────────────────────


def _is_in_active_hours(account: dict, cfg: dict) -> bool:
    """
    B2: True если текущий час (local time) попадает в активное окно
    [active_hours_start, active_hours_end). Дефолт: 9..23.
    Per-account override имеет приоритет над глобальным из config.json.
    """
    start = int(account.get("active_hours_start", cfg.get("active_hours_start", 9)))
    end = int(account.get("active_hours_end", cfg.get("active_hours_end", 23)))
    hour = time.localtime().tm_hour
    # Overnight window: start > end (e.g. 22:00–06:00)
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def _seconds_until_active_hours(account: dict, cfg: dict) -> float:
    """
    B2: сколько секунд до начала активного окна.
    Если сейчас уже в окне — вернёт 0.
    """
    start = int(account.get("active_hours_start", cfg.get("active_hours_start", 9)))
    end = int(account.get("active_hours_end", cfg.get("active_hours_end", 23)))
    now = time.localtime()
    current_secs = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    target_secs = start * 3600
    # Overnight window: start > end (e.g. 22:00–06:00)
    if start > end:
        if current_secs >= target_secs or current_secs < end * 3600:
            return 0.0  # уже в окне
        # сейчас до start — ждём до start сегодня
        return float(target_secs - current_secs)
    if current_secs < target_secs:
        return float(target_secs - current_secs)
    # уже после start сегодня → ждём до завтра
    return float(86400 - current_secs + target_secs)


# F6: probabilistic active hours — заменяет бинарный B2 на вероятностную
# модель. Реальный пользователь:
#   утро (9-11)  ~95%, обед (12-14)  ~50%, день (15-17) ~85%,
#   вечер (18-21) ~90%, поздно (22-23) ~30%, ночь (0-8) ~5%.
# Бот, который ровно в 22:30 работает, а ровно в 23:01 спит — тоже паттерн.
# Теперь каждый цикл бросаем монетку с вероятностью _active_probability(hour);
# выпало > prob — пропускаем один цикл (длинная пауза 30-90 мин).
_ACTIVITY_BY_HOUR: dict[int, float] = {
    0: 0.02,
    1: 0.01,
    2: 0.01,
    3: 0.005,
    4: 0.005,
    5: 0.01,
    6: 0.05,
    7: 0.20,
    8: 0.40,
    9: 0.85,
    10: 0.95,
    11: 0.95,
    12: 0.55,
    13: 0.45,
    14: 0.55,
    15: 0.85,
    16: 0.85,
    17: 0.80,
    18: 0.90,
    19: 0.90,
    20: 0.85,
    21: 0.70,
    22: 0.45,
    23: 0.20,
}


def _active_probability(account: dict, cfg: dict, hour: int | None = None) -> float:
    """F6: вероятность того, что аккаунт «активен» в указанный час.

    Источники паттерна (по убыванию приоритета):
      1. account["activity_pattern"] — dict {hour: prob}, per-account.
      2. cfg["activity_pattern"]      — глобальный.
      3. _ACTIVITY_BY_HOUR            — default-распределение.

    Совместимость с B2: если у аккаунта/конфига заданы
    `active_hours_start`/`active_hours_end`, ВНЕ этого окна возвращаем 0
    (бот строго спит). Это для пользователей, которые хотят жёсткое 9-23
    без вероятностной модели — задают окно, и вероятностная модель
    превращается в бинарную.

    `hour` — для тестов; в проде передаём None и берём текущий локальный час.
    """
    if hour is None:
        hour = time.localtime().tm_hour

    # Совместимость с B2: жёсткое окно перебивает probabilistic-модель.
    start = account.get("active_hours_start", cfg.get("active_hours_start"))
    end = account.get("active_hours_end", cfg.get("active_hours_end"))
    if start is not None and end is not None:
        if not (int(start) <= hour < int(end)):
            return 0.0

    pattern = (
        account.get("activity_pattern")
        if account.get("activity_pattern") is not None
        else cfg.get("activity_pattern")
        if cfg.get("activity_pattern") is not None
        else _ACTIVITY_BY_HOUR
    )
    # Ключи в JSON всегда строки — нормализуем оба варианта.
    if isinstance(pattern, dict):
        return float(pattern.get(hour, pattern.get(str(hour), 0.5)))
    return 0.5


# ─────────────────────────────────────────────────────────────────────────────
# S1: run_thread декомпозиция. Раньше run_thread весил ~370 строк после
# F5/F6/F7/F8/F9 и был сложен для дальнейших правок. Сейчас он — оркестратор
# 50-60 строк, а основные секции вынесены в private helpers _ниже_.
# Все helpers идут в порядке вызова из run_thread.
# ─────────────────────────────────────────────────────────────────────────────


def _apply_per_account_overrides(account: dict) -> None:
    """G2/F7/A2: применяем per-account настройки из accounts.json.

    Объединяет три вида overrides: captcha-cooldown (G2), базовая вероятность
    dead-day (F7), дневные бюджеты на listings/messages/phone (A2). Значение
    None в accounts.json для любого ключа = «оставить глобальный дефолт».
    """
    account_name = account["name"]

    # G2: per-account override captcha_cooldown_minutes из accounts.json.
    account_state.set_account_cooldown_minutes(
        account_name, account.get("captcha_cooldown_minutes")
    )

    # F7: per-account override базовой вероятности dead-day. None — глобальный
    # default (5% базы, ×3 в выходные). Может быть переопределён через
    # accounts.json ключ "dead_day_rate" (или 0 чтобы выключить).
    account_state.set_account_dead_day_rate(account_name, account.get("dead_day_rate"))

    # A2: per-account дневные бюджеты. Ключи: daily_budget_listings /
    # daily_budget_messages / daily_budget_phone.
    account_state.set_daily_budget_limits(
        account_name,
        {
            "listings": account.get("daily_budget_listings"),
            "messages": account.get("daily_budget_messages"),
            "phone": account.get("daily_budget_phone"),
        },
    )


def _apply_warmup_if_new(account: dict, account_name: str) -> None:
    """B1: warmup-режим для нового аккаунта.

    Если в accounts.json задано "created_at": "YYYY-MM-DD", первые
    warmup_days дней (default 3) аккаунт работает в щадящем режиме:
    нет кликов телефона, нет LLM-сообщений, меньше листингов.
    Тихо игнорируется если created_at невалидный (только лог-warning).
    """
    created_at_str = account.get("created_at")
    if not created_at_str:
        return

    try:
        import datetime as _dt

        created_dt = _dt.datetime.strptime(created_at_str, "%Y-%m-%d")
        warmup_days = int(account.get("warmup_days", 3))
        warmup_end_ts = created_dt.timestamp() + warmup_days * 86400
        account_state.set_warmup_until(account_name, warmup_end_ts)
        if account_state.is_in_warmup(account_name):
            warmup_listing_limit = int(account.get("warmup_daily_listings", 20))
            account_state.set_daily_budget_limits(account_name, {"listings": warmup_listing_limit})
            log(
                account_name,
                f"B1: warmup-режим до {_dt.datetime.fromtimestamp(warmup_end_ts).strftime('%Y-%m-%d')} "
                f"(+{warmup_days} дн. с created_at). "
                f"Листингов/день: {warmup_listing_limit}. Телефон и сообщения — выкл.",
            )
    except (ValueError, OSError):
        _bot_logger.warning(
            "[%s] B1: некорректный created_at=%r — warmup пропускаем",
            account_name,
            created_at_str,
        )


def _check_health_and_log(account_name: str, db_manager: DatabaseManager) -> None:
    """C1: вычисляем captcha_rate за 7 дней и применяем degraded/critical
    ограничения, если health-score плохой. C1 не должен блокировать запуск,
    поэтому ловим Exception и игнорируем — стартуем как обычно при ошибках
    в health-расчётах.
    """
    try:
        from account_state import apply_health_restrictions, compute_account_health

        health = compute_account_health(account_name, db_manager)
        if health["mode"] in ("degraded", "critical"):
            apply_health_restrictions(account_name, health)
            log(
                account_name,
                f"C1: режим {health['mode']} (captcha_rate={health['score']:.3f}, "
                f"капч={health['captchas_7d']}, листингов={health['listings_7d']} за 7д). "
                "Бюджет снижен, телефон заблокирован.",
            )
        else:
            log(
                account_name,
                f"C1: аккаунт здоров (captcha_rate={health['score']:.3f}, "
                f"капч={health['captchas_7d']} за 7д).",
            )
    except Exception:
        _bot_logger.debug(
            "C1 health check failed", exc_info=True
        )  # C1 не должен блокировать запуск


def _pick_rotation_proxy(cfg: dict | None, exclude: set[str] | None = None) -> str | None:
    """T18: подбирает прокси для rotation-fallback при connection failure.

    Если probe enabled (cfg.proxy_probe_enabled, default True) — пытается
    через `pick_healthy_proxy` (HTTP-probe → живой). Иначе legacy:
    случайный из proxies.txt без проверки.

    `exclude` — прокси, которые уже пробовали (текущий мёртвый); не берём
    их повторно.
    """
    if cfg is not None and bool(cfg.get("proxy_probe_enabled", True)):
        from proxy_health import pick_healthy_proxy

        pool = _load_proxies_pool()
        random.shuffle(pool)
        if exclude:
            pool = [p for p in pool if p not in exclude]
        if not pool:
            return None
        chosen, _result = pick_healthy_proxy(
            pool,
            timeout=float(cfg.get("proxy_probe_timeout_sec", 10.0)),
            probe_url=cfg.get("proxy_probe_url", "https://api.ipify.org?format=json"),
            expected_country=cfg.get("proxy_expected_country") or None,
            max_attempts=int(cfg.get("proxy_max_probe_attempts", 5)),
            scheme=cfg.get("proxy_probe_scheme", "socks5h"),
            log_prefix="rotation: ",
            cache_get=account_state.get_cached_probe,
            cache_set=account_state.set_cached_probe,
        )
        return chosen

    # Legacy: random без probe.
    return get_random_proxy()


def _connect_with_retry(
    adspower: "AdsPowerAPI",
    user_id: str,
    account_name: str,
    cfg: dict | None = None,
) -> tuple | None:
    """Запуск AdsPower-профиля + WebDriver-подключение с попытками.

    На каждом провале — ротируем прокси из общего пула proxies.txt и
    останавливаем профиль перед следующей попыткой. T18: при включённом
    probe (default через `cfg`) ротация выбирает заведомо живой прокси
    через `pick_healthy_proxy`, а не случайный мёртвый из pool.

    Если профиль уже запущен — пробуем stop + poll и повторяем start.
    Возвращает (driver, wait) при успехе, либо None если все попытки
    исчерпаны (вызывающий должен сделать early return).
    """
    max_retries = 2
    tried_proxies: set[str] = set()
    for attempt in range(max_retries + 1):
        if _tg.is_stop_requested(account_name):
            return None
        try:
            log(account_name, f"Starting profile (Attempt {attempt + 1})...")
            debug_port = adspower.start_profile(user_id)
        except RuntimeError as e:
            msg = str(e).lower()
            if "already running" in msg or "already run" in msg:
                log(account_name, "Profile already running — stopping and retrying...")
                adspower.stop_profile(user_id)
                for _ in range(10):
                    if _tg.is_stop_requested(account_name):
                        return None
                    time.sleep(1)
                    if not adspower.is_profile_running(user_id):
                        break
                else:
                    _bot_logger.warning(
                        "[%s] Profile still running after stop, waiting another 5s...",
                        account_name,
                    )
                    time.sleep(5)
                if attempt < max_retries:
                    continue
            # Другие RuntimeError (временная недоступность) — тоже retry
            log(account_name, f"start_profile RuntimeError: {e}")
            if attempt < max_retries:
                time.sleep(3)
                continue
            return None
        except Exception as e:
            log(account_name, f"start_profile failed: {e}")
            return None

        # polling sleep с проверкой stop_event (5 раз по 1с)
        for _ in range(5):
            if _tg.is_stop_requested(account_name):
                adspower.stop_profile(user_id)
                return None
            time.sleep(1)

        driver = None
        try:
            driver = connect_to_sphere(debug_port)
            wait = WebDriverWait(driver, 15)

            # Test connection
            if safe_get(driver, "https://ya.ru", account_name, retries=1):
                return driver, wait
            raise Exception("Connection failed")
        except Exception as e:
            log(account_name, f"Connection fail: {e}. Rotating proxy...")
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            new_proxy = _pick_rotation_proxy(cfg, exclude=tried_proxies)
            if new_proxy and update_profile_proxy(adspower, user_id, new_proxy):
                tried_proxies.add(new_proxy)
                # H4-fix: маскируем credentials в логах — показываем только host:port
                _proxy_display = _mask_proxy(new_proxy)
                log(account_name, f"Proxy updated to {_proxy_display}")
            elif new_proxy is None and cfg is not None:
                log(account_name, "Rotation: probe не нашёл живого прокси.")
            adspower.stop_profile(user_id)
            if attempt == max_retries:
                return None
            hp(5, 10)
    return None


def _build_avito_client(driver, wait, account: dict, cfg: dict, db_manager: DatabaseManager):
    """Конструируем AvitoClient с per-account overrides для F1/F2/F5/E2.

    Все «волшебные числа» (favorite_rate, message_rate, max_listings_per_*,
    messenger_*) собираются здесь по приоритету: account → cfg → дефолт.
    Возвращает готовый AvitoClient.
    """
    # G1: AvitoClient — единый фасад над selenium-flow. Lazy import чтобы
    # не плодить циклические зависимости (avito_client тоже использует bot).
    from avito_client import AvitoClient

    account_name = account["name"]

    llm_config = {
        "api_key": cfg.get("openai_api_key", ""),
        "model": cfg.get("openai_model", "gpt-3.5-turbo"),
        "api_base": cfg.get("openai_api_base", "https://api.openai.com/v1"),
    }
    llm = LLMClassifier(llm_config, db_manager=db_manager)

    # F1: вероятности кликов «Избранное»/«Написать» в view_listing.
    fav_rate = float(
        account.get(
            "view_listing_favorite_rate",
            cfg.get("view_listing_favorite_rate", 0.08),
        )
    )
    # message_rate — вероятность «Написать» (замена старого call_rate/«Позвонить»).
    # Default 5% — стелс-режим: не каждому собственнику пишем.
    # Back-compat: если задан view_listing_message_rate/call_rate — используем его.
    msg_rate = float(
        account.get(
            "view_listing_message_rate",
            cfg.get(
                "view_listing_message_rate",
                account.get(
                    "view_listing_call_rate",
                    cfg.get("view_listing_call_rate", 0.05),
                ),
            ),
        )
    )

    # F2: верхние границы числа листингов/категорий за цикл.
    max_listings = int(
        account.get("max_listings_per_search", cfg.get("max_listings_per_search", 7))
    )
    max_cats = int(
        account.get("max_categories_per_browse", cfg.get("max_categories_per_browse", 4))
    )
    max_browse_listings = int(
        account.get("max_listings_per_browse", cfg.get("max_listings_per_browse", 4))
    )

    # F5: реалистичные задержки ответа в мессенджере. Любой ключ можно
    # опустить — соответствующий дефолт подхватится в AvitoMessenger.__init__.
    messenger_cfg: dict = {}
    for key in (
        "min_reply_age_min",
        "max_reply_age_min",
        "reply_delay_mu",
        "reply_delay_sigma",
        "ignore_new_dialog_chance",
    ):
        full_key = f"messenger_{key}"
        val = account.get(full_key, cfg.get(full_key))
        if val is not None:
            messenger_cfg[key] = float(val)

    # T5: persona в мессенджере — для подбора темпа печати (молодой/инвестор).
    # Та же persona что в outbound_messenger (consistency), либо None.
    persona = account.get("persona") or cfg.get("persona")
    if persona:
        messenger_cfg["persona"] = persona

    # H1: per-account outbound config (max_per_cycle, listing_min_age_hours, etc.)
    outbound_cfg = {}
    for key in (
        "outbound_max_per_cycle",
        "outbound_listing_min_age_hours",
        "outbound_between_messages_min_sec",
        "outbound_between_messages_max_sec",
    ):
        val = account.get(key)
        if val is not None:
            outbound_cfg[key] = val

    client = AvitoClient(
        driver,
        wait,
        account_name,
        log_func=log,
        db_manager=db_manager,
        llm_classifier=llm,
        search_filters=account.get("search_filters"),
        favorite_rate=fav_rate,
        message_rate=msg_rate,
        max_listings_per_search=max_listings,
        max_categories_per_browse=max_cats,
        max_listings_per_browse=max_browse_listings,
        messenger_config=messenger_cfg or None,
        outbound_config=outbound_cfg or None,
    )
    # Сохраняем account на client для persona-driven messaging в view_listing.
    client._account = account
    return client


def _sleep_until_tomorrow(account: dict, cfg: dict, account_name: str) -> None:
    """F7: спим до завтрашнего active_hours_start (default 9:00).

    Прерывается если выставлен stop_event (TG /stop). Используется
    после положительного решения F7 dead-day — досыпаем до начала
    следующего дня, чтобы не пропустить решение второй раз сегодня.
    """
    import datetime as _dt

    start_hour = int(account.get("active_hours_start", cfg.get("active_hours_start", 9)))
    now = _dt.datetime.now()
    try:
        tomorrow = (now + _dt.timedelta(days=1)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
    except ValueError:
        # DST transition: несуществующий час (например 2:00 при переходе вперёд)
        # Сдвигаем на час вперёд
        tomorrow = (now + _dt.timedelta(days=1)).replace(
            hour=start_hour + 1, minute=0, second=0, microsecond=0
        )
    wait_secs = max(0.0, (tomorrow - now).total_seconds())
    log(
        account_name,
        f"F7: сегодня dead-day — спим {wait_secs / 3600:.1f} ч до завтра {start_hour:02d}:00.",
    )
    slept = 0.0
    stop_ev = _tg.get_account_stop_event(account_name)
    while slept < wait_secs and not stop_ev.is_set() and not _tg.is_stop_requested():
        chunk = min(60.0, wait_secs - slept)
        time.sleep(chunk)
        slept += chunk


def _run_main_loop(client, driver, account: dict, cfg: dict, account_name: str) -> None:
    """Основной цикл: F7 dead-day → F6 prob → F8 cycle dispatch → A4 пауза.

    Гоняем пока не выставлен _tg.get_account_stop_event(account_name). Каждый «цикл» — это либо
    один из 4-х F8-вариантов (full/messenger_only/browse_only/profile_check),
    либо пропуск (F7 dead-day / F6 неудачный бросок). После каждого цикла
    — A4 пауза 30-90 мин (per-account overridable через session_pause_*).
    """

    while not _tg.is_stop_requested():
        # Per-account stop event — проверяем чаще для быстрого выхода
        if _tg.is_stop_requested(account_name):
            break

        # ── F7: dead-day — пропускаем сегодняшний день целиком ─────────
        if account_state.is_dead_day(account_name):
            _sleep_until_tomorrow(account, cfg, account_name)
            continue

        # ── F6: Probabilistic active hours ────────────────────────────
        # Каждый цикл бросаем монетку с вероятностью _active_probability:
        # ночью ~2-5%, утром/вечером ~85-95%. Если выпало больше prob —
        # пропускаем цикл с обычной session-паузой 30-90 мин.
        # Совместимость с B2: если active_hours_start/end заданы, ВНЕ
        # окна prob=0 — поведение строго бинарное, как раньше.
        prob = _active_probability(account, cfg)
        if random.random() > prob:
            pause_sec = pick_cycle_pause(account, cfg, kind="session_pause")
            log(
                account_name,
                f"F6: prob={prob:.2f} < random — пропускаю цикл, пауза {pause_sec / 60:.0f} мин",
            )
            hp(pause_sec, pause_sec, stop_event=_tg.get_account_stop_event(account_name))
            continue

        # A3/A4: сбрасываем сессионные phone-счётчики в начале цикла.
        account_state.start_new_session(account_name)

        # ── F8 + T17: выбор типа цикла ───────────────────────────────────
        # Не каждый цикл должен быть «полным» (browse + parse + messenger).
        # Реальный пользователь чаще заходит просто проверить мессенджер
        # или полистать профиль. См. _pick_cycle_kind для деталей.
        # T17: если после недавней Avito-капчи outbound заблокирован —
        # _pick_cycle_kind зануляет его вес, не выпадет никогда.
        outbound_disabled = account_state.is_outbound_disabled(account_name)
        if outbound_disabled:
            until_ts = account_state.get_outbound_disabled_until(account_name)
            until_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(until_ts))
            log(account_name, f"T17: outbound заблокирован после Avito-капчи (до {until_str})")
        kind = _pick_cycle_kind(
            account,
            cfg,
            is_warmup=account_state.is_in_warmup(account_name),
            outbound_disabled=outbound_disabled,
        )
        log(account_name, f"F8: cycle kind = {kind}")

        if kind == "full":
            # Behavioral camouflage: перед парсингом — короткий browse по категориям,
            # как реальный пользователь который сначала листает, потом открывает.
            # Без этого бот сразу прыгает в find_and_view — паттерн автоматизации.
            if random.random() < 0.6:
                hp(3, 8, stop_event=_tg.get_account_stop_event(account_name))
                client.browse_commercial_categories()
                if _tg.is_stop_requested(account_name):
                    break

            processed, new_listings, errors = client.find_and_view_commercial_listings()
            log(account_name, f"Processed {processed}, {new_listings} new, {errors} errors")
            if _tg.is_stop_requested(account_name):
                break

            client.process_messages()
            if _tg.is_stop_requested(account_name):
                break

        elif kind == "messenger_only":
            # F8: только мессенджер. Имитирует «зашёл с пуша/прочитать».
            client.process_messages()
            if _tg.is_stop_requested(account_name):
                break

        elif kind == "browse_only":
            # F8: browse + парс листингов, БЕЗ мессенджера.
            log(account_name, "  Browse-only cycle...")
            hp(4, 10)
            client.browse_commercial_categories()
            if _tg.is_stop_requested(account_name):
                break

            processed, new_listings, errors = client.find_and_view_commercial_listings()
            log(account_name, f"Processed {processed}, {new_listings} new, {errors} errors")
            if _tg.is_stop_requested(account_name):
                break

        elif kind == "profile_check":
            # F8: «зашёл просто посмотреть свой профиль». Без LLM/сообщений.
            _do_profile_check(driver, account_name)
            if _tg.is_stop_requested(account_name):
                break

        elif kind == "outbound_only":
            # H1: proactive контакты — пишем 1-3 собственникам по их листингам.
            # Свой класс OutboundMessenger (см. outbound_messenger.py),
            # max_per_cycle, min_listing_age и паузы конфигурируются
            # per-account через accounts.json. В warmup outbound_only
            # никогда не выпадает (вес 0 в _CYCLE_KINDS_WARMUP).
            client.run_outbound_cycle(account=account)
            if _tg.is_stop_requested(account_name):
                break

        # ── A4 + T19: Session pause ─────────────────────────────────────
        # Раньше: uniform [pause_min, pause_max] минут — слишком регулярно.
        # T19: lognormal по умолчанию + шанс длинного перерыва «обед/ужин»
        # (1-2 раза в день по 2-5 часов в часах 12-14 / 18-21).
        # cycle_pause.pick_cycle_pause возвращает (seconds, label) и сам
        # инкрементирует счётчик long-break'ов через account_state.
        pause_secs, pause_label = pick_cycle_pause(
            account, cfg, account_state=account_state, account_name=account_name
        )
        # T20: запись sample'а для percentile-аудита в /health.
        # Разделяем regular (lognormal распределение) и long_break (uniform 2-5h)
        # — иначе distribution получается bimodal и теряется смысл аудита.
        try:
            db = getattr(client, "db", None)
            if db is not None:
                event_type = "long_break_sec" if pause_label == "long_break" else "cycle_pause_sec"
                db.record_behavioral_sample(account_name, event_type, float(pause_secs))
        except Exception as e:
            log(account_name, f"T20: не смог записать behavioral sample: {e}")
        next_time = time.strftime("%H:%M", time.localtime(time.time() + pause_secs))
        if pause_label == "long_break":
            log(
                account_name,
                f"T19: Длинный перерыв «обед/ужин» — {pause_secs / 60:.0f} мин, до ~{next_time}.",
            )
        else:
            log(
                account_name,
                f"Цикл завершён. Следующий запуск в ~{next_time} "
                f"(пауза {pause_secs / 60:.0f} мин).",
            )
        _human_delay(
            pause_secs,
            pause_secs,
            stop_event=_tg.get_account_stop_event(account_name),
            distribution="uniform",
        )


def run_thread(account: dict, cfg: dict, adspower: AdsPowerAPI, db_manager: DatabaseManager):
    """Точка входа потока-аккаунта. Оркестрирует setup → connect →
    login → main-loop → cleanup. Каждая стадия вынесена в свой helper
    выше — это упрощает чтение, тестирование и дальнейшие правки.
    """
    account_name = account["name"]
    user_id = account.get("user_id")
    driver = None

    # Guard: user_id (adspower_id) обязателен
    if not user_id:
        _bot_logger.error("No user_id/adspower_id for account %s", account_name)
        return

    # ── Pre-cycle setup ───────────────────────────────────────────────────
    _apply_per_account_overrides(account)
    _apply_warmup_if_new(account, account_name)
    _check_health_and_log(account_name, db_manager)

    # Восстанавливаем captcha_timestamps из БД (persist across restarts)
    try:
        captcha_ts = db_manager.get_captcha_timestamps_24h(account_name)
        if captcha_ts:
            account_state.load_captcha_history(account_name, captcha_ts)
    except Exception as e:
        _bot_logger.debug("Failed to load captcha history for %s: %s", account_name, e)

    # ── A1 + T18: Per-account proxy setup with health probe ─────────────
    # Приоритет: 1) поле "proxy" в accounts.json, 2) случайный из proxies.txt.
    # T18: перед apply делается HTTP-probe (api.ipify.org через прокси);
    # если timeout / non-200 / wrong country — пробуем следующий кандидат.
    # Если ни один не прошёл — спрашиваем админа в TG (продолжить без прокси?).
    proxy_result = _apply_account_proxy(adspower, user_id, account, account_name, cfg)
    if proxy_result is None:
        return
    if proxy_result == "__no_proxy__":
        log(account_name, "A1: Запуск БЕЗ прокси (админ разрешил).")

    try:
        # ── Connect (с retry + T18 probe-driven ротацией прокси) ────────
        connect_result = _connect_with_retry(adspower, user_id, account_name, cfg)
        if connect_result is None:
            return
        driver, wait = connect_result

        # ── Build AvitoClient (G1 фасад над selenium-flow) ──────────────
        client = _build_avito_client(driver, wait, account, cfg, db_manager)

        # ── Stage 0: Multi-site Warmup ──────────────────────────────────────
        # T12: Используем усиленный multi-site прогрев (behavioral camouflage)
        # вместо одиночного Yandex. Yandex поиск по-прежнему может вызываться
        # внутри (если with_yandex_search=True), но при капче он не убьёт
        # весь прогрев, а просто будет пропущен, перейдя к другим сайтам.
        from warmup import big_warmup

        warmup_sites = account.get("warmup_sites", cfg.get("warmup_sites", 3))
        warmup_stats = big_warmup(
            driver, account_name=account_name, num_sites=warmup_sites, with_yandex_search=True
        )

        if not warmup_stats or not warmup_stats.get("ok"):
            log(
                account_name,
                f"WARN: Warmup warning/failure. Visited: {warmup_stats.get('sites_visited', 0)}. "
                "Продолжаю с ослабленным fingerprint.",
            )

        # ── Stage 1: Login (B4 native → cookies → manual phone/password) ──
        # G1: 3-уровневая логика инкапсулирована в client.login().
        if not client.login(
            cookies_path=account.get("cookies_path"),
            phone=account.get("phone"),
            password=account.get("password"),
        ):
            return

        if _tg.is_stop_requested(account_name):
            return

        # ── Main loop (F7/F6/F8 + A4 пауза) ─────────────────────────────
        _run_main_loop(client, driver, account, cfg, account_name)

    except Exception:
        # L6: run_thread — top-level потока. Раньше теряли traceback (только
        # str(e)), что критично, если падение из-за неочевидного бага в
        # глубине Selenium/avito-flow. logger.exception() добавляет полный
        # stacktrace + триггерит TGAlertHandler (E4) → админ получает алерт
        # с trace, не нужно идти в файл-лог.
        get_account_logger(_bot_logger.name, account_name).exception("run_thread crashed")
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                # L7: driver.quit() может упасть при уже закрытом браузере /
                # отвалившемся debug-port. Любые другие исключения тут —
                # настоящие баги, не глотаем.
                pass
        adspower.stop_profile(user_id)


def run_big_warmup_for_account(
    account: dict,
    cfg: dict,
    adspower: AdsPowerAPI,
    *,
    num_sites: int | None = None,
    with_yandex_search: bool = True,
) -> dict:
    """T12: standalone big_warmup для одного аккаунта (без парсинга/messenger).

    Делается lifecycle:
        1. apply proxy (как в run_thread) — если прокси не выдан, ВЫХОДИМ
           с error (T12 без proxy — стрельба в ногу).
        2. AdsPower start_profile → connect_to_sphere → apply_stealth.
        3. big_warmup(driver, num_sites=10, with_yandex_search=True).
           Это ~15-30 минут (3-5 сайтов × 30-90s + Yandex queries).
        4. driver.quit + AdsPower stop_profile.

    Args:
        account: dict из accounts.json.
        cfg: глобальный config.json (для legacy proxy fallback).
        adspower: AdsPowerAPI инстанс.
        num_sites: сколько сайтов посетить (default ~10 для большого прогрева).
        with_yandex_search: True → ещё и Yandex queries.

    Returns:
        Stats dict: {"ok": bool, "stats": {...big_warmup stats...},
                     "error": str | None}
    """
    from warmup import big_warmup

    account_name = account["name"]
    user_id = account.get("user_id")
    driver = None
    result = {"ok": False, "stats": None, "error": None}

    # ── A1 + T18: proxy setup with health probe ────────────────────────
    if _apply_account_proxy(adspower, user_id, account, account_name, cfg) is None:
        result["error"] = "no proxy"
        return result

    try:
        # ── Connect (с T18 probe-driven ротацией) ───────────────────────
        connect_result = _connect_with_retry(adspower, user_id, account_name, cfg)
        if connect_result is None:
            result["error"] = "connect failed"
            return result
        driver, _wait = connect_result

        log(account_name, f"T12: starting big_warmup ({num_sites or 10} sites)...")
        stats = big_warmup(
            driver,
            account_name=account_name,
            num_sites=num_sites if num_sites is not None else 10,
            with_yandex_search=with_yandex_search,
            log_func=log,
        )
        log(
            account_name,
            f"T12: big_warmup finished — sites_visited="
            f"{stats.get('sites_visited', 0)}, "
            f"failed={stats.get('sites_failed', 0)}, "
            f"yandex_ok={stats.get('yandex_ok')}, "
            f"duration={stats.get('duration_seconds', 0.0):.0f}s",
        )
        result["ok"] = True
        result["stats"] = stats
    except Exception as exc:
        get_account_logger(_bot_logger.name, account_name).exception("T12: big_warmup crashed")
        result["error"] = f"{type(exc).__name__}: {exc!s:.120}"
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                pass
        if user_id:
            adspower.stop_profile(user_id)

    return result


def _load_cfg():
    """
    Загружает config.json. На все типичные ошибки пишет чёткое сообщение
    в stderr/log_buffer и выходит из процесса (C1).
    """
    cfg_path = Path(__file__).parent / "config.json"
    # L1: print + add_log → _bot_logger.critical. После setup_logging() и
    # install_tg_buffer_handler() стандартные log-handler'ы сами доставляют
    # в stderr (HumanFormatter) и в TG-буфер. Если уже стоит TGAlertHandler
    # (на момент перезапуска через TG-команду), сообщение пойдёт админу
    # мгновенно — что лучше старого поведения через ручной add_log.
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        _bot_logger.critical(
            "config.json не найден: %s\n"
            "Создай файл по шаблону README.md (минимум: telegram_bot_token, "
            "telegram_admin_id, accounts[]). Пример — в начале README.",
            cfg_path,
        )
        raise SystemExit(2) from None
    except json.JSONDecodeError as e:
        _bot_logger.critical(
            "config.json не парсится как JSON: %s (line=%d, col=%d)\n"
            "Открой %s и проверь синтаксис (запятые, кавычки, скобки).",
            e.msg,
            e.lineno,
            e.colno,
            cfg_path,
        )
        raise SystemExit(2) from None
    except OSError as e:
        _bot_logger.critical("не могу прочитать %s: %s", cfg_path, e)
        raise SystemExit(2) from None

    # Базовая валидация ожидаемых полей (warn-only, чтобы не блокировать
    # запуск с неполным конфигом во время разработки).
    if not isinstance(cfg, dict):
        _bot_logger.critical(
            "config.json должен быть JSON-объектом, получено: %s", type(cfg).__name__
        )
        raise SystemExit(2)
    if "accounts" not in cfg or not isinstance(cfg["accounts"], list):
        # L1: ручной _tg.add_log → logger.warning (через TGBufferHandler уйдёт
        # в тот же буфер). Сообщение информационное — не CRITICAL.
        _bot_logger.warning("в config.json отсутствует или некорректен ключ 'accounts'")

    # H3: применить env-переопределения для секретов
    # (OPENAI_API_KEY / TELEGRAM_BOT_TOKEN / ADSPOWER_API_KEY и т.п.).
    # Приоритет: ENV > config.json. Так можно держать рабочий config.json
    # без секретов и подкладывать их через .env (который в .gitignore).
    from env_config import apply_env_overrides

    apply_env_overrides(cfg)

    # A3: подхватить captcha_cooldown_minutes из config.json (если задан).
    try:
        from account_state import configure_from_cfg as _cfg_account_state

        _cfg_account_state(cfg)
    except Exception:
        # Не блокируем загрузку бота из-за конфигурации cooldown'а.
        pass
    return cfg


def _launch_commercial_bot_threads(cfg=None):
    if cfg is None:
        cfg = _load_cfg()
    # G2: список аккаунтов теперь грузится через accounts.py:
    # сначала пытаемся accounts.json, затем legacy cfg["accounts"].
    # Disabled-аккаунты отфильтрованы; алиасы (adspower_id ↔ user_id)
    # нормализованы.
    from accounts import load_accounts

    accounts = load_accounts(Path(__file__).parent, cfg)
    if not accounts:
        _tg.add_log("Нет включённых аккаунтов для запуска (G2: проверь accounts.json).")
        return

    # Pre-flight ping: проверяем что AdsPower API доступен ДО создания потоков
    # /status endpoint не существует в AdsPower — используем /api/v1/browser/active
    adspower_url = cfg.get("adspower_api_url", "")
    try:
        r = requests.get(adspower_url.rstrip("/") + "/api/v1/browser/active", timeout=5)
        r.raise_for_status()
    except requests.RequestException:
        msg = f"❌ AdsPower API недоступен по {adspower_url}. Запустите AdsPower и повторите."
        _bot_logger.error(msg)
        _tg._send_message(msg)
        return

    adspower = AdsPowerAPI(adspower_url, cfg.get("adspower_api_key"))
    db_manager = DatabaseManager()

    threads = []

    # Pre-launch cleanup: Ensure all profiles are stopped before starting
    _bot_logger.info("Performing pre-launch cleanup of AdsPower profiles...")
    for acc in accounts:
        user_id = acc.get("adspower_id")
        if user_id:
            try:
                adspower.stop_profile(user_id)
            except Exception as e:
                _bot_logger.warning("Cleanup failed for profile %s: %s", user_id, e)
            # Ждём фактической остановки профиля до 10 секунд
            for _attempt in range(10):
                if not adspower.is_profile_running(user_id):
                    break
                time.sleep(1)
            else:
                _bot_logger.warning(
                    "Profile %s still running after stop — continuing anyway", user_id
                )

    for acc in accounts:
        t = threading.Thread(
            target=run_thread,
            args=(acc, cfg, adspower, db_manager),
            name="acc-" + acc["name"],
        )
        threads.append(t)
        with _tg._threads_lock:
            _tg.active_threads.append(t)
        t.start()
        # B3: случайная задержка 30-180 сек между стартами потоков.
        # Предотвращает регулярный паттерн «все аккаунты стартуют одновременно».
        _human_delay(30, 180, stop_event=_tg.stop_event)

    # ── Thread Supervisor ─────────────────────────────────────────────────
    # Мониторим потоки и перезапускаем упавшие (max 3 restart/час на аккаунт).
    # Если stop_event выставлен — не перезапускаем, ждём завершения.
    _MAX_RESTARTS_PER_HOUR = 3
    restart_history: dict[str, list[float]] = {}  # acc_name -> [timestamps]
    # Маппинг thread -> account для перезапуска
    thread_account_map: dict[threading.Thread, dict] = {}
    for t, acc in zip(threads, accounts):
        thread_account_map[t] = acc

    # Watchdog: периодически чистим orphaned AdsPower profiles (zombie Chrome).
    _WATCHDOG_INTERVAL = 300  # 5 минут
    _last_watchdog = time.time()

    while not _tg.stop_event.is_set():
        alive_threads = []
        for t in list(thread_account_map.keys()):
            if t.is_alive():
                alive_threads.append(t)
                continue

            # Поток умер — решаем перезапускать ли
            acc = thread_account_map.pop(t)
            acc_name = acc["name"]

            # Убираем из active_threads
            with _tg._threads_lock:
                if t in _tg.active_threads:
                    _tg.active_threads.remove(t)

            # Проверяем лимит рестартов (скользящее окно 1 час)
            now = time.time()
            history = restart_history.setdefault(acc_name, [])
            # Чистим записи старше часа
            history[:] = [ts for ts in history if now - ts < 3600]

            if len(history) >= _MAX_RESTARTS_PER_HOUR:
                _bot_logger.error(
                    "Account %s: %d restarts in last hour — NOT restarting "
                    "(possible crash loop). Manual intervention required.",
                    acc_name,
                    len(history),
                )
                _tg._send_message(
                    f"⚠️ Аккаунт {acc_name}: {len(history)} рестартов за час. "
                    f"Остановлен. Проверьте логи."
                )
                continue

            # Перезапускаем
            history.append(now)
            _bot_logger.warning(
                "Account %s thread died — restarting (%d/%d this hour)",
                acc_name,
                len(history),
                _MAX_RESTARTS_PER_HOUR,
            )
            # Сбрасываем per-account stop event (мог быть выставлен при ошибке)
            _tg.clear_account_stop_event(acc_name)

            new_t = threading.Thread(
                target=run_thread,
                args=(acc, cfg, adspower, db_manager),
                name="acc-" + acc_name,
            )
            thread_account_map[new_t] = acc
            with _tg._threads_lock:
                _tg.active_threads.append(new_t)
            new_t.start()
            alive_threads.append(new_t)

        if not alive_threads and not thread_account_map:
            # Все потоки мертвы и ни один не перезапущен
            break

        # Watchdog: каждые 5 мин проверяем orphaned AdsPower profiles.
        # Если профиль запущен, но его нет среди живых потоков — останавливаем.
        if time.time() - _last_watchdog > _WATCHDOG_INTERVAL:
            _last_watchdog = time.time()
            active_user_ids = set()
            for _t, _acc in thread_account_map.items():
                if _t.is_alive():
                    uid = _acc.get("user_id") or _acc.get("adspower_id")
                    if uid:
                        active_user_ids.add(uid)
            for acc in accounts:
                uid = acc.get("user_id") or acc.get("adspower_id")
                if uid and uid not in active_user_ids:
                    if adspower.is_profile_running(uid):
                        _bot_logger.warning(
                            "Watchdog: orphaned profile %s (no alive thread) — stopping", uid
                        )
                        adspower.stop_profile(uid)

        # Проверяем каждые 10 секунд
        _tg.stop_event.wait(timeout=10)

    # Ждём завершения оставшихся потоков после stop_event
    for t in thread_account_map:
        t.join(timeout=30)


def _graceful_shutdown(signum, frame):
    """Signal handler: выставляет глобальный stop_event и ждёт завершения потоков.

    Потоки проверяют is_stop_requested() на каждой итерации main loop и в
    каждом human_delay — они увидят сигнал в течение 1-5 секунд и начнут
    cleanup (driver.quit + adspower.stop_profile в finally-блоке run_thread).
    """
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    _bot_logger.info("Received %s — initiating graceful shutdown...", sig_name)
    _tg.stop_event.set()

    # Даём потокам SHUTDOWN_TIMEOUT секунд на cleanup (driver.quit + profile stop)
    shutdown_timeout = 30
    deadline = time.time() + shutdown_timeout
    for t in list(_tg.active_threads):
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        t.join(timeout=remaining)

    still_alive = [t.name for t in _tg.active_threads if t.is_alive()]
    if still_alive:
        _bot_logger.warning(
            "Threads still alive after %ds timeout: %s — forcing exit",
            shutdown_timeout,
            ", ".join(still_alive),
        )
    else:
        _bot_logger.info("All threads stopped cleanly.")

    # C5-fix: НЕ вызываем sys.exit() из signal handler — это может повредить
    # SQLite WAL. Вместо этого main loop проверяет stop_event и выходит сам.
    # os._exit только как last resort если main loop завис.


def main():
    # H3: загружаем .env (если есть) ДО setup_logging — чтобы LOG_LEVEL /
    # LOG_FORMAT тоже можно было задавать через .env.
    from env_config import load_dotenv_if_present

    load_dotenv_if_present(Path(__file__).parent / ".env")

    # ── Lockfile: защита от двойного запуска ─────────────────────────────
    # На Windows 3+ процесса bot.py запускают各自的 TG-контроллер и
    # stop_event, что ломает кнопки Стоп/Запуск (callback уходит не в тот
    # процесс). Lockfile предотвращает запуск второй копии.
    # Кроссплатформенный: msvcrt (Windows) / fcntl (Linux/macOS).
    _lockfile_path = Path(__file__).parent / ".bot.lock"
    try:
        _lockfile_fd = os.open(str(_lockfile_path), os.O_CREAT | os.O_RDWR)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(_lockfile_fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_lockfile_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            "FATAL: другой экземпляр bot.py уже запущен. "
            "Если это не так — удали .bot.lock и попробуй снова.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Пишем PID для диагностики
    os.write(_lockfile_fd, str(os.getpid()).encode())
    os.fsync(_lockfile_fd)

    # E1: инициализируем логирование первым делом — так все последующие
    # модули, попадая в логи, получат единый формат и уровень.
    setup_logging()
    # Все логи дублируются в кольцевой TG-буфер (раньше это делал log()
    # вручную через _tg.add_log).
    install_tg_buffer_handler(_tg.add_log)

    # Graceful shutdown: SIGINT (Ctrl+C) и SIGTERM (docker stop / systemd)
    # выставляют stop_event и ждут join потоков с таймаутом.
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    cfg = _load_cfg()
    tg_token = cfg.get("telegram_bot_token", "")
    tg_admin = cfg.get("telegram_admin_id", 0)

    if tg_token and tg_token != "YOUR_TG_BOT_TOKEN":
        from tg_bot import TelegramController

        tg_ctrl = TelegramController(tg_token, tg_admin)
        # Поддержка нескольких админов: telegram_admin_ids из config.json
        admin_ids = cfg.get("telegram_admin_ids", [])
        if admin_ids:
            tg_ctrl.set_admin_ids(admin_ids)
        # Shared DB для TG-команд — один экземпляр на весь процесс.
        tg_ctrl.set_db_manager(DatabaseManager())
        _tg._tg_controller = tg_ctrl

        def _safe_launch():
            """Обёртка: ловит SystemExit от _load_cfg чтобы не убить весь процесс."""
            try:
                _launch_commercial_bot_threads(_load_cfg())
            except SystemExit as e:
                _bot_logger.critical("_load_cfg() упал с SystemExit(%s) — битый конфиг?", e.code)
                from tg_bot import _send_message

                _send_message("❌ Не удалось запустить бота: ошибка конфигурации. Проверь логи.")

        tg_ctrl.set_run_callback(_safe_launch)
        # E4: ERROR/CRITICAL → мгновенно в TG админу.
        install_tg_alert_handler(tg_ctrl.notify)
        threading.Thread(target=tg_ctrl.start_polling, daemon=True).start()

        _bot_logger.info("TG bot polling started. Waiting for /start command in Telegram...")

        # Держим main thread живым — потоки запускаются через TG callback.
        # stop_event.wait() блокирует до graceful shutdown (SIGINT/SIGTERM).
        _tg.stop_event.wait()
    else:
        _launch_commercial_bot_threads(cfg)


if __name__ == "__main__":
    main()
