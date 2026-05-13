"""
B4: тесты long-cooldown при множественных капчах за 24ч.

Проверяем:
- captcha_hits_24h: считает только за последние 24ч.
- mark_captcha: при 1-2 капчах — обычный cooldown.
- mark_captcha: при >= 3 капчах — cooldown >= 4 ч.
- mark_captcha: при >= 5 капчах — cooldown до следующего дня.
- mark_captcha: при >= 5 капчах — бюджеты вдвое.
- Старые timestamps (>24ч) не считаются.
"""

import time
from unittest.mock import patch

import pytest

from account_state import DEFAULT_DAILY_BUDGET, AccountState


@pytest.fixture
def state():
    s = AccountState()
    yield s
    s.reset_all()


# ── captcha_hits_24h ──────────────────────────────────────────────────────


def test_hits_24h_empty(state):
    assert state.captcha_hits_24h("acc1") == 0


def test_hits_24h_counts_recent(state):
    state.mark_captcha("acc1")
    state.mark_captcha("acc1")
    assert state.captcha_hits_24h("acc1") == 2


def test_hits_24h_ignores_old(state):
    """Timestamps старше 24ч не учитываются."""
    old_ts = time.time() - 90000  # 25 часов назад
    with state._lock:
        entry = state._get("acc1")
        entry.captcha_timestamps = [old_ts, old_ts]
    # Добавим одну свежую через mark_captcha
    state.mark_captcha("acc1")
    assert state.captcha_hits_24h("acc1") == 1


# ── Обычный cooldown (< 3 капч) ───────────────────────────────────────────


def test_normal_cooldown_under_3_hits(state):
    """1-2 капчи → обычный short cooldown."""
    now = time.time()
    state.mark_captcha("acc1")
    until = state.mark_captcha("acc1")
    # Cooldown должен быть порядка DEFAULT_CAPTCHA_COOLDOWN_MINUTES (30 мин)
    assert until - now < 3600  # точно не 4+ часа


# ── Long-cooldown при >= 3 капчах ─────────────────────────────────────────


def test_long_cooldown_at_3_hits(state):
    """3 капчи за 24ч → cooldown >= 4 часов (240 мин)."""
    now = time.time()
    for _ in range(3):
        until = state.mark_captcha("acc1")
    assert until - now >= 4 * 3600 - 1  # >= 4 часа (с допуском в 1 сек)


def test_long_cooldown_at_4_hits(state):
    """4 капчи → по-прежнему long-cooldown (4-8 ч)."""
    now = time.time()
    for _ in range(4):
        until = state.mark_captcha("acc1")
    assert until - now >= 4 * 3600 - 1


# ── Cooldown до следующего дня при >= 5 капчах ────────────────────────────


def test_next_day_cooldown_at_5_hits(state):
    """5+ капч → cooldown до следующего календарного дня."""
    import datetime

    for _ in range(5):
        until = state.mark_captcha("acc1")
    # Окончание cooldown должно быть >= завтра полночь (UTC offset не важен —
    # проверяем что до cooldown > 0 и что он далеко)
    tomorrow_start = (
        datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        + datetime.timedelta(days=1)
    ).timestamp()
    assert until >= tomorrow_start - 1


def test_budget_halved_at_5_hits(state):
    """5+ капч → daily_budget_overrides для listings вдвое."""
    state.set_daily_budget_limits("acc1", {"listings": 80, "messages": 30, "phone": 25})
    for _ in range(5):
        state.mark_captcha("acc1")
    with state._lock:
        entry = state._get("acc1")
    assert entry.daily_budget_overrides["listings"] == 40
    assert entry.daily_budget_overrides["messages"] == 15
    assert entry.daily_budget_overrides["phone"] == 12


def test_budget_halved_uses_default_if_no_override(state):
    """При отсутствии per-account override — берёт дефолтный бюджет и режет вдвое."""
    for _ in range(5):
        state.mark_captcha("acc1")
    with state._lock:
        entry = state._get("acc1")
    assert entry.daily_budget_overrides["listings"] == DEFAULT_DAILY_BUDGET["listings"] // 2


def test_captcha_hits_counter_increments(state):
    """captcha_hits (total) инкрементируется с каждым mark_captcha."""
    state.mark_captcha("acc1")
    state.mark_captcha("acc1")
    assert state.captcha_hits("acc1") == 2
