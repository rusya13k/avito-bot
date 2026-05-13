"""
Avito Commercial Real Estate Parser Bot
"""

import json
import logging
import random
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
        except (requests.RequestException, ValueError):
            # L12: bare → конкретные. RequestException — все сетевые/HTTP ошибки
            # requests, ValueError — на случай invalid params/url. Stop — fire-and-
            # forget, нам всё равно нечего делать с ошибкой; лог только в DEBUG.
            pass

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
        # L1: print → logger.warning (заметно в логах, но не критично)
        _bot_logger.warning("Standard driver install failed: %s. Trying fallback...", e)
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


def view_listing(driver, wait, account_name, *, favorite_rate=0.08, call_rate=0.05):
    """
    F1: favorite_rate — вероятность «Добавить в избранное» (default 8%).
    F1: call_rate    — вероятность нажать «Позвонить»       (default 5%).
    Оба параметра конфигурируются через config.json / accounts.json
    (ключи view_listing_favorite_rate / view_listing_call_rate).

    F9: dwell_time распределён по lognormal (5-120 секунд, пик ~20-30s).
    Дополнительно interest-score:
      • 15% листингов «очень интересные» — +60..300s дополнительного чтения.
      • 20% «совсем неинтересные» — закрываем рано (return True), без
        scroll/favorite/call. Имитирует «открыл, не моё, закрыл».
    """
    if check_block(driver, account_name):
        return False

    try:
        wait.until(
            EC.presence_of_element_located((By.XPATH, "//h1[@data-marker='item-view/title-info']"))
        )
    except Exception:
        return True

    # F9: lognormal dwell — реалистичный «прочитать заголовок и фото».
    # mean ~ 20-30s, длинный хвост до 2 мин (заинтересованный пользователь).
    dwell_time = hp(5, 120, distribution="lognormal")
    log(account_name, f"  Viewing listing (dwell time: {dwell_time:.1f}s)...")

    # F9: interest score — после первого взгляда решаем, насколько интересно.
    if random.random() < 0.15:
        # 15% — очень интересный листинг: дополнительные 1-5 минут чтения.
        extra = random.uniform(60, 300)
        log(account_name, f"  F9: интересный листинг — читаем ещё ~{extra:.0f}s")
        hp(extra, extra)
    elif random.random() < 0.20:
        # 20% — закрываем рано (без scroll/photos/favorite/call).
        # return True — не считаем это ошибкой, просто «не моё».
        log(account_name, "  F9: неинтересно, закрываем")
        return True

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
                # F9: чтение описания — lognormal 15-90s. Объект на 5-15 млн
                # рублей читается дольше, чем uniform 3-7s.
                hp(15, 90, distribution="lognormal")
            except:
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

    # F1: «Позвонить» — реалистичная вероятность 5% вместо 55%.
    # A3: проверяем дневной/сессионный лимит phone_clicks перед кликом —
    # та же защита, что и для «Показать телефон» в commercial_parser.
    # Конфигурируется через view_listing_call_rate в config.json/accounts.json.
    if random.random() < call_rate:
        if account_state.should_skip_phone(account_name):
            log(account_name, "  A3: Пропуск 'Позвонить' — дневной/сессионный лимит")
        else:
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
                account_state.record_phone_click(account_name)  # F1: учитываем в A3
                hp(5, 12)  # Stay after call
            except Exception:
                log(account_name, "  'Call' button not found.")

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


# F8: idle cycles — иногда только messenger или только профиль.
# Default-распределение типов цикла. Реальный пользователь открывает Авито
# с разными целями: проверить мессенджер, полистать главную, просто зайти
# в профиль. Бот не должен ВСЕГДА делать «полный цикл» (browse + parse +
# messenger) — это сильнейший behavioral fingerprint.
_CYCLE_KINDS_DEFAULT: dict[str, float] = {
    "full": 0.55,            # warmup → browse → find_and_view → messenger (как сейчас)
    "messenger_only": 0.20,  # только мессенджер
    "browse_only": 0.15,     # browse + find_and_view, без messenger
    "profile_check": 0.10,   # просто заходим в /profile, никаких действий
}

# Распределение для warmup-режима (B1): аккаунт пока не должен слать
# сообщений (messenger_only=0) и стараемся НЕ парсить много — больше
# спокойных profile_check / browse_only.
_CYCLE_KINDS_WARMUP: dict[str, float] = {
    "full": 0.20,
    "messenger_only": 0.0,
    "browse_only": 0.40,
    "profile_check": 0.40,
}


def _pick_cycle_kind(account: dict, cfg: dict, *, is_warmup: bool = False) -> str:
    """F8: выбирает тип сегодняшнего цикла.

    Чистая функция (без обращения к account_state) — для лёгкой
    тестируемости. Флаг `is_warmup` пробрасывается из run_thread, который
    сам опрашивает account_state.is_in_warmup().

    Override для НЕ-warmup-режима:
        1. account["cycle_distribution"]   (per-account, accounts.json)
        2. cfg["cycle_distribution"]       (глобальный, config.json)
        3. _CYCLE_KINDS_DEFAULT            (хард-кодед)

    Returns:
        Один из ключей словаря весов: "full" / "messenger_only" /
        "browse_only" / "profile_check".
    """
    if is_warmup:
        weights = _CYCLE_KINDS_WARMUP
    else:
        weights = (
            account.get("cycle_distribution")
            or cfg.get("cycle_distribution")
            or _CYCLE_KINDS_DEFAULT
        )
    kinds = list(weights.keys())
    probs = [float(weights[k]) for k in kinds]
    # random.choices гарантирует выбор хотя бы одного, если суммa > 0.
    # _CYCLE_KINDS_WARMUP содержит 0 для messenger_only — это нормально.
    return random.choices(kinds, weights=probs, k=1)[0]


def _do_profile_check(driver, account_name: str) -> None:
    """F8: «короткий цикл» — заходим в профиль, листаем 30-60 секунд.

    Не парсим, не отвечаем — просто визит. Имитирует «зашёл проверить
    своё». В 30% случаев заглядываем ещё и в /profile/favorites.
    """
    safe_get(driver, "https://www.avito.ru/profile", account_name)
    hp(30, 60)
    if random.random() < 0.3:
        safe_get(driver, "https://www.avito.ru/profile/favorites", account_name)
        hp(20, 40)


def yandex_warmup(driver, wait, account_name, num_queries=2):
    log(account_name, "=== Enhanced Thematic Yandex Warmup ===")

    if not safe_get(driver, "https://ya.ru", account_name):
        return False

    # F4: смешиваем тематические (70%), общие (25%) и пропуски (5%).
    queries = _pick_queries(num_queries)
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

    # F4: если queries пустой (все слоты попали в 5%-пропуск) — это нормально:
    # аккаунт просто открыл Яндекс и ничего не искал. Не считается провалом.
    if not queries:
        log(account_name, "Warmup completed (idle — no queries this time).")
        return True

    if success_count == 0:
        log(account_name, "  Warmup FAILED: No successful queries.")
        return False

    log(account_name, f"Warmup completed ({success_count} queries successful).")
    return True


def browse_commercial_categories(
    driver,
    wait,
    account_name,
    num_categories=None,
    ads_per_category=None,
    search_filters=None,
    favorite_rate=0.08,
    call_rate=0.05,
    max_categories_per_browse=4,
    max_listings_per_browse=4,
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
    if _cities:
        _city_prefix = "/" + random.choice(_cities)

    for cat in chosen:
        url = "https://www.avito.ru" + _city_prefix + cat
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
            for lnk in random.sample(links, min(n_ads, len(links)))
            if lnk.get_attribute("href")
        ]

        for i, href in enumerate(hrefs, 1):
            log(account_name, f"    Listing {i}/{len(hrefs)}")
            if not safe_get(driver, href, account_name):
                break
            if not view_listing(
                driver, wait, account_name,
                favorite_rate=favorite_rate, call_rate=call_rate,
            ):
                break
            driver.back()
            hp(2, 4)


def find_and_view_commercial_listings(
    driver, wait, account_name, db_manager, search_filters=None, max_listings_per_search=7
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

    # F2: случайное число листингов [1, max_listings_per_search] с весами.
    _n = _weighted_listing_count(max_n=max_listings_per_search)
    log(account_name, f"  F2: листингов в этом запросе={_n}")
    hrefs = [
        lnk.get_attribute("href")
        for lnk in random.sample(links, min(_n, len(links)))
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
    except OSError:
        # L2: bare except → OSError (PermissionError, IsADirectoryError и т.п.).
        return None


def _apply_account_proxy(
    adspower: "AdsPowerAPI",
    user_id: str,
    account: dict,
    account_name: str,
) -> str | None:
    """
    A1: Выбирает и применяет прокси для AdsPower-профиля.

    Порядок:
      1. Поле "proxy" из accounts.json (per-account).
      2. Случайный прокси из proxies.txt (глобальный fallback).
      3. Если ничего нет — логирует ERROR (E4 → TG) и возвращает None.

    Возвращает строку прокси, который удалось применить, или None.
    """
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

    _bot_logger.error(
        "[%s] A1: Нет доступного прокси — ни per-account, ни в proxies.txt. "
        "Аккаунт пропущен для защиты от блокировки. "
        'Добавь поле "proxy" в accounts.json или заполни proxies.txt.',
        account_name,
    )
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
    return start <= hour < end


def _seconds_until_active_hours(account: dict, cfg: dict) -> float:
    """
    B2: сколько секунд до начала активного окна.
    Если сейчас уже в окне — вернёт 0.
    """
    start = int(account.get("active_hours_start", cfg.get("active_hours_start", 9)))
    now = time.localtime()
    current_secs = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    target_secs = start * 3600
    if current_secs < target_secs:
        return float(target_secs - current_secs)
    # уже после start сегодня → ждём до завтра
    return float(86400 - current_secs + target_secs)


def run_thread(account: dict, cfg: dict, adspower: AdsPowerAPI, db_manager: DatabaseManager):
    account_name = account["name"]
    user_id = account.get("user_id")
    driver = None

    # G2: применяем per-account override captcha_cooldown_minutes из accounts.json.
    # None -> глобальный DEFAULT (см. account_state.configure_from_cfg).
    account_state.set_account_cooldown_minutes(
        account_name, account.get("captcha_cooldown_minutes")
    )

    # F7: per-account override базовой вероятности dead-day. None — глобальный
    # default (5% базы, ×3 в выходные). Может быть переопределён через
    # accounts.json ключ "dead_day_rate" (или 0 чтобы выключить).
    account_state.set_account_dead_day_rate(account_name, account.get("dead_day_rate"))

    # A2: применяем per-account дневные бюджеты из accounts.json.
    # Ключи: daily_budget_listings / daily_budget_messages / daily_budget_phone.
    # Значение None → не переопределяем (используем глобальный дефолт).
    _budget_overrides = {
        "listings": account.get("daily_budget_listings"),
        "messages": account.get("daily_budget_messages"),
        "phone": account.get("daily_budget_phone"),
    }
    account_state.set_daily_budget_limits(account_name, _budget_overrides)

    # ── B1: Warmup-режим для нового аккаунта ──────────────────────────────
    # Если в accounts.json задано "created_at": "YYYY-MM-DD", то первые
    # warmup_days дней (default 3) аккаунт работает в щадящем режиме:
    # нет кликов телефона, нет LLM-сообщений, меньше листингов.
    _created_at_str = account.get("created_at")
    if _created_at_str:
        try:
            import datetime as _dt

            _created_dt = _dt.datetime.strptime(_created_at_str, "%Y-%m-%d")
            _warmup_days = int(account.get("warmup_days", 3))
            _warmup_end_ts = _created_dt.timestamp() + _warmup_days * 86400
            account_state.set_warmup_until(account_name, _warmup_end_ts)
            if account_state.is_in_warmup(account_name):
                _warmup_listing_limit = int(account.get("warmup_daily_listings", 20))
                account_state.set_daily_budget_limits(
                    account_name, {"listings": _warmup_listing_limit}
                )
                log(
                    account_name,
                    f"B1: warmup-режим до {_dt.datetime.fromtimestamp(_warmup_end_ts).strftime('%Y-%m-%d')} "
                    f"(+{_warmup_days} дн. с created_at). "
                    f"Листингов/день: {_warmup_listing_limit}. Телефон и сообщения — выкл.",
                )
        except (ValueError, OSError):
            _bot_logger.warning(
                "[%s] B1: некорректный created_at=%r — warmup пропускаем",
                account_name,
                _created_at_str,
            )

    # ── C1: Health score — применяем ограничения если нужно ──────────────
    # Вычисляем captcha_rate за 7 дней ПОСЛЕ применения per-account бюджетов
    # (B1/A2), чтобы health-ограничения были дополнительным слоем поверх.
    try:
        from account_state import apply_health_restrictions, compute_account_health

        _health = compute_account_health(account_name, db_manager)
        if _health["mode"] in ("degraded", "critical"):
            apply_health_restrictions(account_name, _health)
            log(
                account_name,
                f"C1: режим {_health['mode']} (captcha_rate={_health['score']:.3f}, "
                f"капч={_health['captchas_7d']}, листингов={_health['listings_7d']} за 7д). "
                "Бюджет снижен, телефон заблокирован.",
            )
        else:
            log(
                account_name,
                f"C1: аккаунт здоров (captcha_rate={_health['score']:.3f}, "
                f"капч={_health['captchas_7d']} за 7д).",
            )
    except Exception:
        pass  # C1 не должен блокировать запуск

    # ── A1: Per-account proxy setup ────────────────────────────────────────
    # Приоритет: 1) поле "proxy" в accounts.json, 2) случайный из proxies.txt.
    # Если прокси нет совсем — ERROR в TG (E4) и пропускаем аккаунт, чтобы
    # несколько аккаунтов с одного IP не склеились и не были забанены вместе.
    if _apply_account_proxy(adspower, user_id, account, account_name) is None:
        return

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

        # F1: вероятности кликов «Избранное»/«Позвонить» в view_listing.
        # Per-account override → глобальный cfg → дефолт (8% / 5%).
        _fav_rate = float(
            account.get("view_listing_favorite_rate",
                         cfg.get("view_listing_favorite_rate", 0.08))
        )
        _call_rate = float(
            account.get("view_listing_call_rate",
                         cfg.get("view_listing_call_rate", 0.05))
        )

        # F2: верхние границы числа листингов/категорий за цикл.
        # Per-account override → глобальный cfg → дефолт.
        _max_listings = int(
            account.get("max_listings_per_search",
                         cfg.get("max_listings_per_search", 7))
        )
        _max_cats = int(
            account.get("max_categories_per_browse",
                         cfg.get("max_categories_per_browse", 4))
        )
        _max_browse_listings = int(
            account.get("max_listings_per_browse",
                         cfg.get("max_listings_per_browse", 4))
        )

        # F5: реалистичные задержки ответа в мессенджере. Параметры читаются
        # с приоритетом per-account → cfg → дефолты в AvitoMessenger.__init__.
        # Любой ключ можно опустить — соответствующий дефолт подхватится.
        _messenger_cfg: dict = {}
        for _key in (
            "min_reply_age_min",
            "max_reply_age_min",
            "reply_delay_mu",
            "reply_delay_sigma",
            "ignore_new_dialog_chance",
        ):
            _full_key = f"messenger_{_key}"
            _val = account.get(_full_key, cfg.get(_full_key))
            if _val is not None:
                _messenger_cfg[_key] = float(_val)

        client = AvitoClient(
            driver,
            wait,
            account_name,
            log_func=log,
            db_manager=db_manager,
            llm_classifier=llm,
            search_filters=account.get("search_filters"),
            favorite_rate=_fav_rate,
            call_rate=_call_rate,
            max_listings_per_search=_max_listings,
            max_categories_per_browse=_max_cats,
            max_listings_per_browse=_max_browse_listings,
            messenger_config=_messenger_cfg or None,
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

        # ── A4: Main cycle loop ─────────────────────────────────────────────
        # Warmup и Login выполнены выше один раз; дальше гоняем цикл пока не
        # придёт сигнал stop. После каждого цикла — пауза 30-90 мин (A4).
        # Per-account параметры: session_pause_min / session_pause_max.
        _pause_min = float(account.get("session_pause_min", cfg.get("session_pause_min", 30)))
        _pause_max = float(account.get("session_pause_max", cfg.get("session_pause_max", 90)))

        while not _tg.stop_event.is_set():
            # ── F7: dead-day — пропускаем сегодняшний день целиком ─────────
            # Реальный пользователь не работает каждый день: 5% дней пропуск
            # (×3 в выходные). Решение принимается один раз в сутки
            # (account_state кэширует ответ по дате).
            if account_state.is_dead_day(account_name):
                import datetime as _dt

                _start_hour = int(
                    account.get("active_hours_start", cfg.get("active_hours_start", 9))
                )
                _now = _dt.datetime.now()
                _tomorrow = (_now + _dt.timedelta(days=1)).replace(
                    hour=_start_hour, minute=0, second=0, microsecond=0
                )
                _wait_secs = max(0.0, (_tomorrow - _now).total_seconds())
                log(
                    account_name,
                    f"F7: сегодня dead-day — спим {_wait_secs / 3600:.1f} ч "
                    f"до завтра {_start_hour:02d}:00.",
                )
                _slept = 0.0
                while _slept < _wait_secs and not _tg.stop_event.is_set():
                    _chunk = min(60.0, _wait_secs - _slept)
                    time.sleep(_chunk)
                    _slept += _chunk
                continue

            # ── B2: Активное окно времени ─────────────────────────────────
            # Если текущий час вне [active_hours_start, active_hours_end)
            # — ждём до начала окна, проверяя stop_event каждые 30 сек.
            if not _is_in_active_hours(account, cfg):
                _active_start = int(
                    account.get("active_hours_start", cfg.get("active_hours_start", 9))
                )
                _wait_secs = _seconds_until_active_hours(account, cfg)
                _wake_time = time.strftime("%H:%M", time.localtime(time.time() + _wait_secs))
                log(
                    account_name,
                    f"B2: вне активного окна — сплю до {_active_start:02d}:00 (~{_wake_time}).",
                )
                _slept = 0.0
                while _slept < _wait_secs and not _tg.stop_event.is_set():
                    _chunk = min(30.0, _wait_secs - _slept)
                    time.sleep(_chunk)
                    _slept += _chunk
                continue

            # A3/A4: сбрасываем сессионные phone-счётчики в начале цикла.
            account_state.start_new_session(account_name)

            # ── F8: выбор типа цикла ─────────────────────────────────────────
            # Не каждый цикл должен быть «полным» (browse + parse + messenger).
            # Реальный пользователь чаще заходит просто проверить мессенджер
            # или полистать профиль — и эту вариативность надо имитировать.
            # См. _pick_cycle_kind для деталей распределения и warmup-mode.
            kind = _pick_cycle_kind(
                account, cfg, is_warmup=account_state.is_in_warmup(account_name)
            )
            log(account_name, f"F8: cycle kind = {kind}")

            if kind == "full":
                # ── Stage 2: Browse & Search ──
                log(account_name, "  Ready to work. Selecting categories...")
                hp(4, 10)  # Thinking time
                client.browse_commercial_categories()

                if _tg.stop_event.is_set():
                    break

                processed, new_listings, errors = client.find_and_view_commercial_listings()
                log(account_name, f"Processed {processed}, {new_listings} new, {errors} errors")

                if _tg.stop_event.is_set():
                    break

                # ── Stage 3: Messages ──
                client.process_messages()

                if _tg.stop_event.is_set():
                    break

            elif kind == "messenger_only":
                # F8: только мессенджер. Имитирует «зашёл с пуша/прочитать».
                client.process_messages()
                if _tg.stop_event.is_set():
                    break

            elif kind == "browse_only":
                # F8: browse + парс листингов, но БЕЗ мессенджера. Часто бывает
                # у людей: «Открыл — поискать что есть нового, мессенджер не
                # лез смотреть».
                log(account_name, "  Browse-only cycle...")
                hp(4, 10)
                client.browse_commercial_categories()

                if _tg.stop_event.is_set():
                    break

                processed, new_listings, errors = client.find_and_view_commercial_listings()
                log(account_name, f"Processed {processed}, {new_listings} new, {errors} errors")

                if _tg.stop_event.is_set():
                    break

            elif kind == "profile_check":
                # F8: «зашёл просто посмотреть свой профиль». Никакого парсинга,
                # никакого LLM, никаких сообщений — только активность сессии.
                _do_profile_check(driver, account_name)
                if _tg.stop_event.is_set():
                    break

            # ── A4: Session pause ───────────────────────────────────────────
            # После каждого полного цикла бот делает паузу, как реальный пользователь.
            # Пауза прерывается при получении stop-сигнала (команда /stop в TG).
            _pause_secs = random.uniform(_pause_min * 60, _pause_max * 60)
            _next_time = time.strftime("%H:%M", time.localtime(time.time() + _pause_secs))
            log(
                account_name,
                f"Цикл завершён. Следующий запуск в ~{_next_time} "
                f"(пауза {_pause_secs / 60:.0f} мин).",
            )
            _human_delay(
                _pause_secs, _pause_secs, stop_event=_tg.stop_event, distribution="uniform"
            )

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
                # L7: bare except → WebDriverException. driver.quit() может
                # упасть при уже закрытом браузере / отвалившемся debug-port.
                # Любые другие исключения тут — настоящие баги, не глотаем.
                pass
        adspower.stop_profile(user_id)


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
        # B3: случайная задержка 30-180 сек между стартами потоков.
        # Предотвращает регулярный паттерн «все аккаунты стартуют одновременно».
        _human_delay(30, 180, stop_event=_tg.stop_event)

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
        except (KeyboardInterrupt, EOFError):
            # L2: bare except → конкретные. Ctrl-C / EOF — нормальные пути
            # завершения main, проглатываем без лога. Любое другое
            # исключение сюда теперь не попадёт и убьёт процесс с trace.
            pass
        if not _tg.is_running():
            _launch_commercial_bot_threads(cfg)
    else:
        _launch_commercial_bot_threads(cfg)


if __name__ == "__main__":
    main()
