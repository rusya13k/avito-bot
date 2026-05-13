"""
H1: тесты для outbound_contacts таблицы и DB-методов.

Проверяем:
- Схема создаётся (выживание init_database / migrate)
- record_outbound пишет, повтор по profile_id игнорируется (UNIQUE)
- was_owner_contacted находит существующую запись
- get_owners_to_contact фильтрует:
    * только classification='owner'
    * только parse_status NULL/ok (исключает captcha/error)
    * только profile_id != NULL/'unknown'
    * НЕ включает уже контактированных (по UNIQUE constraint)
    * limit, min_age_hours
- get_outbound_count_today считает только сегодняшние записи.
"""

import time

import pytest

# ── Helper: вставить листинг ──────────────────────────────────────────────


def _insert_listing(
    db,
    *,
    url: str,
    profile_id: str | None = "owner_a",
    classification: str | None = "owner",
    parse_status: str | None = "ok",
    title: str = "Test listing",
    age_hours: float = 0,
):
    """Вставить листинг с заданными атрибутами + опц. классификацией.

    age_hours — если >0, устанавливает date_scraped в прошлое.
    """
    if age_hours > 0:
        date_scraped = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_hours * 3600)
        )
    else:
        date_scraped = time.strftime("%Y-%m-%d %H:%M:%S")

    listing_id = db.upsert_listing(
        url=url,
        category="commercial",
        area=50.0,
        price=100000.0,
        location="Москва",
        description="Test description",
        date_parsed=date_scraped,
        date_published=date_scraped,
        date_scraped=date_scraped,
        title=title,
        seller_name="Test Seller",
        profile_id=profile_id,
    )
    if classification:
        db.update_listing_classification(
            listing_id=listing_id,
            classification=classification,
            confidence=0.95,
            source="test",
            classified_at=date_scraped,
        )
    if parse_status:
        db.mark_listing_parse_status(url=url, status=parse_status, listing_id=listing_id)
    return listing_id


# ── was_owner_contacted ────────────────────────────────────────────────────


def test_was_owner_contacted_empty(db):
    """Пустая БД → was_owner_contacted = False."""
    assert db.was_owner_contacted("unknown_profile") is False


def test_was_owner_contacted_after_record(db):
    """После record_outbound → was_owner_contacted = True."""
    listing_id = _insert_listing(db, url="https://test/1")
    assert db.record_outbound("acc1", "owner_a", listing_id, "https://test/1") is True
    assert db.was_owner_contacted("owner_a") is True
    assert db.was_owner_contacted("other_owner") is False


def test_was_owner_contacted_handles_none(db):
    """profile_id=None → False (защита от пустых ключей)."""
    assert db.was_owner_contacted("") is False
    assert db.was_owner_contacted(None) is False


# ── record_outbound: dedup ─────────────────────────────────────────────────


def test_record_outbound_unique_per_profile_id(db):
    """Повторный record_outbound одного profile_id (даже разными аккаунтами)
    должен вернуть False — UNIQUE constraint срабатывает."""
    listing_id = _insert_listing(db, url="https://test/1")
    assert db.record_outbound("acc1", "owner_a", listing_id, "https://test/1") is True
    # Тот же profile_id с другого аккаунта — должен быть отброшен.
    assert db.record_outbound("acc2", "owner_a", listing_id, "https://test/1") is False
    # И тот же аккаунт повторно — тоже отброшен.
    assert db.record_outbound("acc1", "owner_a", listing_id, "https://test/1") is False


def test_record_outbound_different_profiles_ok(db):
    """Разные profile_id → разные записи, обе создаются."""
    _insert_listing(db, url="https://test/1", profile_id="owner_a")
    _insert_listing(db, url="https://test/2", profile_id="owner_b")
    assert db.record_outbound("acc1", "owner_a", listing_id=None) is True
    assert db.record_outbound("acc1", "owner_b", listing_id=None) is True


# ── get_owners_to_contact: фильтры ────────────────────────────────────────


def test_get_owners_to_contact_only_owner_class(db):
    """Возвращает только classification='owner', не agent/uncertain."""
    _insert_listing(db, url="https://o/1", profile_id="o1", classification="owner")
    _insert_listing(db, url="https://a/1", profile_id="a1", classification="agent")
    _insert_listing(db, url="https://u/1", profile_id="u1", classification="uncertain")
    rows = db.get_owners_to_contact("acc1", limit=10)
    assert len(rows) == 1
    assert rows[0]["profile_id"] == "o1"


def test_get_owners_to_contact_excludes_already_contacted(db):
    """profile_id уже в outbound_contacts → не попадает в кандидаты."""
    _insert_listing(db, url="https://o/1", profile_id="o1")
    _insert_listing(db, url="https://o/2", profile_id="o2")
    _insert_listing(db, url="https://o/3", profile_id="o3")
    db.record_outbound("acc1", "o2", None, None)

    rows = db.get_owners_to_contact("acc1", limit=10)
    profile_ids = {r["profile_id"] for r in rows}
    assert profile_ids == {"o1", "o3"}  # o2 уже контактирован


def test_get_owners_to_contact_excludes_null_profile_id(db):
    """profile_id IS NULL/''/'unknown' → не кандидат (нечем dedup'ить)."""
    _insert_listing(db, url="https://o/1", profile_id="o1")
    _insert_listing(db, url="https://o/2", profile_id="unknown")
    _insert_listing(db, url="https://o/3", profile_id=None)
    _insert_listing(db, url="https://o/4", profile_id="")

    rows = db.get_owners_to_contact("acc1", limit=10)
    assert {r["profile_id"] for r in rows} == {"o1"}


def test_get_owners_to_contact_excludes_captcha_status(db):
    """parse_status='captcha' → не кандидат (плохой парс, listing-data
    могут быть пустыми/искаженными)."""
    _insert_listing(db, url="https://o/1", profile_id="o1", parse_status="ok")
    _insert_listing(db, url="https://o/2", profile_id="o2", parse_status="captcha")
    _insert_listing(db, url="https://o/3", profile_id="o3", parse_status="error")
    rows = db.get_owners_to_contact("acc1", limit=10)
    assert {r["profile_id"] for r in rows} == {"o1"}


def test_get_owners_to_contact_min_age_hours(db):
    """min_age_hours отсекает свежие листинги (< указанного возраста)."""
    _insert_listing(db, url="https://o/fresh", profile_id="o_fresh", age_hours=0.5)
    _insert_listing(db, url="https://o/old", profile_id="o_old", age_hours=2.0)

    # Без фильтра — оба
    assert len(db.get_owners_to_contact("acc1", limit=10)) == 2
    # С min_age=1h — только old
    rows = db.get_owners_to_contact("acc1", limit=10, min_age_hours=1.0)
    assert {r["profile_id"] for r in rows} == {"o_old"}


def test_get_owners_to_contact_respects_limit(db):
    for i in range(5):
        _insert_listing(db, url=f"https://o/{i}", profile_id=f"o{i}")
    rows = db.get_owners_to_contact("acc1", limit=3)
    assert len(rows) == 3


def test_get_owners_to_contact_returns_listing_fields(db):
    """Возвращаемая запись содержит ключевые поля для LLM-промпта."""
    listing_id = _insert_listing(
        db,
        url="https://o/1",
        profile_id="o1",
        title="Офис 50 кв.м в центре",
    )
    rows = db.get_owners_to_contact("acc1", limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == listing_id
    assert r["url"] == "https://o/1"
    assert r["title"] == "Офис 50 кв.м в центре"
    assert r["profile_id"] == "o1"
    # Должны быть и другие поля для промпта
    for field in ("seller_name", "location", "area", "price", "description", "category"):
        assert field in r


# ── get_outbound_count_today ──────────────────────────────────────────────


def test_get_outbound_count_today_empty(db):
    assert db.get_outbound_count_today("acc1") == 0


def test_get_outbound_count_today_counts_per_account(db):
    _insert_listing(db, url="https://o/1", profile_id="o1")
    _insert_listing(db, url="https://o/2", profile_id="o2")
    _insert_listing(db, url="https://o/3", profile_id="o3")
    db.record_outbound("acc1", "o1", None, None)
    db.record_outbound("acc1", "o2", None, None)
    db.record_outbound("acc2", "o3", None, None)

    assert db.get_outbound_count_today("acc1") == 2
    assert db.get_outbound_count_today("acc2") == 1
    assert db.get_outbound_count_today("acc3") == 0
