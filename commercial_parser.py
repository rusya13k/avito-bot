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
from human_mouse import human_click as _human_click

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
    """Цена из item-view/price. C5: до 3s ждём, страница может догружаться.
    Возвращаем 0.0 sentinel — _nil_if маппит в None при сохранении в БД.
    """
    try:
        price_elem = _wf(driver, "//span[@data-marker='item-view/price']")
        price_text = price_elem.text.replace("₽", "").replace(" ", "").replace("\n", "")
        return float(re.sub(r"[^\d,]", "", price_text).replace(",", "."))
    except Exception:
        return 0.0


def _extract_description(driver) -> str:
    """Текст описания из item-description. Пустая строка если нет."""
    try:
        desc_elem = _wf(driver, "//div[@data-marker='item-description']")
        return desc_elem.text
    except Exception:
        return ""


def _extract_location(driver) -> str:
    """Локация из item-view/item-location//span. Sentinel "Неизвестно"."""
    try:
        location_elem = _wf(driver, "//div[@data-marker='item-view/item-location']//span")
        return location_elem.text
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
    """
    try:
        seller_name_elem = _wf(driver, "//div[@data-marker='seller-info/username']//a")
        seller_name = seller_name_elem.text
        profile_url = seller_name_elem.get_attribute("href")

        profile_id_match = re.search(r"/user/([^/]+)", profile_url or "")
        profile_id = profile_id_match.group(1) if profile_id_match else "unknown"

        try:
            active_elem = _wf(
                driver, "//div[@data-marker='seller-info/active-ads-count']", timeout=1
            )
            count_match = re.search(r"\d+", active_elem.text)
            active_count = int(count_match.group()) if count_match else 0
        except Exception:
            active_count = 0

        return {
            "seller_name": seller_name,
            "profile_url": profile_url,
            "profile_id": profile_id,
            "active_listings_count": active_count,
        }
    except Exception:
        return {
            "seller_name": "Неизвестно",
            "profile_url": "",
            "profile_id": "unknown",
            "active_listings_count": 0,
        }


def _extract_publication_date(driver) -> str:
    """Дата публикации из item-view/date. Пустая строка если нет."""
    try:
        date_elem = _wf(driver, "//div[@data-marker='item-view/date']", timeout=1)
        return date_elem.text
    except Exception:
        return ""


def _extract_photos(driver, max_n: int = 3) -> list[str]:
    """Первые `max_n` URL фото из image-frame/image. [] если нет."""
    try:
        photo_elements = driver.find_elements(By.XPATH, "//img[@data-marker='image-frame/image']")
        return [p.get_attribute("src") for p in photo_elements[:max_n]]
    except Exception:
        return []


def _try_show_phone(driver, account_name: str, log_func, listing_data: dict) -> None:
    """A3: попытка клика «Показать телефон» с многоуровневыми проверками.

    Этапы (в порядке гарантированной строгости):
      1. captcha-cooldown — аккаунт «остывает» после прошлой капчи
      2. Дневной in-memory лимит (should_skip_phone — hard limit + prev session >5)
      3. Session soft-limit: random.random() < 0.3 (кликаем только в 30% случаев)
      4. Pre-click captcha check
      5. Сам клик + 30-90s human_delay
      6. Post-click captcha check / попытка прочесть номер

    Мутирует listing_data:
      listing_data["phone"]            -> str | None
      listing_data[_CAPTCHA_FLAG]      -> True если поймали капчу
      listing_data[_PHONE_CLICKED_FLAG] -> True если успешно прочли номер
    """
    listing_data["phone"] = None

    if account_state.is_cooled_down(account_name):
        log_func(
            account_name,
            f"Skip 'Показать телефон' — аккаунт в cooldown ещё "
            f"{account_state.cooldown_remaining_seconds(account_name)}s",
        )
        return
    if account_state.should_skip_phone(account_name):
        # A3: дневной hard-limit достигнут ИЛИ предыдущая сессия >5 кликов
        log_func(account_name, "A3: Пропуск 'Показать телефон' — дневной/сессионный лимит")
        return
    if random.random() >= 0.3:
        # A3: session soft-limit — кликаем только в ~30% случаев
        log_func(account_name, "A3: Пропуск 'Показать телефон' — session soft-limit (30%)")
        return
    if detect_phone_captcha(driver, log_func=log_func, account_name=account_name):
        log_func(account_name, "Капча обнаружена ДО клика 'Показать телефон' — пропускаем")
        listing_data[_CAPTCHA_FLAG] = True
        # T17: phone-page капча, но клик ещё НЕ был — это «листинг-уровень»
        # (не наш клик триггерил). Менее опасно, чем пост-клик ниже.
        account_state.mark_captcha(account_name, captcha_type="avito_listing")
        return

    try:
        phone_btn = driver.find_element(
            By.XPATH, "//button[@data-marker='item-contact-bar/call-button']"
        )
        # T6: «Показать телефон» — самое палевное действие, кликаем
        # через Bezier-движение курсора + jitter, не «телепорт-click».
        _human_click(driver, phone_btn, stop_event=_tg.get_account_stop_event(account_name))
        # A3: cooldown 30-90 сек после клика (ранее было 0.8-1.6с).
        # Реальный пользователь не кликает "Показать телефон" каждые 2 секунды.
        # uniform (более равномерный), чтобы не быть предсказуемым.
        human_delay(
            30, 90, stop_event=_tg.get_account_stop_event(account_name), distribution="uniform"
        )
    except Exception as e:
        log_func(account_name, f"Ошибка клика 'Показать телефон': {e}")
        return

    # Post-click captcha check
    if detect_phone_captcha(driver, log_func=log_func, account_name=account_name):
        log_func(
            account_name,
            "!!! Капча после клика 'Показать телефон' — аккаунт уйдёт в cooldown",
        )
        listing_data[_CAPTCHA_FLAG] = True
        # T17: avito_phone — самый опасный тип (×2 multiplier).
        # Сам бот спровоцировал клик, и Avito ответил капчей.
        account_state.mark_captcha(account_name, captcha_type="avito_phone")
        return

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

    Тонкий оркестратор поверх _extract_*-helpers + _try_show_phone +
    _enrich_phones_from_description. Каждое поле — отдельный try/except
    внутри своего helper, чтобы один сбойный селектор не положил весь
    парс. Возвращает None только при критической ошибке (Exception
    наружу из любого helper'а), иначе — dict со всеми полями.
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

        # A3: попытка клика «Показать телефон» (мутирует listing_data).
        _try_show_phone(driver, account_name, log_func, listing_data)

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


def _save_phones_for_listing(db_manager, cur, listing_data: dict) -> None:
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
        )


def _record_listing_outcome_metrics(
    db_manager, cur, account_name: str, listing_data: dict, status: str
) -> None:
    """E2/A3/C3: счётчики и журнал для уже сохранённого листинга.

    listings_parsed/<status> — общая статистика и разбивка по исходу.
    captcha_hits + log_captcha — отдельно при status='captcha' (не каждый
    captcha-инцидент привязан к листингу, например login captcha).
    phone_clicks — если был успешный клик «Показать телефон»
    (флаг _PHONE_CLICKED_FLAG из extract_listing_data).

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

    # A3: метрика phone_clicks — для аналитики кликов «Показать телефон».
    # Флаг выставляется в _try_show_phone после успешного получения номера.
    if listing_data.get(_PHONE_CLICKED_FLAG):
        db_manager.incr_metric(account_name, "phone_clicks", cursor=cur)


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

            _save_phones_for_listing(db_manager, cur, listing_data)

            # A3: parse_status — 'ok' или 'captcha' (если поймали капчу).
            if listing_id is not None:
                status = "captcha" if listing_data.get(_CAPTCHA_FLAG) else "ok"
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
