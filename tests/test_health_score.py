"""
C1: тесты per-account health score.

Проверяем:
- compute_account_health: mode "healthy" без данных.
- compute_account_health: mode "healthy" при captcha_rate < 0.01.
- compute_account_health: mode "warning" при 0.01 <= rate < 0.05.
- compute_account_health: mode "degraded" при rate >= 0.05 (< 5 капч).
- compute_account_health: mode "critical" при rate >= 0.05 (>= 5 капч).
- apply_health_restrictions: снижает бюджет вдвое.
- apply_health_restrictions: устанавливает warmup_until.
- apply_health_restrictions: ничего не делает в healthy-режиме.
"""

import time
from unittest.mock import MagicMock

import pytest

from account_state import (
    AccountState,
    compute_account_health,
)


def _make_db(listings: int, captchas: int) -> MagicMock:
    """Создаёт mock db_manager с заданными метриками за 7 дней."""
    db = MagicMock()
    rows = []
    if listings > 0:
        rows.append({"metric": "listings_parsed", "value": listings})
    if captchas > 0:
        rows.append({"metric": "captcha_hits", "value": captchas})
    db.get_metrics.return_value = rows
    return db


# ── compute_account_health ────────────────────────────────────────────────


def test_healthy_no_data():
    db = _make_db(0, 0)
    h = compute_account_health("acc1", db)
    assert h["mode"] == "healthy"
    assert h["score"] == 0.0


def test_healthy_low_rate():
    # 1 капча / 200 листингов = 0.005 < 0.01
    db = _make_db(200, 1)
    h = compute_account_health("acc1", db)
    assert h["mode"] == "healthy"
    assert h["listings_7d"] == 200
    assert h["captchas_7d"] == 1


def test_warning_mid_rate():
    # 3 капчи / 100 листингов = 0.03 (0.01 <= x < 0.05)
    db = _make_db(100, 3)
    h = compute_account_health("acc1", db)
    assert h["mode"] == "warning"


def test_degraded_high_rate_few_captchas():
    # 3 капчи / 50 листингов = 0.06 >= 0.05, но капч < 5
    db = _make_db(50, 3)
    h = compute_account_health("acc1", db)
    assert h["mode"] == "degraded"
    assert h["score"] >= 0.05


def test_critical_high_rate_many_captchas():
    # 6 капч / 60 листингов = 0.1 >= 0.05, капч >= 5
    db = _make_db(60, 6)
    h = compute_account_health("acc1", db)
    assert h["mode"] == "critical"


def test_db_none_returns_healthy():
    h = compute_account_health("acc1", None)
    assert h["mode"] == "healthy"


# ── apply_health_restrictions ─────────────────────────────────────────────


@pytest.fixture
def state():
    s = AccountState()
    yield s
    s.reset_all()


def test_apply_restrictions_degraded_halves_budget(state):
    state.set_daily_budget_limits("acc1", {"listings": 80, "messages": 30, "phone": 25})
    health = {"mode": "degraded", "score": 0.06, "captchas_7d": 3, "listings_7d": 50}
    state.apply_health_restrictions("acc1", health)
    assert state.get_effective_limit("acc1", "listings") == 40
    assert state.get_effective_limit("acc1", "messages") == 15
    assert state.get_effective_limit("acc1", "phone") == 12


def test_apply_restrictions_sets_warmup(state):
    health = {"mode": "critical", "score": 0.1, "captchas_7d": 6, "listings_7d": 60}
    before = time.time()
    state.apply_health_restrictions("acc1", health)
    assert state.is_in_warmup("acc1") is True
    # Warmup должен быть минимум 3 дня
    with state._lock:
        entry = state._get("acc1")
    assert entry.warmup_until >= before + 3 * 86400 - 1


def test_apply_restrictions_healthy_no_change(state):
    state.set_daily_budget_limits("acc1", {"listings": 80})
    health = {"mode": "healthy", "score": 0.005, "captchas_7d": 1, "listings_7d": 200}
    state.apply_health_restrictions("acc1", health)
    # Лимит не должен измениться
    assert state.get_effective_limit("acc1", "listings") == 80


def test_apply_restrictions_does_not_overwrite_longer_warmup(state):
    """Если warmup_until уже дальше — не перезаписываем."""
    far_future = time.time() + 30 * 86400  # 30 дней
    state.set_warmup_until("acc1", far_future)
    health = {"mode": "degraded", "score": 0.06, "captchas_7d": 3, "listings_7d": 50}
    state.apply_health_restrictions("acc1", health)
    with state._lock:
        entry = state._get("acc1")
    assert entry.warmup_until == far_future
