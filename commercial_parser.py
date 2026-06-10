import logging
import re
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# E1: модульный logger. Раньше парсер писал только через переданный
# log_func (account-aware строки в TG-буфер). Теперь критичные ошибки —
# через logger.exception, чтобы алерт-хендлер их увидел.
logger = logging.getLogger(__name__)

# Module for parsing commercial real estate listings from Avito

# A3 (удалён): _try_show_phone, флаги _CAPTCHA_FLAG/_PHONE_CLICKED_FLAG
# и метрика phone_clicks — убраны по решению Бакугана. Телефон всегда None.


def _wf(driver, xpath, timeout=3):
    """
    C5: короткий WebDriverWait-обёртка вместо голого driver.find_element.
    Без неё страница не успевает прогрузиться -> мы попадаем в except
    и теряем реальные данные.
    """
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))


def normalize_phone(phone_text):
    """
    D3: нормализация в E.164 (+XXXXXXXXXXX) с защитой от мусора.
    Поддерживает номера РФ, СНГ и международные форматы.
    """
    if not phone_text:
        return None

    # Запоминаем, был ли явный плюс в начале
    has_plus = phone_text.strip().startswith("+")
    digits = re.sub(r"\D", "", phone_text)

    if not digits:
        return None

    length = len(digits)

    if has_plus:
        # Явный международный формат: просто проверяем длину (обычно 10-15 цифр)
        if 10 <= length <= 15:
            return f"+{digits}"
        return None

    # Без плюса: пытаемся нормализовать локальные форматы
    if length == 11 and digits.startswith("8"):
        # Локальный 8-800 или 8-9XX -> меняем на 7
        digits = "7" + digits[1:]
    elif length == 10:
        # Локальный 10-значный без 8/7 в начале -> предполагаем РФ (+7)
        digits = "7" + digits
    elif length < 10 or length > 15:
        # Слишком короткий или длинный номер
        return None

    return f"+{digits}"


# D3: regex для выгрести все номера из произвольного текста (описание).
# Покрывает форматы: +7..., 8..., (495) ..., с разделителями любыми.
# Минимум 10 цифр в кандидате (после удаления всего нецифрового).
_PHONE_RE = re.compile(
    r"(?:\+?[78][\d\s\-\(\)]{8,}\d)",
    flags=re.UNICODE,
)


def extract_phones_from_text(text):
    """
    D3: достаёт все валидные российские номера из текста в виде
    отсортированного списка уникальных E.164-строк. Используется для
    обогащения phones из описания листинга — продавцы часто пишут
    номер прямо в тексте, а не открывают через "Показать телефон".

    Возвращает list[str].
    """
    if not text:
        return []
    found = set()
    for match in _PHONE_RE.finditer(text):
        normalized = normalize_phone(match.group(0))
        if normalized:
            found.add(normalized)
    return sorted(found)


def normalize_listing_url(url: str) -> str:
    """
    D4: канонический URL листинга — убираем query/fragment, lowercase
    схему/хост, убираем trailing slash. Это нужно для дедупликации:
    один и тот же листинг приходит к нам с разными UTM-метками
    (?utm_source=fb, ?slocation_id=...), и без нормализации создаются
    дубликаты в БД.

    Avito-URL уникальны по item_id в path (последний сегмент с числом),
    поэтому query-параметры можно безопасно отбрасывать.
    """
    if not url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit

        sp = urlsplit(url.strip())
        scheme = (sp.scheme or "https").lower()
        host = (sp.netloc or "").lower()
        path = sp.path.rstrip("/") or "/"
        return urlunsplit((scheme, host, path, "", ""))
    except Exception:
        return url


# ─────────────────────────────────────────────────────────────────────────────
# S3: extract_listing_data декомпозиция. Раньше функция занимала 213 строк
# одним блоком, в котором перепутаны pure-data, Selenium-парсинг, A3-флоу
# с кликом телефона и пост-обработка. Сейчас она — ~35-строчный оркестратор;
# каждая ответственность вынесена в свой helper. Логика идентична оригиналу.
# ─────────────────────────────────────────────────────────────────────────────


def _extract_title(wait) -> str:
    """Заголовок объявления из <h1>. Sentinel "Неизвестно" если не нашли —
    save_listing_to_db потом замаппит его обратно в None через _nil_if.
    """
    try:
        title_elem = wait.until(
            EC.presence_of_element_located((By.XPATH, "//h1[@data-marker='item-view/title-info']"))
        )
        return title_elem.text
    except TimeoutException:
        return "Неизвестно"


def _extract_price(driver) -> float:
    """Цена из item-view/item-price. C5: до 3s ждём, страница может догружаться.
    Возвращаем 0.0 sentinel — _nil_if маппит в None при сохранении в БД.
    """
    try:
        price_elem = _wf(driver, "//span[@data-marker='item-view/item-price']")
        price_text = price_elem.text.replace("₽", "").replace(" ", "").replace("\n", "")
        return float(re.sub(r"[^\d,]", "", price_text).replace(",", "."))
    except Exception:
        return 0.0


def _extract_description(driver) -> str:
    """Текст описания из item-view/item-description. Пустая строка если нет."""
    try:
        desc_elem = _wf(driver, "//div[@data-marker='item-view/item-description']")
        return desc_elem.text
    except Exception:
        return ""


def _extract_location(driver) -> str:
    """Локация: пробуем несколько стратегий — geo-span рядом с картой,
    breadcrumbs, или координаты из item-map-wrapper. Sentinel "Неизвестно".
    """
    try:
        # Стратегия 1: span с адресом рядом с картой (стабильный паттерн)
        map_wrapper = driver.find_elements(By.XPATH, "//*[@data-marker='item-map-wrapper']")
        if map_wrapper:
            # Адрес обычно в предыдущем sibling-блоке карты
            addr_spans = driver.find_elements(
                By.XPATH,
                "//*[@data-marker='item-map-wrapper']/preceding::span[contains(@class, 'geo')]"
                " | //*[@data-marker='item-map-wrapper']/..//span[string-length(text()) > 5]",
            )
            for span in addr_spans:
                text = span.text.strip()
                if (
                    text
                    and len(text) > 5
                    and ("," in text or "ул" in text.lower() or "пр" in text.lower())
                ):
                    return text
        # Стратегия 2: itemprop address
        addr_elem = driver.find_elements(By.XPATH, "//*[@itemprop='address']")
        if addr_elem:
            return addr_elem[0].text.strip() or "Неизвестно"
        # Стратегия 3: breadcrumbs последний элемент (город/район)
        crumbs = driver.find_elements(
            By.XPATH, "//div[@data-marker='breadcrumbs']//span[@itemprop='name']"
        )
        if crumbs:
            return crumbs[-1].text.strip() or "Неизвестно"
        return "Неизвестно"
    except Exception:
        return "Неизвестно"


def _extract_area(title: str, description: str) -> float:
    """Площадь в кв.м из заголовка/описания (regex). Pure function — не
    лезет в Selenium. Возвращает 0.0 если не найдено.
    """
    try:
        full_text = title.lower() + " " + description.lower()
        area_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:кв\.?\s*м|м2)", full_text)
        if area_match:
            return float(area_match.group(1))
        return 0.0
    except Exception:
        return 0.0


def _extract_category(driver, title: str) -> str:
    """Категория объявления — определяется по URL или тексту title.
    Маппинг: офис/торгов/склад/производств/бизнес → конкретная категория,
    иначе общий "коммерческая недвижимость".
    """
    try:
        url = driver.current_url
        title_lower = title.lower()
        if "офис" in url or "офис" in title_lower:
            return "офисные помещения"
        if "торгов" in url or "торгов" in title_lower:
            return "торговые помещения"
        if "склад" in url or "склад" in title_lower:
            return "склады"
        if "производств" in url or "производств" in title_lower:
            return "производственные помещения"
        if "бизнес" in url or "бизнес" in title_lower:
            return "готовый бизнес"
        return "коммерческая недвижимость"
    except Exception:
        return "коммерческая недвижимость"


def _extract_seller_info(driver) -> dict:
    """Информация о продавце: seller_name + profile_url + profile_id +
    active_listings_count. Возвращает dict, готовый для merge в listing_data.

    Новая вёрстка Avito (2025+): нет data-marker для seller-info.
    Имя продавца — в span с title внутри блока item-view/item-view-contacts.
    Ссылка на профиль — ближайший <a> с href содержащим /user/ или /brands/.
    """
    try:
        # Стратегия 1: meta-тег vk:seller_name (самый стабильный)
        seller_name = "Неизвестно"
        meta_seller = driver.find_elements(By.XPATH, "//meta[@property='vk:seller_name']")
        if meta_seller:
            seller_name = meta_seller[0].get_attribute("content") or "Неизвестно"
        else:
            # Стратегия 2: span с title внутри contacts-блока
            contacts_block = driver.find_elements(
                By.XPATH, "//*[@data-marker='item-view/item-view-contacts']"
            )
            if contacts_block:
                name_spans = contacts_block[0].find_elements(By.XPATH, ".//span[@title]")
                for span in name_spans:
                    title = span.get_attribute("title") or ""
                    if title and len(title) > 1:
                        seller_name = title
                        break

        # Ссылка на профиль
        profile_url = ""
        profile_id = "unknown"
        profile_links = driver.find_elements(
            By.XPATH,
            "//*[@data-marker='item-view/item-view-contacts']"
            "//a[contains(@href,'/user/') or contains(@href,'/brands/')]",
        )
        if profile_links:
            profile_url = profile_links[0].get_attribute("href") or ""
            profile_id_match = re.search(r"/user/([^/?]+)", profile_url)
            profile_id = profile_id_match.group(1) if profile_id_match else "unknown"

        return {
            "seller_name": seller_name,
            "profile_url": profile_url,
            "profile_id": profile_id,
            "active_listings_count": 0,
        }
    except Exception:
        return {
            "seller_name": "Неизвестно",
            "profile_url": "",
            "profile_id": "unknown",
            "active_listings_count": 0,
        }


def _extract_publication_date(driver) -> str:
    """Дата публикации из item-view/item-date. Пустая строка если нет."""
    try:
        date_elem = _wf(driver, "//*[@data-marker='item-view/item-date']", timeout=1)
        return date_elem.text
    except Exception:
        return ""


def _extract_photos(driver, max_n: int = 3) -> list[str]:
    """Первые `max_n` URL фото из image-preview/item или image-frame/image-wrapper. [] если нет."""
    try:
        # Новая вёрстка: превью-лента
        photo_elements = driver.find_elements(
            By.XPATH,
            "//ul[@data-marker='image-preview/preview-wrapper']"
            "//li[@data-marker='image-preview/item']//img",
        )
        if not photo_elements:
            # Fallback: основная галерея
            photo_elements = driver.find_elements(
                By.XPATH, "//*[@data-marker='image-frame/image-wrapper']//img"
            )
        urls = []
        for p in photo_elements[:max_n]:
            src = p.get_attribute("src") or p.get_attribute("data-src") or ""
            if src and "avito" in src:
                urls.append(src)
        return urls
    except Exception:
        return []


def _visit_seller_profile(driver, listing_data: dict, log_func, account_name: str) -> None:
    """Открывает профиль продавца в новой вкладке, считает активные объявления
    и количество похожих (в той же категории). Мутирует listing_data:
      - active_listings_count: общее число активных объявлений продавца
      - similar_listings_count: число объявлений в той же категории (недвижимость)

    Работает через новую вкладку — текущая страница объявления не теряется.
    При любой ошибке — молча возвращает, не ломая основной парсинг.
    """
    profile_url = listing_data.get("profile_url", "")
    if not profile_url:
        return

    original_window = driver.current_window_handle
    try:
        # Снимаем snapshot handles ДО открытия — чтобы точно найти новую вкладку.
        handles_before = set(driver.window_handles)
        # Открываем профиль в новой вкладке
        driver.execute_script("window.open(arguments[0], '_blank');", profile_url)
        # Ждём появления новой вкладки (до 3 сек)
        new_handle = None
        for _ in range(6):
            diff = set(driver.window_handles) - handles_before
            if diff:
                new_handle = diff.pop()
                break
            time.sleep(0.5)
        if not new_handle:
            return
        driver.switch_to.window(new_handle)

        # Ждём загрузки страницы профиля (до 8 сек)
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//div[@data-marker='profile-item']"
                        " | //*[contains(@data-marker,'item')]//a[@data-marker='item-title']"
                        " | //*[@data-marker='profile/summary']",
                    )
                )
            )
        except TimeoutException:
            # Страница не загрузилась — закрываем и уходим
            driver.close()
            driver.switch_to.window(original_window)
            return

        # Парсим количество объявлений
        _parse_seller_profile_listings(driver, listing_data, log_func, account_name)

    except Exception as e:
        log_func(account_name, f"Ошибка визита в профиль продавца: {e}")
    finally:
        # Гарантированно закрываем вкладку и возвращаемся
        try:
            current_handles = driver.window_handles
            if original_window in current_handles:
                if driver.current_window_handle != original_window:
                    driver.close()
                    driver.switch_to.window(original_window)
            else:
                # Оригинальное окно пропало — переключаемся на первое доступное
                log_func(
                    account_name,
                    "Оригинальное окно пропало, переключаюсь на первое доступное",
                )
                if current_handles:
                    driver.switch_to.window(current_handles[0])
        except Exception:
            # Последний fallback — пробуем вернуться
            try:
                driver.switch_to.window(original_window)
            except Exception:
                pass


def _parse_seller_profile_listings(driver, listing_data: dict, log_func, account_name: str) -> None:
    """Парсит страницу профиля продавца. Считает общее количество объявлений
    и количество в категории недвижимости.

    Авито профиль продавца показывает:
    - Список объявлений (item-title ссылки)
    - Счётчик "N объявлений" в шапке
    - Категории/табы с числами
    """
    # Стратегия 1: ищем счётчик объявлений в шапке профиля
    total_count = 0
    try:
        # Текст вида "42 объявления" или "Объявления (42)"
        counter_elems = driver.find_elements(
            By.XPATH,
            "//*[contains(text(),'объявлен')] | //*[contains(text(),'Объявлен')]",
        )
        for elem in counter_elems:
            text = elem.text.strip()
            match = re.search(r"(\d+)", text)
            if match:
                total_count = max(total_count, int(match.group(1)))
                break
    except Exception:
        pass

    # Стратегия 2: если счётчик не нашли — считаем карточки на странице
    if total_count == 0:
        try:
            items = driver.find_elements(
                By.XPATH,
                "//a[@data-marker='item-title'] | //*[@data-marker='profile-item']",
            )
            total_count = len(items)
        except Exception:
            pass

    listing_data["active_listings_count"] = total_count

    # Считаем похожие (в категории недвижимость)
    similar_count = 0
    try:
        items = driver.find_elements(By.XPATH, "//a[@data-marker='item-title']")
        realty_keywords = [
            "аренд",
            "помещен",
            "офис",
            "склад",
            "торгов",
            "коммерч",
            "недвижим",
            "м²",
            "кв.м",
            "этаж",
        ]
        for item in items:
            title_text = (item.text or "").lower()
            href = (item.get_attribute("href") or "").lower()
            combined = title_text + " " + href
            if any(kw in combined for kw in realty_keywords):
                similar_count += 1
    except Exception:
        pass

    listing_data["similar_listings_count"] = similar_count

    log_func(
        account_name,
        f"Профиль продавца: {total_count} объявлений, {similar_count} похожих (недвижимость)",
    )


def _extract_item_id(driver) -> str:
    """ID объявления из item-view/item-id. Пустая строка если нет."""
    try:
        id_elem = _wf(driver, "//*[@data-marker='item-view/item-id']", timeout=1)
        text = id_elem.text.replace("№", "").strip()
        return re.sub(r"[^\d]", "", text)
    except Exception:
        return ""


def _extract_views(driver) -> int:
    """Общее количество просмотров из item-view/total-views. 0 если нет."""
    try:
        views_elem = _wf(driver, "//*[@data-marker='item-view/total-views']", timeout=1)
        match = re.search(r"\d+", views_elem.text.replace(" ", ""))
        return int(match.group()) if match else 0
    except Exception:
        return 0


def _extract_params(driver) -> dict:
    """Параметры помещения из item-view/item-params. {} если нет.
    Возвращает dict вида {"Площадь": "18 м²", "Этаж": "1 из 5", ...}.
    """
    try:
        params_block = driver.find_elements(
            By.XPATH, "//*[@data-marker='item-view/item-params']//li"
        )
        result = {}
        for li in params_block:
            spans = li.find_elements(By.XPATH, ".//span")
            if spans:
                key = spans[0].text.strip().rstrip(":")
                # Значение — текст li минус текст первого span
                full_text = li.text.strip()
                value = full_text.replace(spans[0].text, "").strip().lstrip(":")
                if key:
                    result[key] = value
        return result
    except Exception:
        return {}


def _extract_coordinates(driver) -> dict:
    """Координаты из item-map-wrapper data-map-lat/lon. {} если нет."""
    try:
        map_el = driver.find_elements(By.XPATH, "//*[@data-marker='item-map-wrapper']")
        if map_el:
            lat = map_el[0].get_attribute("data-map-lat")
            lon = map_el[0].get_attribute("data-map-lon")
            if lat and lon:
                return {"lat": float(lat), "lon": float(lon)}
        return {}
    except Exception:
        return {}


def _enrich_phones_from_description(listing_data: dict) -> None:
    """D3: собираем все номера, упомянутые в описании. Продавцы часто
    дублируют контакт прямо в тексте. Объединяем с phone из «Показать
    телефон». Дедуп через set + сортировка. Если основное phone пустое —
    подставляем первый из описания.

    Мутирует listing_data["phones"] и listing_data["phone"].
    """
    phones_from_desc = extract_phones_from_text(listing_data.get("description"))
    all_phones = set(phones_from_desc)
    if listing_data.get("phone"):
        all_phones.add(listing_data["phone"])
    listing_data["phones"] = sorted(all_phones)
    # Если основное поле phone не заполнено (кнопка не сработала /
    # был cooldown), но в описании нашли номер — используем его.
    if not listing_data.get("phone") and listing_data["phones"]:
        listing_data["phone"] = listing_data["phones"][0]


def extract_listing_data(driver, wait, account_name, log_func):
    """Извлечь данные с открытой страницы листинга в dict.

    Тонкий оркестратор поверх _extract_*-helpers + _enrich_phones_from_description.
    Каждое поле — отдельный try/except внутри своего helper, чтобы один сбойный
    селектор не положил весь парс. Возвращает None только при критической ошибке
    (Exception наружу из любого helper'а), иначе — dict со всеми полями.
    """
    listing_data = {}
    try:
        listing_data["title"] = _extract_title(wait)
        listing_data["price"] = _extract_price(driver)
        listing_data["description"] = _extract_description(driver)
        listing_data["location"] = _extract_location(driver)
        listing_data["area"] = _extract_area(listing_data["title"], listing_data["description"])
        listing_data["category"] = _extract_category(driver, listing_data["title"])
        listing_data.update(_extract_seller_info(driver))
        listing_data["date_published"] = _extract_publication_date(driver)
        listing_data["photo_urls"] = _extract_photos(driver)
        listing_data["item_id"] = _extract_item_id(driver)
        listing_data["views"] = _extract_views(driver)
        listing_data["params"] = _extract_params(driver)
        listing_data["coordinates"] = _extract_coordinates(driver)

        # Визит в профиль продавца для подсчёта активных объявлений.
        # Открываем в новой вкладке, парсим, закрываем — не теряем текущую страницу.
        _visit_seller_profile(driver, listing_data, log_func, account_name)

        # A3 (удалён): _try_show_phone убран. Телефон всегда None.
        listing_data["phone"] = None

        # D4: нормализуем URL до канонического вида — иначе один и тот же
        # листинг с разными UTM создаст дубль в БД.
        listing_data["url"] = normalize_listing_url(driver.current_url)
        listing_data["date_scraped"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # D3: обогащаем phones номерами из описания (мутирует listing_data).
        _enrich_phones_from_description(listing_data)

        log_func(account_name, f"Извлечены данные: {listing_data['title']}")
    except Exception as e:
        log_func(account_name, f"Ошибка извлечения данных: {str(e)}")
        return None

    return listing_data


def _nil_if(value, *sentinels):
    """
    Вернёт None, если value попадает в список sentinels (или это пустая строка).
    Используется, чтобы не затирать в БД уже сохранённые значения данными,
    которые парсер не смог достать (см. COALESCE в upsert_listing).
    """
    if value is None:
        return None
    if isinstance(value, str) and (value == "" or value in sentinels):
        return None
    if value in sentinels:
        return None
    return value


# ─────────────────────────────────────────────────────────────────────────────
# S3: save_listing_to_db декомпозиция. Раньше функция была 154 строки одним
# куском (sentinel-нормализация + upsert + phones + статус + 4-5 метрик).
# Сейчас — оркестратор ~40 строк + три helper'а ниже. Поведение/гарантии
# (атомарная транзакция, set sentinel-полей в None, метрики) идентичны.
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_for_db(listing_data: dict) -> dict:
    """Sentinel-стрипинг для upsert_listing. Возвращает dict с теми же
    ключами, что и listing_data, но "Неизвестно"/"unknown"/0.0 → None.

    Это нужно, чтобы upsert_listing'у было что COALESCE'нуть с уже
    сохранёнными в БД полями, а не затереть их sentinel-ами от парсера.
    """
    return {
        "title": _nil_if(listing_data.get("title"), "Неизвестно"),
        "seller_name": _nil_if(listing_data.get("seller_name"), "Неизвестно"),
        "location": _nil_if(listing_data.get("location"), "Неизвестно"),
        "price": _nil_if(listing_data.get("price"), 0.0, 0),
        "area": _nil_if(listing_data.get("area"), 0.0, 0),
        "profile_id": _nil_if(listing_data.get("profile_id"), "unknown"),
        "profile_url": _nil_if(listing_data.get("profile_url")),
        "description": _nil_if(listing_data.get("description")),
        "date_published": _nil_if(listing_data.get("date_published")),
        # active_listings_count: 0 — валидное значение (у продавца может быть
        # 0 активных, если он только что снял листинг), поэтому 0 НЕ считаем
        # sentinel. Null только если парсер явно не достал.
        "active_listings_count": listing_data.get("active_listings_count"),
        # photo_urls: пустой список валиден — это "фото нет". None оставляем
        # если ключа нет. list превратится в JSON в upsert_listing.
        "photo_urls": listing_data.get("photo_urls"),
    }


def _save_phones_for_listing(
    db_manager, cur, listing_data: dict, listing_id: int | None = None
) -> None:
    """D3: пишем ВСЕ найденные номера (через «Показать телефон» + из
    описания), не только основной. Критично для phone_frequency_signal
    в HeuristicScorer — если у одного агента 5 объявлений с одним номером
    в описании, мы должны видеть phone_count=5, иначе все они выглядят
    как owner.

    Идёт в общей транзакции (cur передаётся снаружи).
    """
    phones_to_save = list(listing_data.get("phones") or [])
    if not phones_to_save and listing_data.get("phone"):
        phones_to_save = [listing_data["phone"]]
    for ph in phones_to_save:
        db_manager.upsert_phone(
            phone_normalized=ph,
            listing_count=1,
            score=0.0,
            cursor=cur,
            listing_id=listing_id,
        )


def _record_listing_outcome_metrics(
    db_manager, cur, account_name: str, listing_data: dict, status: str
) -> None:
    """Счётчики и журнал для уже сохранённого листинга.

    listings_parsed/<status> — общая статистика и разбивка по исходу.
    captcha_hits + log_captcha — отдельно при status='captcha' (не каждый
    captcha-инцидент привязан к листингу, например login captcha).

    Лежит в той же транзакции, что и сам листинг — если что-то упадёт,
    откатятся и метрики, и сохранение.
    """
    db_manager.incr_metric(account_name, "listings_parsed", cursor=cur)
    db_manager.incr_metric(account_name, f"listings_{status}", cursor=cur)

    if status == "captcha":
        # captcha_hits отдельно от listings_captcha — не каждый
        # captcha-инцидент привязан к листингу (например, login captcha).
        db_manager.incr_metric(account_name, "captcha_hits", cursor=cur)
        # C3: журнал инцидента для /lastcaptcha
        db_manager.log_captcha(
            account_name=account_name,
            page_url=listing_data.get("url", ""),
            action="phone_click",
            captcha_type="phone_captcha",
            cursor=cur,
        )

    # A3 (удалён): phone_clicks метрика убрана вместе с _try_show_phone.


def save_listing_to_db(listing_data, db_manager, log_func, account_name):
    """Сохраняем листинг в БД одной атомарной транзакцией (C4).

    Тонкий оркестратор поверх _normalize_for_db (sentinel-стрипинг),
    _save_phones_for_listing (D3) и _record_listing_outcome_metrics (E2/C3).
    Возвращает listing_id при успехе, None при ошибке. На ошибку
    дополнительно инкрементит listings_error метрику ВНЕ транзакции
    (она уже откатилась).
    """
    if not listing_data:
        return None

    try:
        norm = _normalize_for_db(listing_data)

        # C4: одна атомарная транзакция на весь листинг. Раньше каждый
        # upsert/mark_status открывал своё соединение, и краш между ними
        # оставлял БД в полу-записанном состоянии. Теперь либо все шаги
        # коммитятся вместе, либо все откатываются.
        with db_manager.transaction() as cur:
            listing_id = db_manager.upsert_listing(
                url=listing_data["url"],
                title=norm["title"],
                category=listing_data.get("category"),
                area=norm["area"],
                price=norm["price"],
                location=norm["location"],
                description=norm["description"],
                seller_name=norm["seller_name"],
                profile_id=norm["profile_id"],
                profile_url=norm["profile_url"],
                phone=listing_data.get("phone"),
                active_listings_count=norm["active_listings_count"],
                photo_urls=norm["photo_urls"],
                date_parsed=listing_data.get("date_scraped"),
                date_published=norm["date_published"],
                date_scraped=listing_data.get("date_scraped"),
                cursor=cur,
            )

            if norm["profile_id"]:
                db_manager.upsert_account(
                    profile_id=norm["profile_id"],
                    name=listing_data.get("seller_name", ""),
                    active_listings_count=listing_data.get("active_listings_count", 0),
                    registration_date="",
                    score=0.0,
                    cursor=cur,
                )

            _save_phones_for_listing(db_manager, cur, listing_data, listing_id=listing_id)

            # Inline heuristic classification — заполняет classification для новых
            # листингов сразу при парсинге (без LLM — только эвристика).
            # Без этого поле остаётся NULL и _try_write_to_owner не может
            # отличить собственника от агента.
            if listing_id is not None and norm["profile_id"]:
                try:
                    from heuristic_scorer import HeuristicScorer

                    scorer = HeuristicScorer(db_manager)
                    cls, conf, reason, _ = scorer.calculate_score(listing_data)
                    from datetime import datetime

                    db_manager.update_listing_classification(
                        listing_id=listing_id,
                        classification=cls,
                        confidence=conf,
                        source="heuristic-inline",
                        classified_at=datetime.now().isoformat(),
                        cursor=cur,
                    )
                    db_manager.update_account_classification(
                        profile_id=norm["profile_id"],
                        classification=cls,
                        confidence=conf,
                        source="heuristic-inline",
                        classified_at=datetime.now().isoformat(),
                        cursor=cur,
                    )
                    log_func(
                        account_name,
                        f"  Inline classification: {cls} (conf={conf:.2f}, {reason})",
                    )
                except Exception:
                    pass  # не ломаем save если классификация упала

            # A3 (удалён): _try_show_phone убран — captcha не ставится через листинг.
            if listing_id is not None:
                status = "ok"
                db_manager.mark_listing_parse_status(
                    url=listing_data["url"],
                    status=status,
                    listing_id=listing_id,
                    cursor=cur,
                )
                _record_listing_outcome_metrics(db_manager, cur, account_name, listing_data, status)

        log_func(account_name, f"Сохранено в БД (id={listing_id}): {listing_data['url']}")
        return listing_id

    except Exception as e:
        log_func(account_name, f"Ошибка сохранения в БД: {str(e)}")
        # E2: внешний инкремент (не в транзакции — она уже откатилась).
        # Если и сама метрика не запишется — глотаем, чтобы не потерять
        # основную ошибку выше.
        try:
            db_manager.incr_metric(account_name, "listings_error")
        except Exception:
            pass
        return None
