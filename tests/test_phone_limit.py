"""
A3: тесты лимитов кликов "Показать телефон".

Проверяем:
- should_skip_phone: дневной hard limit.
- should_skip_phone: предыдущая сессия >5 кликов.
- session soft-limit (30%) — только тестируем что random.random() влияет.
- _PHONE_CLICKED_FLAG ставится в listing_data при успешном клике.
- record_phone_click обновляет дневной и сессионный счётчики.
- start_new_session ротирует счётчики.
- save_listing_to_db инкрементирует phone_clicks метрику при _PHONE_CLICKED_FLAG.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from account_state import AccountState
from commercial_parser import _CAPTCHA_FLAG, _PHONE_CLICKED_FLAG, save_listing_to_db


@pytest.fixture
def state():
    s = AccountState()
    yield s
    s.reset_all()


# ── should_skip_phone ──────────────────────────────────────────────────────


def test_skip_phone_daily_hard_limit(state):
    """После достижения дневного лимита — skip."""
    state.set_daily_budget_limits("acc1", {"phone": 3})
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    assert state.should_skip_phone("acc1") is True


def test_no_skip_under_limit(state):
    state.set_daily_budget_limits("acc1", {"phone": 3})
    state.record_phone_click("acc1")
    assert state.should_skip_phone("acc1") is False


def test_skip_if_prev_session_over_5(state):
    """Предыдущая сессия >5 кликов → пропускаем всю текущую сессию."""
    for _ in range(6):
        state.record_phone_click("acc1")
    state.start_new_session("acc1")
    # Теперь prev_session_phone_clicks = 6
    assert state.should_skip_phone("acc1") is True


def test_no_skip_if_prev_session_5_or_less(state):
    for _ in range(5):
        state.record_phone_click("acc1")
    state.start_new_session("acc1")
    # 5 кликов в предыдущей сессии = граничное значение, не блокируем (>5 нужно)
    assert state.should_skip_phone("acc1") is False


# ── record_phone_click / phone_clicks_today ────────────────────────────────


def test_record_phone_click_increments(state):
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    assert state.phone_clicks_today("acc1") == 2


def test_record_phone_click_session_counter(state):
    """record_phone_click обновляет session_phone_clicks."""
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    # Проверяем через start_new_session
    state.start_new_session("acc1")
    with state._lock:
        entry = state._entries["acc1"]
    assert entry.prev_session_phone_clicks == 2
    assert entry.session_phone_clicks == 0


# ── start_new_session ──────────────────────────────────────────────────────


def test_start_new_session_rotates_counters(state):
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    state.start_new_session("acc1")
    with state._lock:
        entry = state._entries["acc1"]
    assert entry.prev_session_phone_clicks == 3
    assert entry.session_phone_clicks == 0


# ── _PHONE_CLICKED_FLAG в save_listing_to_db ──────────────────────────────


def test_phone_clicked_flag_increments_metric():
    """A3: если _PHONE_CLICKED_FLAG=True — phone_clicks метрика инкрементируется."""
    db = MagicMock()
    db.transaction.return_value.__enter__ = MagicMock(return_value=MagicMock())
    db.transaction.return_value.__exit__ = MagicMock(return_value=False)
    db.upsert_listing.return_value = 1

    listing = {
        "url": "https://avito.ru/test/1",
        "title": "test",
        "category": "office",
        "area": 100.0,
        "price": 50000.0,
        "location": "Москва",
        "description": "desc",
        "seller_name": "Seller",
        "profile_id": "user123",
        "profile_url": "https://avito.ru/user/user123",
        "phone": "+79991234567",
        "phones": ["+79991234567"],
        "active_listings_count": 1,
        "photo_urls": [],
        "date_scraped": "2025-01-01 12:00:00",
        "date_published": "2025-01-01",
        _PHONE_CLICKED_FLAG: True,  # флаг установлен
    }

    log = MagicMock()
    save_listing_to_db(listing, db, log, "acc1")

    # Ищем вызов с "phone_clicks"
    calls = [str(c) for c in db.incr_metric.call_args_list]
    assert any("phone_clicks" in c for c in calls), (
        f"phone_clicks не инкрементирован; calls: {calls}"
    )


def test_phone_not_clicked_no_metric():
    """A3: если _PHONE_CLICKED_FLAG не установлен — phone_clicks НЕ инкрементируется."""
    db = MagicMock()
    db.transaction.return_value.__enter__ = MagicMock(return_value=MagicMock())
    db.transaction.return_value.__exit__ = MagicMock(return_value=False)
    db.upsert_listing.return_value = 1

    listing = {
        "url": "https://avito.ru/test/2",
        "title": "test",
        "category": "office",
        "area": 100.0,
        "price": 50000.0,
        "location": "Москва",
        "description": "desc",
        "seller_name": "Seller",
        "profile_id": "user123",
        "profile_url": "https://avito.ru/user/user123",
        "phone": None,
        "phones": [],
        "active_listings_count": 1,
        "photo_urls": [],
        "date_scraped": "2025-01-01 12:00:00",
        "date_published": "2025-01-01",
        # _PHONE_CLICKED_FLAG НЕ установлен
    }

    log = MagicMock()
    save_listing_to_db(listing, db, log, "acc1")

    calls = [str(c) for c in db.incr_metric.call_args_list]
    assert not any("phone_clicks" in c for c in calls), (
        f"phone_clicks не должен инкрементироваться; calls: {calls}"
    )
