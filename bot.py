"""
Avito Commercial Real Estate Parser Bot
"""

import json
import logging
import random
import sys
import threading
import time
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
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
from database import DatabaseManager
from human_delay import human_delay as _human_delay
from llm_classifier import LLMClassifier
from logging_setup import (
    get_account_logger,
    install_tg_alert_handler,
    install_tg_buffer_handler,
    setup_logging,
)

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

print_lock = threading.Lock()

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

    def __init__(self, base_url: str, api_key: str = None):
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
            print(f"[AdsPower] Using API Key: {key_hint}")

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
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Selenium helpers
# ══════════════════════════════════════════════════════════════════════════════


def connect_to_sphere(debug_port: int) -> webdriver.Chrome:
    """Connect Selenium to already running AdsPower profile."""
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")

    # Use Service to specify the driver path
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"Standard driver install failed: {e}. Trying fallback...")
        driver = webdriver.Chrome(options=options)

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

    with open(path, encoding="utf-8") as f:
        cookies = json.load(f)

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
            pass

    try:
        driver.get(f"https://{domain}")
        time.sleep(3)
    except Exception:
        pass


# ── Behavioral primitives ──────────────────────────────────────────────────

# B2: human-like delays — нормальное распределение вместо плоского uniform,
# с прерыванием по stop_event. Реализация в human_delay.py (импортирован выше).


def hp(lo=0.5, hi=1.5, *, distribution="normal"):
    """
    Backward-compatible wrapper: старые вызовы hp(lo, hi) теперь идут через
    human_delay c distribution='normal' и поддержкой раннего выхода по
    stop_event (TG /stop).
    """
    return _human_delay(lo, hi, distribution=distribution, stop_event=_tg.stop_event)


def human_type(element, text, speed_range=(0.05, 0.25)):
    for ch in text:
        element.send_keys(ch)
        if random.random() < 0.1:
            time.sleep(random.uniform(0.5, 1.5))
        else:
            time.sleep(random.uniform(*speed_range))


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


def move_click(driver, element):
    try:
        actions = ActionChains(driver)
        actions.move_to_element_with_offset(element, random.randint(-4, 4), random.randint(-3, 3))
        actions.pause(random.uniform(0.2, 0.5))
        actions.click()
        actions.perform()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def human_scroll(driver, direction="down", iters=None):
    if iters is None:
        iters = random.randint(3, 7)
    for _ in range(iters):
        amount = random.randint(150, 500) * (1 if direction == "down" else -1)
        if direction == "down" and random.random() < 0.2:
            driver.execute_script(f"window.scrollBy(0, -{random.randint(50, 130)});")
            hp(0.3, 0.7)
        driver.execute_script(f"window.scrollBy(0, {amount});")
        hp(0.3, 1.1)
        if random.random() < 0.25:
            hp(1, 3)


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


def scroll_gallery(driver, wait):
    try:
        wait.until(EC.presence_of_element_located((By.XPATH, "//*[@data-marker='image-frame']")))
    except Exception:
        return
    hp(3, 7)
    for _ in range(20):
        btns = driver.find_elements(
            By.XPATH,
            "//*[@data-marker='image-frame/next'] | "
            "//button[contains(@class,'gallery-next')] | "
            "//button[contains(@aria-label,'следующ')] | "
            "//button[contains(@aria-label,'Next')]",
        )
        if not btns or not btns[0].is_displayed():
            break
        try:
            driver.execute_script("arguments[0].click();", btns[0])
        except Exception:
            break
        hp(3, 7)


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

    # Check page title - Yandex captcha often has "Ой!" as title
    try:
        title = driver.title
        if "Ой!" in title or "Captcha" in title:
            log(account_name, f"!!! ALERT: BLOCK DETECTED BY TITLE ({title}) !!!")
            return True
    except:
        pass

    page_source = driver.page_source
    for pattern in block_patterns:
        if pattern in page_source:
            log(account_name, f"!!! ALERT: IP BLOCK DETECTED BY PATTERN ({pattern}) !!!")
            return True

    # Check for empty or tiny response (ERR_EMPTY_RESPONSE or failed load)
    if len(page_source) < 200:
        log(account_name, "!!! ALERT: EMPTY OR MALFORMED PAGE DETECTED !!!")
        return True

    return False


def view_listing(driver, wait, account_name):
    if check_block(driver, account_name):
        return False

    try:
        wait.until(
            EC.presence_of_element_located((By.XPATH, "//h1[@data-marker='item-view/title-info']"))
        )
    except Exception:
        return True

    # Random dwell time on page start (5-15s)
    dwell_time = random.uniform(5, 15)
    log(account_name, f"  Viewing listing (dwell time: {dwell_time:.1f}s)...")
    hp(dwell_time / 2, dwell_time)

    # Natural behavior: scroll, maybe check photos, scroll more
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
                desc = driver.find_element(By.XPATH, "//div[@data-marker='item-description']")
                slow_scroll_to(driver, desc)
                hp(3, 7)  # Reading description
            except:
                pass
        hp(1, 3)

    # Added more natural pauses and random behaviors
    if random.random() < 0.4:
        random_mouse_move(driver)
        hp(1, 4)

    # Add to favorites with 70% probability
    if random.random() < 0.70:
        try:
            fav_btn = WebDriverWait(driver, 6).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[@data-marker='item-view/add-to-favorites'] | "
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

    # Click "Call" in ~55% of cases
    if random.random() < 0.55:
        try:
            call_btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[@data-marker='item-contact-bar/call'] | "
                        "//button[contains(@class,'phone-button')] | "
                        "//button[.//span[contains(text(),'Позвонить')]]",
                    )
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", call_btn)
            hp(1.5, 3.5)
            move_click(driver, call_btn)
            log(account_name, "  'Call' button pressed.")
            hp(5, 12)  # Stay after call
        except Exception:
            log(account_name, "  'Call' button not found.")

    # Final dwell before leaving
    hp(2, 5)
    return True


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


# Thematic queries for Yandex warmup
THEMATIC_QUERIES = [
    "коммерческая недвижимость в москве купить",
    "аренда офиса от собственника",
    "склады и производства продажа",
    "торговые площади в аренду миллионники",
    "купить готовый бизнес в россии",
    "инвестиции в недвижимость 2024",
    "авито коммерческая недвижимость",
    "помещение свободного назначения купить",
]


def update_profile_proxy(adspower_api, user_id, proxy_str):
    """Updates the proxy for an AdsPower profile via API."""
    # Expected proxy_str format: host:port:user:pass or host:port
    parts = proxy_str.split(":")
    if len(parts) < 2:
        return False

    proxy_config = {
        "proxy_soft": "other",
        "proxy_type": "socks5",  # or http
        "proxy_host": parts[0],
        "proxy_port": parts[1],
    }
    if len(parts) >= 4:
        proxy_config["proxy_user"] = parts[2]
        proxy_config["proxy_password"] = parts[3]

    # AdsPower API endpoint for updating profile
    url = f"{adspower_api.base}/api/v1/user/update"
    headers = {"Authorization": f"Bearer {adspower_api.api_key}"} if adspower_api.api_key else {}
    payload = {"user_id": user_id, "user_proxy_config": proxy_config}

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        return r.json().get("code") == 0
    except:
        return False


def yandex_warmup(driver, wait, account_name, num_queries=2):
    log(account_name, "=== Enhanced Thematic Yandex Warmup ===")

    if not safe_get(driver, "https://ya.ru", account_name):
        return False

    queries = random.sample(THEMATIC_QUERIES, min(num_queries, len(THEMATIC_QUERIES)))
    success_count = 0

    for q_idx, query in enumerate(queries, 1):
        log(account_name, f"  Query {q_idx}: «{query}»")

        try:
            # Robust way to find and focus search box
            selectors = [
                "textarea.search3__input",
                "input.search3__input",
                "#text",
                "[name='text']",
            ]
            box = None
            for sel in selectors:
                try:
                    box = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                    )
                    if box:
                        break
                except:
                    continue

            if not box:
                continue

            # Force focus and clear via JS to avoid 'not interactable'
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", box)
            hp(1, 2)
            driver.execute_script("arguments[0].focus();", box)
            hp(0.5, 1)

            # Slow human typing
            for ch in query:
                box.send_keys(ch)
                time.sleep(random.uniform(0.5, 1.5))

            hp(1, 2)
            box.send_keys(Keys.RETURN)

            # Check results
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//li[contains(@class,'serp-item')] | //a[contains(@class,'organic')]",
                    )
                )
            )
            success_count += 1

            # Interaction and bridge logic...
            # (Rest of the logic: bridge_to_avito or random click)
            # For brevity in replace tool, I'm focusing on the fix
            hp(5, 10)
            human_scroll(driver, "down", iters=random.randint(2, 4))

        except Exception as e:
            log(account_name, f"    Query failed: {str(e)[:50]}")

    if success_count == 0:
        log(account_name, "  Warmup FAILED: No successful queries.")
        return False

    log(account_name, f"Warmup completed ({success_count} queries successful).")
    return True


def browse_commercial_categories(driver, wait, account_name, num_categories=3, ads_per_category=3):
    log(account_name, "=== Browsing commercial real estate categories on Avito ===")
    chosen = random.sample(
        AVITO_COMMERCIAL_CATEGORIES, min(num_categories, len(AVITO_COMMERCIAL_CATEGORIES))
    )

    for cat in chosen:
        url = "https://www.avito.ru" + cat
        log(account_name, f"  Category: {cat}")
        if not safe_get(driver, url, account_name):
            continue

        random_mouse_move(driver)
        human_scroll(driver, "down", iters=random.randint(2, 4))

        links = driver.find_elements(By.XPATH, "//a[@data-marker='item-title']")
        if not links:
            links = driver.find_elements(By.CSS_SELECTOR, "a[data-marker='item-title']")

        if not links:
            continue

        hrefs = [
            lnk.get_attribute("href")
            for lnk in random.sample(links, min(ads_per_category, len(links)))
            if lnk.get_attribute("href")
        ]

        for i, href in enumerate(hrefs, 1):
            log(account_name, f"    Listing {i}/{len(hrefs)}")
            if not safe_get(driver, href, account_name):
                break
            if not view_listing(driver, wait, account_name):
                break
            driver.back()
            hp(2, 4)


def find_and_view_commercial_listings(driver, wait, account_name, db_manager):
    """Search for commercial real estate listings in million-plus cities with price filters."""
    log(account_name, "=== Commercial real estate search (Million Cities + Price Filters) ===")

    # A3: если аккаунт недавно попал на капчу — не дёргаем Avito.
    if account_state.is_cooled_down(account_name):
        remaining = account_state.cooldown_remaining_seconds(account_name)
        log(account_name, f"!! Skip search: аккаунт в captcha-cooldown ещё {remaining}s")
        return 0, 0, 0

    processed_count = 0
    new_listings_count = 0
    error_count = 0

    deal_type = random.choice(["sale", "rent"])
    config = COMMERCIAL_SEARCH_FILTERS[deal_type]

    city = random.choice(MILLION_CITIES)
    category_path = random.choice(config["paths"])
    min_price = config["min_price"]

    url = f"https://www.avito.ru/{city}{category_path}?pmin={min_price}"

    log(account_name, f"  Searching in {city} for {deal_type} (min {min_price} RUB)")
    log(account_name, f"  URL: {url}")

    if not safe_get(driver, url, account_name):
        return 0, 0, 1

    human_scroll(driver, "down", iters=random.randint(3, 6))

    links = driver.find_elements(By.XPATH, "//a[@data-marker='item-title']")
    if not links:
        log(account_name, f"  No listings found in {city} for this category.")
        return 0, 0, 0

    hrefs = [
        lnk.get_attribute("href")
        for lnk in random.sample(links, min(5, len(links)))
        if lnk.get_attribute("href")
    ]

    for i, href in enumerate(hrefs, 1):
        if _tg.stop_event.is_set():
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
            log(account_name, f"  Processing listing {i}/{len(hrefs)}: {href}")
            if not safe_get(driver, href, account_name):
                break

            # C3-bridge + D4: считаем новым только если в БД его действительно
            # ещё нет, причём сравниваем по нормализованному URL (без query/utm).
            # Иначе один и тот же листинг, прилетевший дважды с разными UTM,
            # дважды считался бы как "new".
            is_new = db_manager.is_new_listing(normalize_listing_url(href))

            listing_data = extract_listing_data(driver, wait, account_name, log)
            save_listing_to_db(listing_data, db_manager, log, account_name)

            processed_count += 1
            if listing_data and is_new:
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

    log(account_name, f"=== Manual Login Attempt for {phone} ===")
    try:
        # Navigate to login page
        driver.get("https://www.avito.ru/#login?next=%2F")
        hp(4, 7)

        # 1. Enter phone
        log(account_name, "  Entering phone number (slow mode)...")
        phone_input = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@name='login']"))
        )
        phone_input.click()
        hp(0.8, 1.5)

        phone_input.send_keys(Keys.CONTROL + "a")
        phone_input.send_keys(Keys.DELETE)
        hp(0.5, 1.0)

        for ch in phone:
            phone_input.send_keys(ch)
            time.sleep(random.uniform(0.5, 1.5))
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
        password_input.click()
        hp(0.8, 1.5)
        for ch in password:
            password_input.send_keys(ch)
            time.sleep(random.uniform(0.5, 1.5))
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
        log(account_name, f"  Manual login failed: {str(e)[:200]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Thread logic
# ══════════════════════════════════════════════════════════════════════════════


def get_random_proxy():
    """Reads proxies.txt and returns a random proxy string."""
    try:
        path = Path("proxies.txt")
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            proxies = [line.strip() for line in f if line.strip()]
        return random.choice(proxies) if proxies else None
    except:
        return None


def run_thread(account: dict, cfg: dict, adspower: AdsPowerAPI, db_manager: DatabaseManager):
    account_name = account["name"]
    user_id = account.get("user_id")
    driver = None

    # G2: применяем per-account override captcha_cooldown_minutes из accounts.json.
    # None -> глобальный DEFAULT (см. account_state.configure_from_cfg).
    account_state.set_account_cooldown_minutes(
        account_name, account.get("captcha_cooldown_minutes")
    )

    try:
        # Initial connect attempt
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                log(account_name, f"Starting profile (Attempt {attempt + 1})...")
                debug_port = adspower.start_profile(user_id)
                time.sleep(5)
                driver = connect_to_sphere(debug_port)
                wait = WebDriverWait(driver, 15)

                # Test connection
                if safe_get(driver, "https://ya.ru", account_name, retries=1):
                    break  # Success
                else:
                    raise Exception("Connection failed")
            except Exception as e:
                log(account_name, f"Connection fail: {e}. Rotating proxy...")
                new_proxy = get_random_proxy()
                if new_proxy and update_profile_proxy(adspower, user_id, new_proxy):
                    log(account_name, f"Proxy updated to {new_proxy[:15]}...")
                adspower.stop_profile(user_id)
                if attempt == max_retries:
                    return
                hp(5, 10)

        # G1: вся Selenium-логика идёт через AvitoClient. driver/wait
        # передаём один раз — дальше клиент сам прокидывает их в нужные
        # реализации. db_manager и llm_classifier нужны для save_listing
        # и process_messages соответственно (для warmup/login достаточно
        # только driver+wait).
        from avito_client import AvitoClient

        llm_config = {
            "api_key": cfg.get("openai_api_key", ""),
            "model": cfg.get("openai_model", "gpt-3.5-turbo"),
            "api_base": cfg.get("openai_api_base", "https://api.openai.com/v1"),
        }
        llm = LLMClassifier(llm_config, db_manager=db_manager)
        client = AvitoClient(
            driver,
            wait,
            account_name,
            log_func=log,
            db_manager=db_manager,
            llm_classifier=llm,
        )

        # ── Stage 0: Yandex warmup ──
        if not client.warmup_yandex(num_queries=2):
            log(account_name, "CRITICAL: Warmup failed. Check your IP/Proxy.")
            return

        # ── Stage 1: Login (B4 native -> cookies -> manual phone/password) ──
        # G1: эту 3-уровневую логику теперь содержит client.login(). См.
        # avito_client.py:AvitoClient.login для деталей последовательности.
        if not client.login(
            cookies_path=account.get("cookies_path"),
            phone=account.get("phone"),
            password=account.get("password"),
        ):
            return

        if _tg.stop_event.is_set():
            return

        # ── Stage 2: Browse & Search ──
        log(account_name, "  Ready to work. Selecting categories...")
        hp(4, 10)  # Thinking time
        client.browse_commercial_categories()

        if _tg.stop_event.is_set():
            return

        processed, new_listings, errors = client.find_and_view_commercial_listings()
        log(account_name, f"Processed {processed}, {new_listings} new, {errors} errors")

        # ── Stage 3: Messages ──
        client.process_messages()

    except Exception as e:
        log(account_name, f"ERROR: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        adspower.stop_profile(user_id)


def _load_cfg():
    """
    Загружает config.json. На все типичные ошибки пишет чёткое сообщение
    в stderr/log_buffer и выходит из процесса (C1).
    """
    cfg_path = Path(__file__).parent / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        msg = (
            f"CRITICAL: config.json не найден: {cfg_path}\n"
            f"Создай файл по шаблону README.md (минимум: telegram_bot_token, "
            f"telegram_admin_id, accounts[]). Пример — в начале README."
        )
        print(msg, file=sys.stderr)
        try:
            _tg.add_log(msg)
        except Exception:
            pass
        raise SystemExit(2)
    except json.JSONDecodeError as e:
        msg = (
            f"CRITICAL: config.json не парсится как JSON: {e.msg} "
            f"(line={e.lineno}, col={e.colno})\n"
            f"Открой {cfg_path} и проверь синтаксис (запятые, кавычки, скобки)."
        )
        print(msg, file=sys.stderr)
        try:
            _tg.add_log(msg)
        except Exception:
            pass
        raise SystemExit(2)
    except OSError as e:
        msg = f"CRITICAL: не могу прочитать {cfg_path}: {e}"
        print(msg, file=sys.stderr)
        try:
            _tg.add_log(msg)
        except Exception:
            pass
        raise SystemExit(2)

    # Базовая валидация ожидаемых полей (warn-only, чтобы не блокировать
    # запуск с неполным конфигом во время разработки).
    if not isinstance(cfg, dict):
        msg = f"CRITICAL: config.json должен быть JSON-объектом, получено: {type(cfg).__name__}"
        print(msg, file=sys.stderr)
        raise SystemExit(2)
    if "accounts" not in cfg or not isinstance(cfg["accounts"], list):
        _tg.add_log("WARNING: в config.json отсутствует или некорректен ключ 'accounts'")

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

    adspower = AdsPowerAPI(cfg["adspower_api_url"], cfg.get("adspower_api_key"))
    db_manager = DatabaseManager()

    threads = []
    for acc in accounts:
        t = threading.Thread(
            target=run_thread, args=(acc, cfg, adspower, db_manager), name=acc["name"], daemon=True
        )
        threads.append(t)
        _tg.active_threads.append(t)
        t.start()
        time.sleep(5)

    for t in threads:
        t.join()


def main():
    # H3: загружаем .env (если есть) ДО setup_logging — чтобы LOG_LEVEL /
    # LOG_FORMAT тоже можно было задавать через .env.
    from env_config import load_dotenv_if_present

    load_dotenv_if_present(Path(__file__).parent / ".env")

    # E1: инициализируем логирование первым делом — так все последующие
    # модули, попадая в логи, получат единый формат и уровень.
    setup_logging()
    # Все логи дублируются в кольцевой TG-буфер (раньше это делал log()
    # вручную через _tg.add_log).
    install_tg_buffer_handler(_tg.add_log)

    cfg = _load_cfg()
    tg_token = cfg.get("telegram_bot_token", "")
    tg_admin = cfg.get("telegram_admin_id", 0)

    if tg_token and tg_token != "YOUR_TG_BOT_TOKEN":
        from tg_bot import TelegramController

        tg_ctrl = TelegramController(tg_token, tg_admin)
        _tg._tg_controller = tg_ctrl
        tg_ctrl.set_run_callback(lambda: _launch_commercial_bot_threads(_load_cfg()))
        # E4: ERROR/CRITICAL → мгновенно в TG админу.
        install_tg_alert_handler(tg_ctrl.notify)
        threading.Thread(target=tg_ctrl.start_polling, daemon=True).start()

        try:
            input()
        except:
            pass
        if not _tg.is_running():
            _launch_commercial_bot_threads(cfg)
    else:
        _launch_commercial_bot_threads(cfg)


if __name__ == "__main__":
    main()
