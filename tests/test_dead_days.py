"""
F7: тесты для random «dead days» в account_state.

Покрытие:
- is_dead_day кэширует решение на день: 100 вызовов → одинаковый ответ.
- Cache инвалидируется со сменой даты.
- Weekend boost (×3) — только для Sat/Sun.
- Per-account rate override через set_account_dead_day_rate.
- force_dead_day() ставит True независимо от random/rate.
- Rate=0 → никогда не dead; rate=1 → всегда dead.
"""

from unittest.mock import patch

import pytest

from account_state import AccountState


@pytest.fixture
def state():
    """Свежий AccountState на каждый тест — изоляция от глобального singleton."""
    return AccountState()


# ── Кэширование решения ────────────────────────────────────────────────────


def test_is_dead_day_caches_decision_for_today(state):
    """100 вызовов в одну дату → одинаковый ответ. random.random вызывается 1 раз."""
    state.set_account_dead_day_rate("acc1", 0.5)  # 50% — много шансов на разное
    with patch("account_state.random.random", return_value=0.10) as mock_rng:
        first = state.is_dead_day("acc1")
        for _ in range(100):
            assert state.is_dead_day("acc1") == first
    # Один-единственный бросок.
    mock_rng.assert_called_once()


def test_is_dead_day_resets_on_date_change(state):
    """Меняем «сегодня» → новое решение."""
    state.set_account_dead_day_rate("acc1", 1.0)  # принудительно dead

    # День 1 — всегда dead (rate=1.0).
    with (
        patch("account_state.time.strftime", return_value="2024-01-01"),
        patch("account_state.random.random", return_value=0.5),
    ):
        assert state.is_dead_day("acc1") is True

    # День 2: rate=0.0, новое решение.
    state.set_account_dead_day_rate("acc1", 0.0)
    with (
        patch("account_state.time.strftime", return_value="2024-01-02"),
        patch("account_state.random.random", return_value=0.5),
    ):
        assert state.is_dead_day("acc1") is False


# ── Weekend boost ──────────────────────────────────────────────────────────


def test_weekend_boost_triples_rate(state):
    """В субботу rate ×3 — при base=0.10 эффективная rate=0.30.
    random=0.20 < 0.30 → dead (а в будний день 0.20 > 0.10 — не dead)."""
    state.set_account_dead_day_rate("acc1", 0.10)

    # Будний (понедельник, weekday=0) → rate=0.10, 0.20 не < 0.10 → False.
    with (
        patch("account_state.time.strftime", return_value="2024-01-15"),  # Mon
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.20),
    ):
        mock_lt.return_value.tm_wday = 0
        assert state.is_dead_day("acc1") is False

    # Суббота (новая дата → cache reset). rate ×3 = 0.30, 0.20 < 0.30 → True.
    with (
        patch("account_state.time.strftime", return_value="2024-01-20"),  # Sat
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.20),
    ):
        mock_lt.return_value.tm_wday = 5
        assert state.is_dead_day("acc1") is True


def test_weekend_boost_applies_to_sunday(state):
    """Воскресенье — тоже ×3."""
    state.set_account_dead_day_rate("acc1", 0.10)
    with (
        patch("account_state.time.strftime", return_value="2024-01-21"),  # Sun
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.25),  # 0.25 < 0.30
    ):
        mock_lt.return_value.tm_wday = 6
        assert state.is_dead_day("acc1") is True


# ── Per-account override ──────────────────────────────────────────────────


def test_per_account_rate_override(state):
    """Аккаунт без override использует default 0.05; с override — заданное."""
    # acc1 — дефолт (None в _Entry → rate=0.05), random=0.06 не < 0.05.
    with (
        patch("account_state.time.strftime", return_value="2024-01-15"),
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.06),
    ):
        mock_lt.return_value.tm_wday = 0
        assert state.is_dead_day("acc1") is False

    # acc2 — override на 0.10, random=0.06 < 0.10.
    state.set_account_dead_day_rate("acc2", 0.10)
    with (
        patch("account_state.time.strftime", return_value="2024-01-15"),
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.06),
    ):
        mock_lt.return_value.tm_wday = 0
        assert state.is_dead_day("acc2") is True


def test_set_account_dead_day_rate_none_resets_to_default(state):
    """rate=None → используется default 0.05."""
    state.set_account_dead_day_rate("acc1", 0.5)
    state.set_account_dead_day_rate("acc1", None)
    # Теперь rate=None → default=0.05; 0.06 не < 0.05.
    with (
        patch("account_state.time.strftime", return_value="2024-01-15"),
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.06),
    ):
        mock_lt.return_value.tm_wday = 0
        assert state.is_dead_day("acc1") is False


# ── Граничные значения rate ────────────────────────────────────────────────


def test_rate_zero_never_dead(state):
    """rate=0 → никогда не dead, даже в выходные."""
    state.set_account_dead_day_rate("acc1", 0.0)
    with (
        patch("account_state.time.strftime", return_value="2024-01-20"),  # Sat
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.0001),  # любой малый
    ):
        mock_lt.return_value.tm_wday = 5
        assert state.is_dead_day("acc1") is False


def test_rate_one_always_dead(state):
    """rate=1 → всегда dead, даже когда random=0.99."""
    state.set_account_dead_day_rate("acc1", 1.0)
    with (
        patch("account_state.time.strftime", return_value="2024-01-15"),
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.99),
    ):
        mock_lt.return_value.tm_wday = 0
        assert state.is_dead_day("acc1") is True


# ── force_dead_day (TG /skipday) ──────────────────────────────────────────


def test_force_dead_day_sets_today_dead(state):
    """force_dead_day помечает текущую дату как dead независимо от rate/random."""
    state.set_account_dead_day_rate("acc1", 0.0)  # rate=0 → обычно False
    state.force_dead_day("acc1")
    # Решение для сегодня уже зафиксировано как dead — random не влияет.
    with patch("account_state.random.random", return_value=0.99):
        assert state.is_dead_day("acc1") is True


def test_force_dead_day_only_today(state):
    """force_dead_day помечает ТОЛЬКО сегодня — завтра всё ресет."""
    with patch("account_state.time.strftime", return_value="2024-01-15"):
        state.force_dead_day("acc1")
        with patch("account_state.random.random", return_value=0.99):
            assert state.is_dead_day("acc1") is True

    # Завтра — fresh decision (rate default=0.05 → 0.99 не dead).
    state.set_account_dead_day_rate("acc1", 0.05)
    with (
        patch("account_state.time.strftime", return_value="2024-01-16"),
        patch("account_state.time.localtime") as mock_lt,
        patch("account_state.random.random", return_value=0.99),
    ):
        mock_lt.return_value.tm_wday = 1
        assert state.is_dead_day("acc1") is False
