"""
A3: тесты лимитов кликов "Показать телефон" (код удалён, тесты account_state
оставлены на случай возврата A3-логики).

Проверяем:
- should_skip_phone: дневной hard limit.
- should_skip_phone: предыдущая сессия >5 кликов.
- session soft-limit (30%) — только тестируем что random.random() влияет.
- record_phone_click обновляет дневной и сессионный счётчики.
- start_new_session ротирует счётчики.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from account_state import AccountState


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
