"""
S3: тесты для private helpers в commercial_parser.py — те, которые
можно проверить без Selenium-mock'а (pure functions / простые dict-операции).

Покрываем:
- _extract_area: regex по title+description, корректные/мусорные/пустые входы
- _extract_category: маппинг ключевых слов в title/url
- _normalize_for_db: sentinel-стрипинг для upsert_listing

Selenium-helpers (_extract_title/price/description/seller_info, _try_show_phone,
_save_phones_for_listing, _record_listing_outcome_metrics) покрыты неявно
через test_phone_limit.py и test_phone_and_url.py + интеграционные тесты
save_listing_to_db.
"""

from unittest.mock import MagicMock

from commercial_parser import (
    _extract_area,
    _extract_category,
    _normalize_for_db,
)

# ── _extract_area ────────────────────────────────────────────────────────────


def test_extract_area_finds_kvm_in_title():
    """Заголовок «Офис 50 кв.м» → 50.0."""
    assert _extract_area("Офис 50 кв.м в центре", "") == 50.0


def test_extract_area_finds_m2_short_form():
    """Краткая форма «м2» (без дефиса/пробела) — тоже находится."""
    assert _extract_area("Склад 200 м2", "") == 200.0


def test_extract_area_finds_in_description():
    """Если в title нет — берём из description."""
    assert _extract_area("Объявление", "Площадь помещения 75 кв м, центр") == 75.0


def test_extract_area_decimal_value():
    """Дробное значение (площадь не всегда целая)."""
    assert _extract_area("Помещение 12.5 кв.м", "") == 12.5


def test_extract_area_returns_zero_when_no_match():
    """Если ничего похожего на площадь — 0.0."""
    assert _extract_area("Офис в центре", "Описание без чисел") == 0.0


def test_extract_area_handles_empty_inputs():
    """Пустые строки на входе → 0.0 (никаких exceptions)."""
    assert _extract_area("", "") == 0.0


def test_extract_area_picks_first_match():
    """Если несколько чисел — берёт первое (как первое regex-match)."""
    # Здесь оба matches валидны, regex вернёт первое.
    result = _extract_area("Офис 50 кв.м, склад 200 кв.м рядом", "")
    assert result == 50.0


# ── _extract_category ────────────────────────────────────────────────────────


def _mock_driver(url: str = ""):
    """Маленький helper: driver-like объект с .current_url."""
    d = MagicMock()
    d.current_url = url
    return d


def test_extract_category_office_from_url():
    """URL содержит «офис» → офисные помещения."""
    drv = _mock_driver("https://avito.ru/moskva/kommercheskaya/офисные/...")
    assert _extract_category(drv, "Какой-то заголовок") == "офисные помещения"


def test_extract_category_office_from_title():
    """Title содержит «офис» → офисные помещения."""
    drv = _mock_driver("https://avito.ru/x")
    assert _extract_category(drv, "Аренда офиса 50 м") == "офисные помещения"


def test_extract_category_warehouse():
    """Title содержит «склад» → склады."""
    drv = _mock_driver("https://avito.ru/x")
    assert _extract_category(drv, "Аренда склада 200 м") == "склады"


def test_extract_category_retail():
    """Title содержит «торгов» → торговые помещения."""
    drv = _mock_driver("https://avito.ru/x")
    assert _extract_category(drv, "Торговое помещение 50 м") == "торговые помещения"


def test_extract_category_production():
    """Title содержит «производств» → производственные."""
    drv = _mock_driver("https://avito.ru/x")
    assert _extract_category(drv, "Производственное помещение") == "производственные помещения"


def test_extract_category_business():
    drv = _mock_driver("https://avito.ru/x")
    assert _extract_category(drv, "Готовый бизнес — кафе") == "готовый бизнес"


def test_extract_category_default_for_unknown():
    """Никакое ключевое слово не попало → дефолтная категория."""
    drv = _mock_driver("https://avito.ru/x")
    assert _extract_category(drv, "Объект коммерческой недвижимости") == "коммерческая недвижимость"


def test_extract_category_handles_driver_exception():
    """Если driver.current_url падает — возвращаем default, не пробрасываем."""
    drv = MagicMock()
    type(drv).current_url = property(lambda self: (_ for _ in ()).throw(RuntimeError("dead")))
    assert _extract_category(drv, "title") == "коммерческая недвижимость"


# ── _normalize_for_db ────────────────────────────────────────────────────────


def test_normalize_for_db_strips_unknown_sentinels():
    """Sentinel-значения «Неизвестно»/«unknown»/0.0 → None."""
    raw = {
        "title": "Неизвестно",
        "seller_name": "Неизвестно",
        "location": "Неизвестно",
        "price": 0.0,
        "area": 0,
        "profile_id": "unknown",
    }
    n = _normalize_for_db(raw)
    assert n["title"] is None
    assert n["seller_name"] is None
    assert n["location"] is None
    assert n["price"] is None
    assert n["area"] is None
    assert n["profile_id"] is None


def test_normalize_for_db_preserves_real_values():
    """Реальные значения сохраняются как есть."""
    raw = {
        "title": "Офис 50 кв.м",
        "seller_name": "Иван Петров",
        "location": "Москва, центр",
        "price": 100000.0,
        "area": 50.0,
        "profile_id": "abc123",
        "description": "Хороший офис",
    }
    n = _normalize_for_db(raw)
    assert n["title"] == "Офис 50 кв.м"
    assert n["seller_name"] == "Иван Петров"
    assert n["location"] == "Москва, центр"
    assert n["price"] == 100000.0
    assert n["area"] == 50.0
    assert n["profile_id"] == "abc123"
    assert n["description"] == "Хороший офис"


def test_normalize_for_db_zero_active_listings_count_preserved():
    """active_listings_count: 0 — валидное значение (продавец только что
    снял листинг). НЕ должно стать None."""
    raw = {"active_listings_count": 0}
    n = _normalize_for_db(raw)
    assert n["active_listings_count"] == 0  # not None


def test_normalize_for_db_empty_photo_urls_preserved():
    """photo_urls: пустой список валиден («фото нет»). Не должно стать None."""
    raw = {"photo_urls": []}
    n = _normalize_for_db(raw)
    assert n["photo_urls"] == []


def test_normalize_for_db_empty_description_becomes_none():
    """Пустая строка description → None (см. _nil_if)."""
    raw = {"description": ""}
    n = _normalize_for_db(raw)
    assert n["description"] is None


def test_normalize_for_db_missing_keys_become_none():
    """Если ключа вообще нет — get(...) → None → _nil_if(None) → None."""
    raw = {}  # ничего не парсилось
    n = _normalize_for_db(raw)
    # Проверяем все ключи, которые есть в _nil_if'е
    for k in (
        "title",
        "seller_name",
        "location",
        "price",
        "area",
        "profile_id",
        "profile_url",
        "description",
        "date_published",
        "active_listings_count",
        "photo_urls",
    ):
        assert n[k] is None, f"key={k}: ожидался None, получили {n[k]!r}"
