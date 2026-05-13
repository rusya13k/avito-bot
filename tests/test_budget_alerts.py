"""
C2: тесты TG-алертов при 80%/100% дневного бюджета.

Проверяем:
- check_budget_alert: None когда < 80%.
- check_budget_alert: "80" при 80-99%.
- check_budget_alert: "100" при 100%+.
- De-duplication: один алерт за день на каждый порог.
- get_effective_limit: возвращает per-account или default лимит.
"""

import pytest

from account_state import AccountState


@pytest.fixture
def state():
    s = AccountState()
    yield s
    s.reset_all()


# ── check_budget_alert ────────────────────────────────────────────────────


def test_no_alert_below_80(state):
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 79) is None


def test_alert_80_at_boundary(state):
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 80) == "80"


def test_alert_80_mid_range(state):
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 90) == "80"


def test_alert_100_at_boundary(state):
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 100) == "100"


def test_alert_100_over_limit(state):
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 150) == "100"


def test_dedup_80_same_day(state):
    """Второй вызов с теми же параметрами в тот же день → None."""
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 85) == "80"
    assert state.check_budget_alert("acc1", "listings", 85) is None


def test_dedup_100_same_day(state):
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 100) == "100"
    assert state.check_budget_alert("acc1", "listings", 100) is None


def test_dedup_independent_per_action(state):
    """Алерты для разных actions не мешают друг другу."""
    state.set_daily_budget_limits("acc1", {"listings": 100, "messages": 50})
    assert state.check_budget_alert("acc1", "listings", 80) == "80"
    assert state.check_budget_alert("acc1", "messages", 40) == "80"


def test_80_then_100_same_day(state):
    """80%-алерт не блокирует 100%-алерт."""
    state.set_daily_budget_limits("acc1", {"listings": 100})
    assert state.check_budget_alert("acc1", "listings", 80) == "80"
    assert state.check_budget_alert("acc1", "listings", 100) == "100"


# ── get_effective_limit ───────────────────────────────────────────────────


def test_get_effective_limit_default(state):
    from account_state import DEFAULT_DAILY_BUDGET

    limit = state.get_effective_limit("new_acc", "listings")
    assert limit == DEFAULT_DAILY_BUDGET["listings"]


def test_get_effective_limit_per_account(state):
    state.set_daily_budget_limits("acc1", {"listings": 42})
    assert state.get_effective_limit("acc1", "listings") == 42
