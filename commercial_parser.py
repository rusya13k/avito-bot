import logging
import random
import re
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import tg_bot as _tg
from account_state import account_state
from captcha_detect import detect_phone_captcha
from human_delay import human_delay

# E1: модульный logger. Раньше парсер писал только через переданный
# log_func (account-aware строки в TG-буфер). Теперь критичные ошибки —
# через logger.exception, чтобы алерт-хендлер их увидел.
logger = logging.getLogger(__name__)

# Module for parsing commercial real estate listings from Avito

# Внутренний sentinel-ключ в listing_data: True -> при сохранении в БД
# проставим parse_status='captcha'. Не часть schema и не предназначен
# для внешних потребителей.
_CAPTCHA_FLAG = "_a3_captcha_hit"

# A3: флаг в listing_data: True -> при сохранении инкрементируем метрику phone_clicks.
_PHONE_CLICKED_FLAG = "_a3_phone_clicked"


def _wf(driver, xpath, timeout=3):
    """
    C5: короткий WebDriverWait-обёртка вместо голого driver.find_element.
    Без неё страница не успевает прогрузиться -> мы попадаем в except
    и теряем реальные данные.
    """
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))


def normalize_phone(phone_text):
    """
    D3: нормализация в E.164 (+7XXXXXXXXXX) с защитой от мусора.

    Изменено относительно прежней версии:
    - 10-значный номер с любого старта НЕ префиксуется автоматически '7'
      (раньше '4155550123' → '+74155550123', что давало битый "российский"
      номер для иностранных вводов). Теперь 10 цифр = только если
      явно нет кода страны и это похоже на российский (мобильный 9XX,
      городской 4XX/8XX). В неоднозначных случаях возвращаем None.
    - Скрытые номера ('+7 (***) ***-**-**') корректно → None.
    - Слишком короткие/длинные → None.
    """
    if not phone_text:
        return None

    digits = re.sub(r"\D", "", phone_text)
    if not digits:
        return None

    # 11 digits начиная с 8 → российский, заменяем 8 на 7
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    # 11 digits начиная с 7 → уже E.164 без '+'
    elif len(digits) == 11 and digits.startswith("7"):
        pass
    # 10 digits → российский без кода страны. Принимаем ТОЛЬКО мобильные
    # (9XX) — их написание без префикса однозначно. Городские (4XX/8XX)
    # без префикса не отличить от иностранных (415, 800), поэтому
    # требуем их с явным '+7' / '8'.
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    else:
        return None

    return f"+{digits}"


# D3: regex для выгрести все номера из произвольного текста (описание).
# Покрывает форматы: +7..., 8..., (495) ..., с разделителями любыми.
# Минимум 10 цифр в кандидате (после удаления всего нецифрового).
_PHONE_RE = re.compile(
    r"(?:\+?\d[\d\s\-\(\)]{8,}\d)",
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


def extract_listing_data(driver, wait, account_name, log_func):
    """
    Extract data from a commercial real estate listing page
    """
    listing_data = {}

    try:
        # Extract basic listing information
        try:
            title_elem = wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//h1[@data-marker='item-view/title-info']")
                )
            )
            listing_data["title"] = title_elem.text
        except TimeoutException:
            listing_data["title"] = "Неизвестно"

        try:
            # Extract price (C5: ждём до 3s, страница может догружаться)
            price_elem = _wf(driver, "//span[@data-marker='item-view/price']")
            price_text = price_elem.text.replace("₽", "").replace(" ", "").replace("\n", "")
            listing_data["price"] = float(re.sub(r"[^\d,]", "", price_text).replace(",", "."))
        except Exception:
            listing_data["price"] = 0.0

        try:
            # Extract description
            desc_elem = _wf(driver, "//div[@data-marker='item-description']")
            listing_data["description"] = desc_elem.text
        except Exception:
            listing_data["description"] = ""

        try:
            # Extract location
            location_elem = _wf(driver, "//div[@data-marker='item-view/item-location']//span")
            listing_data["location"] = location_elem.text
        except Exception:
            listing_data["location"] = "Неизвестно"

        try:
            # Extract area
            # Look for area in the title or description
            title_text = listing_data["title"].lower()
            desc_text = listing_data["description"].lower()
            full_text = title_text + " " + desc_text

            # Try to find area in square meters
            area_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:кв\.?\s*м|м2)", full_text)
            if area_match:
                listing_data["area"] = float(area_match.group(1))
            else:
                listing_data["area"] = 0.0
        except Exception:
            listing_data["area"] = 0.0

        # Extract category and type
        try:
            # Try to determine category from URL or breadcrumbs
            url = driver.current_url
            if "офис" in url or "офис" in listing_data["title"].lower():
                listing_data["category"] = "офисные помещения"
            elif "торгов" in url or "торгов" in listing_data["title"].lower():
                listing_data["category"] = "торговые помещения"
            elif "склад" in url or "склад" in listing_data["title"].lower():
                listing_data["category"] = "склады"
            elif "производств" in url or "производств" in listing_data["title"].lower():
                listing_data["category"] = "производственные помещения"
            elif "бизнес" in url or "бизнес" in listing_data["title"].lower():
                listing_data["category"] = "готовый бизнес"
            else:
                listing_data["category"] = "коммерческая недвижимость"
        except Exception:
            listing_data["category"] = "коммерческая недвижимость"

        # Extract seller information (C5: short waits)
        try:
            seller_name_elem = _wf(driver, "//div[@data-marker='seller-info/username']//a")
            listing_data["seller_name"] = seller_name_elem.text
            listing_data["profile_url"] = seller_name_elem.get_attribute("href")

            profile_id_match = re.search(r"/user/([^/]+)", listing_data["profile_url"] or "")
            if profile_id_match:
                listing_data["profile_id"] = profile_id_match.group(1)
            else:
                listing_data["profile_id"] = "unknown"

            try:
                active_listings_elem = _wf(
                    driver, "//div[@data-marker='seller-info/active-ads-count']", timeout=1
                )
                count_match = re.search(r"\d+", active_listings_elem.text)
                listing_data["active_listings_count"] = (
                    int(count_match.group()) if count_match else 0
                )
            except Exception:
                listing_data["active_listings_count"] = 0
        except Exception:
            listing_data["seller_name"] = "Неизвестно"
            listing_data["profile_url"] = ""
            listing_data["profile_id"] = "unknown"
            listing_data["active_listings_count"] = 0

        # Extract publication date (короткий wait — поле необязательное)
        try:
            date_elem = _wf(driver, "//div[@data-marker='item-view/date']", timeout=1)
            listing_data["date_published"] = date_elem.text
        except Exception:
            listing_data["date_published"] = ""

        # Extract photos
        try:
            photo_urls = []
            photo_elements = driver.find_elements(
                By.XPATH, "//img[@data-marker='image-frame/image']"
            )
            for photo_elem in photo_elements[:3]:  # Get first 3 photos
                photo_urls.append(photo_elem.get_attribute("src"))
            listing_data["photo_urls"] = photo_urls
        except Exception:
            listing_data["photo_urls"] = []

        # Extract phone number if available
        # A3: многоуровневые проверки перед кликом "Показать телефон":
        #   1. captcha-cooldown (уже был ранее)
        #   2. Дневной in-memory лимит (should_skip_phone — hard limit + prev session >5)
        #   3. Session soft-limit: random.random() < 0.3 (кликаем только в 30% случаев)
        #   4. Pre-click captcha check
        #   5. Post-click captcha check
        listing_data["phone"] = None

        if account_state.is_cooled_down(account_name):
            log_func(
                account_name,
                f"Skip 'Показать телефон' — аккаунт в cooldown ещё "
                f"{account_state.cooldown_remaining_seconds(account_name)}s",
            )
        elif account_state.should_skip_phone(account_name):
            # A3: дневной hard-limit достигнут ИЛИ предыдущая сессия >5 кликов
            log_func(account_name, "A3: Пропуск 'Показать телефон' — дневной/сессионный лимит")
        elif random.random() >= 0.3:
            # A3: session soft-limit — кликаем только в ~30% случаев
            log_func(account_name, "A3: Пропуск 'Показать телефон' — session soft-limit (30%)")
        elif detect_phone_captcha(driver, log_func=log_func, account_name=account_name):
            log_func(account_name, "Капча обнаружена ДО клика 'Показать телефон' — пропускаем")
            listing_data[_CAPTCHA_FLAG] = True
            account_state.mark_captcha(account_name)
        else:
            try:
                phone_btn = driver.find_element(
                    By.XPATH, "//button[@data-marker='item-contact-bar/call-button']"
                )
                phone_btn.click()
                # A3: cooldown 30-90 сек после клика (ранее было 0.8-1.6с).
                # Реальный пользователь не кликает "Показать телефон" каждые 2 секунды.
                # Используем uniform (более равномерный), чтобы не быть предсказуемым.
                human_delay(30, 90, stop_event=_tg.stop_event, distribution="uniform")

                # Post-click captcha check.
                if detect_phone_captcha(driver, log_func=log_func, account_name=account_name):
                    log_func(
                        account_name,
                        "!!! Капча после клика 'Показать телефон' — аккаунт уйдёт в cooldown",
                    )
                    listing_data[_CAPTCHA_FLAG] = True
                    account_state.mark_captcha(account_name)
                else:
                    try:
                        phone_elem = WebDriverWait(driver, 5).until(
                            EC.visibility_of_element_located(
                                (By.XPATH, "//span[@data-marker='item-contact-bar/phone']")
                            )
                        )
                        listing_data["phone"] = normalize_phone(phone_elem.text)
                        # A3: успешный клик — обновляем in-memory счётчики.
                        # Метрика phone_clicks в БД инкрементируется в save_listing_to_db
                        # через флаг _PHONE_CLICKED_FLAG.
                        account_state.record_phone_click(account_name)
                        listing_data[_PHONE_CLICKED_FLAG] = True
                    except TimeoutException:
                        # Телефон не появился, но и капчи нет -> просто не показали номер.
                        log_func(account_name, "Номер не появился после клика (без капчи)")
                    except Exception as inner:
                        log_func(account_name, f"Ошибка чтения номера: {inner}")
            except Exception as e:
                log_func(account_name, f"Ошибка клика 'Показать телефон': {e}")

        # Add URL and current date.
        # D4: нормализуем URL до канонического вида сразу — иначе
        # один и тот же листинг с разными UTM создаст дубль в БД.
        listing_data["url"] = normalize_listing_url(driver.current_url)
        listing_data["date_scraped"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # D3: соберём ВСЕ номера, упомянутые в описании (продавцы часто
        # дублируют контакт прямо в тексте). Объединяем с phone, который
        # достали через "Показать телефон". Дедуп — set + сортировка.
        phones_from_desc = extract_phones_from_text(listing_data.get("description"))
        all_phones = set(phones_from_desc)
        if listing_data.get("phone"):
            all_phones.add(listing_data["phone"])
        listing_data["phones"] = sorted(all_phones)
        # Если основное поле phone не заполнено (кнопка не сработала /
        # был cooldown), но в описании нашли номер — используем его.
        if not listing_data.get("phone") and listing_data["phones"]:
            listing_data["phone"] = listing_data["phones"][0]

        # Log the extracted data
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


def save_listing_to_db(listing_data, db_manager, log_func, account_name):
    """
    Save listing data to database.

    Возвращает listing_id при успешной записи, иначе None.
    Сохраняет ВСЕ поля из extract_listing_data: title, seller_name, profile_id,
    profile_url, photo_urls, phone, active_listings_count (раньше терялись).

    Sentinel-значения (напр. "Неизвестно", "unknown", 0.0 для цены/площади)
    нормализуются в None, чтобы не перезаписывать в БД реально спарсенные
    ранее данные.
    """
    if not listing_data:
        return None

    try:
        # --- Нормализуем sentinel-значения из extract_listing_data в None ---
        title = _nil_if(listing_data.get("title"), "Неизвестно")
        seller_name = _nil_if(listing_data.get("seller_name"), "Неизвестно")
        location = _nil_if(listing_data.get("location"), "Неизвестно")
        price = _nil_if(listing_data.get("price"), 0.0, 0)
        area = _nil_if(listing_data.get("area"), 0.0, 0)
        profile_id = _nil_if(listing_data.get("profile_id"), "unknown")
        profile_url = _nil_if(listing_data.get("profile_url"))
        description = _nil_if(listing_data.get("description"))
        date_published = _nil_if(listing_data.get("date_published"))
        # active_listings_count: 0 — валидное значение (у продавца может быть 0
        # активных объявлений, если он только что снял листинг), поэтому
        # ноль НЕ считаем sentinel. Null только если парсер явно не достал.
        active_listings_count = listing_data.get("active_listings_count")
        # photo_urls: пустой список валиден — это "фото нет". None оставляем,
        # если ключа нет. list превратится в JSON в upsert_listing.
        photo_urls = listing_data.get("photo_urls")

        # C4: одна атомарная транзакция на весь листинг.
        # Раньше каждый upsert/mark_status открывал своё соединение, и краш
        # между ними оставлял БД в полу-записанном состоянии (например,
        # листинг записан, но phone/parse_status — нет). Теперь либо все
        # четыре операции коммитятся вместе, либо все откатываются.
        with db_manager.transaction() as cur:
            # Save listing (все поля разом — раньше терялись)
            listing_id = db_manager.upsert_listing(
                url=listing_data["url"],
                title=title,
                category=listing_data.get("category"),
                area=area,
                price=price,
                location=location,
                description=description,
                seller_name=seller_name,
                profile_id=profile_id,
                profile_url=profile_url,
                phone=listing_data.get("phone"),
                active_listings_count=active_listings_count,
                photo_urls=photo_urls,
                date_parsed=listing_data.get("date_scraped"),
                date_published=date_published,
                date_scraped=listing_data.get("date_scraped"),
                cursor=cur,
            )

            # Save account if exists
            if profile_id:
                db_manager.upsert_account(
                    profile_id=profile_id,
                    name=listing_data.get("seller_name", ""),
                    active_listings_count=listing_data.get("active_listings_count", 0),
                    registration_date="",
                    score=0.0,
                    cursor=cur,
                )

            # Save phones if exist.
            # D3: пишем ВСЕ найденные номера (через "Показать телефон" +
            # из текста описания), не только основной. Это критично для
            # phone_frequency_signal в HeuristicScorer — если у одного
            # агента 5 объявлений с одним номером в описании, мы должны
            # видеть phone_count=5, иначе все они выглядят как owner.
            phones_to_save = list(listing_data.get("phones") or [])
            if not phones_to_save and listing_data.get("phone"):
                phones_to_save = [listing_data["phone"]]
            for ph in phones_to_save:
                db_manager.upsert_phone(
                    phone_normalized=ph,
                    listing_count=1,
                    score=0.0,
                    cursor=cur,
                )

            # A3: проставляем parse_status. По умолчанию 'ok'; если был детект
            # капчи на этапе extract_listing_data — пишем 'captcha'.
            # В рамках общей транзакции — если эта запись упадёт, откатятся
            # и предыдущие шаги тоже (атомарность).
            if listing_id is not None:
                status = "captcha" if listing_data.get(_CAPTCHA_FLAG) else "ok"
                db_manager.mark_listing_parse_status(
                    url=listing_data["url"],
                    status=status,
                    listing_id=listing_id,
                    cursor=cur,
                )
                # E2: счётчики per account / per hour. listings_parsed —
                # общая сумма прошедших через парсер; listings_<status> —
                # разбивка по исходу. Лежит в той же транзакции — если
                # commit'нется листинг, commit'нется и метрика.
                db_manager.incr_metric(
                    account_name,
                    "listings_parsed",
                    cursor=cur,
                )
                db_manager.incr_metric(
                    account_name,
                    f"listings_{status}",
                    cursor=cur,
                )
                if status == "captcha":
                    # captcha_hits отдельно от listings_captcha —
                    # потому что не каждый captcha-инцидент привязан к
                    # листингу (login captcha, например).
                    db_manager.incr_metric(
                        account_name,
                        "captcha_hits",
                        cursor=cur,
                    )
                    # C3: журнал инцидента для /lastcaptcha
                    db_manager.log_captcha(
                        account_name=account_name,
                        page_url=listing_data.get("url", ""),
                        action="phone_click",
                        captcha_type="phone_captcha",
                        cursor=cur,
                    )
                # A3: метрика phone_clicks — новая E2-метрика для аналитики
                # кликов "Показать телефон". Флаг выставляется в extract_listing_data
                # после успешного получения номера.
                if listing_data.get(_PHONE_CLICKED_FLAG):
                    db_manager.incr_metric(
                        account_name,
                        "phone_clicks",
                        cursor=cur,
                    )

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
