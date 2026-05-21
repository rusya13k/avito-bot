"""
T19: тесты для cycle_pause — lognormal regular + long breaks (обед/ужин).
"""

from __future__ import annotations

import datetime as dt
import random
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cycle_pause import (  # noqa: E402
    DEFAULT_LONG_BREAK_MAX_MIN,
    DEFAULT_LONG_BREAK_MIN_MIN,
    DEFAULT_LONG_BREAKS_PER_DAY,
    DINNER_WINDOW,
    LUNCH_WINDOW,
    _is_meal_window,
    _lognormal_seconds,
    _resolve,
    _uniform_seconds,
    pick_cycle_pause,
)

# ── Stub AccountState ─────────────────────────────────────────────────────────


class _StubAccountState:
    """Минимальный мок AccountState для cycle_pause тестов."""

    def __init__(self, taken_today: int = 0):
        self._lock = threading.RLock()
        self._taken = taken_today
        self.record_calls: list[str] = []

    def count_long_breaks_today(self, account_name: str) -> int:
        return self._taken

    def record_long_break(self, account_name: str) -> None:
        self.record_calls.append(account_name)
        self._taken += 1


# ── _is_meal_window ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hour,expected",
    [
        (0, False),
        (8, False),
        (11, False),
        (12, True),
        (13, True),
        (14, False),
        (15, False),
        (17, False),
        (18, True),
        (19, True),
        (20, True),
        (21, False),
        (23, False),
    ],
)
def test_is_meal_window(hour, expected):
    assert _is_meal_window(hour) is expected


def test_meal_windows_constants_are_reasonable():
    """LUNCH/DINNER окна должны быть в разумных пределах."""
    assert LUNCH_WINDOW[0] < LUNCH_WINDOW[1] <= 24
    assert DINNER_WINDOW[0] < DINNER_WINDOW[1] <= 24
    # Не пересекаются.
    assert LUNCH_WINDOW[1] <= DINNER_WINDOW[0]


# ── _resolve ──────────────────────────────────────────────────────────────────


def test_resolve_account_override_wins():
    assert _resolve({"x": 5}, {"x": 10}, "x", 0) == 5


def test_resolve_cfg_used_when_no_account():
    assert _resolve(None, {"x": 10}, "x", 0) == 10


def test_resolve_default_when_neither():
    assert _resolve(None, None, "x", 42) == 42


def test_resolve_skips_none_in_account():
    """account["x"] = None → fall through к cfg."""
    assert _resolve({"x": None}, {"x": 10}, "x", 0) == 10


def test_resolve_skips_missing_keys():
    """Если ключа нет в account — берёт из cfg."""
    assert _resolve({}, {"x": 10}, "x", 0) == 10


# ── _lognormal_seconds ───────────────────────────────────────────────────────


def test_lognormal_seconds_returns_minimum_floor():
    """Sample не может быть меньше lo (clamp)."""
    rng = random.Random(0)
    samples = [_lognormal_seconds(30, 90, rng=rng) for _ in range(200)]
    # All ≥ lo_min minutes = 30 * 60 = 1800 sec.
    assert min(samples) >= 30 * 60


def test_lognormal_seconds_typical_range():
    """Sample обычно в [lo, hi*2] минутах."""
    rng = random.Random(0)
    samples = [_lognormal_seconds(30, 90, rng=rng) for _ in range(2000)]
    # Upper bound: hi*2 = 180 min = 10800 sec.
    assert max(samples) <= 180 * 60 + 0.001


def test_lognormal_seconds_has_long_tail():
    """Lognormal имеет правый хвост — должны быть значения выше mid."""
    rng = random.Random(0)
    samples = [_lognormal_seconds(30, 90, rng=rng) for _ in range(2000)]
    mid = 60 * 60  # 60 минут в секундах
    above_mid = sum(1 for s in samples if s > mid)
    # Хотя бы 10% значений больше mid.
    assert above_mid > 200, f"only {above_mid} samples above mid (expected >200)"


def test_lognormal_seconds_zero_range_returns_lo():
    """hi <= lo → возвращаем lo (в секундах)."""
    assert _lognormal_seconds(30, 30) == 30 * 60.0
    assert _lognormal_seconds(30, 20) == 30 * 60.0


# ── _uniform_seconds ─────────────────────────────────────────────────────────


def test_uniform_seconds_in_range():
    rng = random.Random(0)
    samples = [_uniform_seconds(120, 300, rng=rng) for _ in range(500)]
    assert all(120 * 60 <= s <= 300 * 60 for s in samples)


def test_uniform_seconds_zero_range_returns_lo():
    assert _uniform_seconds(120, 120) == 120 * 60.0


# ── pick_cycle_pause: regular path ──────────────────────────────────────────


def test_pick_returns_regular_when_no_long_break_allowed():
    """long_breaks_per_day=0 → всегда regular."""
    state = _StubAccountState(taken_today=0)
    rng = random.Random(0)
    secs, label = pick_cycle_pause(
        {"long_breaks_per_day": 0},
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),  # lunch window
        rng=rng,
    )
    assert label == "regular"
    assert secs > 0
    assert state.record_calls == []


def test_pick_returns_regular_when_limit_reached():
    """taken_today >= long_breaks_per_day → всегда regular."""
    state = _StubAccountState(taken_today=2)
    rng = random.Random(0)
    secs, label = pick_cycle_pause(
        {"long_breaks_per_day": 2},
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label == "regular"
    assert state.record_calls == []


def test_pick_regular_uses_lognormal_seconds_range():
    """Regular pause в пределах [pause_min, pause_max*2] минут."""
    state = _StubAccountState(taken_today=2)  # запрет long-break
    rng = random.Random(42)
    samples_secs = []
    for _ in range(200):
        secs, label = pick_cycle_pause(
            {"session_pause_min": 30, "session_pause_max": 90, "long_breaks_per_day": 2},
            {},
            account_state=state,
            account_name="acc1",
            now=dt.datetime(2025, 1, 1, 13, 0),
            rng=rng,
        )
        assert label == "regular"
        samples_secs.append(secs)
    # Все попадают в [pause_min*60, pause_max*60*2].
    assert min(samples_secs) >= 30 * 60
    assert max(samples_secs) <= 90 * 60 * 2 + 0.001


# ── pick_cycle_pause: long_break path ───────────────────────────────────────


def test_pick_returns_long_break_in_window_with_chance_1():
    """chance_in_window=1.0 + lunch window → всегда long_break."""
    state = _StubAccountState(taken_today=0)
    rng = random.Random(0)
    secs, label = pick_cycle_pause(
        {
            "long_break_chance_in_window": 1.0,
            "long_break_chance_out_window": 0.0,
            "long_breaks_per_day": 2,
        },
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),  # lunch window
        rng=rng,
    )
    assert label == "long_break"
    # Long break длительность по дефолту [120, 300] минут.
    assert DEFAULT_LONG_BREAK_MIN_MIN * 60 <= secs <= DEFAULT_LONG_BREAK_MAX_MIN * 60
    # Side effect: счётчик инкрементирован.
    assert state.record_calls == ["acc1"]


def test_pick_returns_regular_in_window_with_chance_0():
    """chance_in_window=0.0 → regular, даже в lunch."""
    state = _StubAccountState(taken_today=0)
    rng = random.Random(0)
    secs, label = pick_cycle_pause(
        {
            "long_break_chance_in_window": 0.0,
            "long_break_chance_out_window": 0.0,
            "long_breaks_per_day": 2,
        },
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label == "regular"
    assert state.record_calls == []


def test_pick_long_break_out_window_less_likely():
    """В out-window вероятность long break ниже (default 0.05 vs 0.30)."""
    rng = random.Random(0)
    in_window_long_breaks = 0
    out_window_long_breaks = 0

    for _ in range(2000):
        state = _StubAccountState(taken_today=0)
        _, label = pick_cycle_pause(
            {"long_breaks_per_day": 5},  # высокий лимит, чтобы не упирались
            {},
            account_state=state,
            account_name="acc",
            now=dt.datetime(2025, 1, 1, 13, 0),  # lunch
            rng=rng,
        )
        if label == "long_break":
            in_window_long_breaks += 1

    for _ in range(2000):
        state = _StubAccountState(taken_today=0)
        _, label = pick_cycle_pause(
            {"long_breaks_per_day": 5},
            {},
            account_state=state,
            account_name="acc",
            now=dt.datetime(2025, 1, 1, 9, 0),  # 9 утра — не еда
            rng=rng,
        )
        if label == "long_break":
            out_window_long_breaks += 1

    # В window должно быть кратно больше long break'ов.
    assert in_window_long_breaks > out_window_long_breaks * 3, (
        f"in={in_window_long_breaks}, out={out_window_long_breaks}"
    )


def test_pick_records_long_break_in_state():
    """record_long_break вызывается ровно 1 раз при long break."""
    state = _StubAccountState(taken_today=0)
    rng = random.Random(0)
    _, label = pick_cycle_pause(
        {"long_break_chance_in_window": 1.0, "long_breaks_per_day": 2},
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label == "long_break"
    assert state.record_calls == ["acc1"]


def test_pick_long_break_respects_dinner_window():
    """Ужин 18-21 тоже триггерит in-window chance."""
    state = _StubAccountState(taken_today=0)
    rng = random.Random(0)
    _, label = pick_cycle_pause(
        {"long_break_chance_in_window": 1.0, "long_breaks_per_day": 2},
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 19, 0),  # 19:00 ужин
        rng=rng,
    )
    assert label == "long_break"


def test_pick_uses_account_overrides_for_long_break_duration():
    """account override long_break_min_min/max_min используется."""
    state = _StubAccountState(taken_today=0)
    rng = random.Random(0)
    secs, label = pick_cycle_pause(
        {
            "long_break_min_min": 60,  # 1h
            "long_break_max_min": 120,  # 2h
            "long_break_chance_in_window": 1.0,
            "long_breaks_per_day": 2,
        },
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label == "long_break"
    assert 60 * 60 <= secs <= 120 * 60


def test_pick_cfg_fallback():
    """Если account=None, берёт значения из cfg."""
    state = _StubAccountState(taken_today=0)
    rng = random.Random(0)
    secs, label = pick_cycle_pause(
        None,
        {
            "long_break_chance_in_window": 1.0,
            "long_break_min_min": 30,
            "long_break_max_min": 60,
            "long_breaks_per_day": 1,
        },
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label == "long_break"
    assert 30 * 60 <= secs <= 60 * 60


def test_pick_uses_defaults_when_no_config():
    """account=None, cfg=None → используются DEFAULT_* константы."""
    state = _StubAccountState(taken_today=DEFAULT_LONG_BREAKS_PER_DAY)
    rng = random.Random(0)
    # Лимит исчерпан → должен быть regular.
    secs, label = pick_cycle_pause(
        None,
        None,
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label == "regular"


# ── Real AccountState integration ────────────────────────────────────────────


def test_real_account_state_counts_long_breaks():
    """Используем настоящий AccountState — счётчик инкрементируется."""
    from account_state import AccountState

    state = AccountState()
    rng = random.Random(0)

    assert state.count_long_breaks_today("acc1") == 0

    # Первая long_break.
    _, label1 = pick_cycle_pause(
        {"long_break_chance_in_window": 1.0, "long_breaks_per_day": 2},
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label1 == "long_break"
    assert state.count_long_breaks_today("acc1") == 1

    # Вторая.
    _, label2 = pick_cycle_pause(
        {"long_break_chance_in_window": 1.0, "long_breaks_per_day": 2},
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label2 == "long_break"
    assert state.count_long_breaks_today("acc1") == 2

    # Третья — лимит, regular.
    _, label3 = pick_cycle_pause(
        {"long_break_chance_in_window": 1.0, "long_breaks_per_day": 2},
        {},
        account_state=state,
        account_name="acc1",
        now=dt.datetime(2025, 1, 1, 13, 0),
        rng=rng,
    )
    assert label3 == "regular"
    assert state.count_long_breaks_today("acc1") == 2


def test_real_account_state_resets_counter_next_day(monkeypatch):
    """При смене календарной даты счётчик сбрасывается."""
    import account_state as as_module
    from account_state import AccountState

    state = AccountState()

    monkeypatch.setattr(as_module.time, "strftime", lambda fmt, *a: "2025-01-01")
    state.record_long_break("acc1")
    state.record_long_break("acc1")
    assert state.count_long_breaks_today("acc1") == 2

    # Следующий день — сброс.
    monkeypatch.setattr(as_module.time, "strftime", lambda fmt, *a: "2025-01-02")
    assert state.count_long_breaks_today("acc1") == 0


def test_real_account_state_independent_accounts():
    """Счётчики разных аккаунтов независимы."""
    from account_state import AccountState

    state = AccountState()
    state.record_long_break("acc1")
    state.record_long_break("acc1")
    state.record_long_break("acc2")

    assert state.count_long_breaks_today("acc1") == 2
    assert state.count_long_breaks_today("acc2") == 1
    assert state.count_long_breaks_today("acc3") == 0
