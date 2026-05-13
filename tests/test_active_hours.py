"""
B2 + F6: тесты активного окна времени.

Проверяем:
- _is_in_active_hours: True внутри окна, False снаружи.
- _is_in_active_hours: берёт per-account override над глобальным cfg.
- _seconds_until_active_hours: возвращает > 0 когда вне окна.
- _seconds_until_active_hours: возвращает 0 или переход на следующий день.

F6: probabilistic active hours — заменяет бинарную модель на вероятностную.
- _active_probability: дефолтные веса (утро ~95%, ночь ~5%).
- Per-account / cfg activity_pattern override.
- Жёсткое окно (active_hours_start/end) перебивает: вне окна prob=0.
"""

from unittest.mock import patch

import pytest

from bot import (
    _ACTIVITY_BY_HOUR,
    _active_probability,
    _is_in_active_hours,
    _seconds_until_active_hours,
)

DEFAULT_CFG = {"active_hours_start": 9, "active_hours_end": 23}


# ── _is_in_active_hours ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hour, expected",
    [
        (9, True),  # ровно start
        (12, True),  # середина дня
        (22, True),  # за час до end
        (23, False),  # ровно end — уже вне окна
        (0, False),  # полночь
        (8, False),  # час до start
    ],
)
def test_is_in_active_hours_defaults(hour, expected):
    import time

    fake_time = time.struct_time((2025, 1, 1, hour, 0, 0, 0, 1, 0))
    with patch("bot.time") as mock_time:
        mock_time.localtime.return_value = fake_time
        result = _is_in_active_hours({}, DEFAULT_CFG)
    assert result is expected


def test_per_account_override_wins():
    """Per-account active_hours_start/end перебивает глобальный cfg."""
    import time

    account = {"active_hours_start": 10, "active_hours_end": 20}
    cfg = {"active_hours_start": 9, "active_hours_end": 23}

    # 9:00 — в глобальном окне, но НЕ в per-account (start=10)
    fake_time = time.struct_time((2025, 1, 1, 9, 0, 0, 0, 1, 0))
    with patch("bot.time") as mock_time:
        mock_time.localtime.return_value = fake_time
        assert _is_in_active_hours(account, cfg) is False

    # 10:00 — в per-account окне
    fake_time2 = time.struct_time((2025, 1, 1, 10, 0, 0, 0, 1, 0))
    with patch("bot.time") as mock_time:
        mock_time.localtime.return_value = fake_time2
        assert _is_in_active_hours(account, cfg) is True


# ── _seconds_until_active_hours ───────────────────────────────────────────


def test_seconds_until_when_before_window():
    """08:30 → start=9 → нужно 30 мин = 1800 сек."""
    import time

    fake_time = time.struct_time((2025, 1, 1, 8, 30, 0, 0, 1, 0))
    with patch("bot.time") as mock_time:
        mock_time.localtime.return_value = fake_time
        result = _seconds_until_active_hours({}, DEFAULT_CFG)
    assert abs(result - 1800) < 2


def test_seconds_until_wraps_to_next_day():
    """23:30 → start=9 → нужно до 09:00 следующего дня = ~9.5 ч."""
    import time

    fake_time = time.struct_time((2025, 1, 1, 23, 30, 0, 0, 1, 0))
    with patch("bot.time") as mock_time:
        mock_time.localtime.return_value = fake_time
        result = _seconds_until_active_hours({}, DEFAULT_CFG)
    expected = (9 * 3600) + (86400 - 23 * 3600 - 30 * 60)  # до 09:00 следующего дня
    assert abs(result - expected) < 2


# ── F6: _active_probability ───────────────────────────────────────────────


def test_default_pattern_high_at_morning_peak():
    """F6: дефолт-паттерн даёт ~0.95 в 10:00 (пик активности)."""
    p = _active_probability({}, {}, hour=10)
    assert p >= 0.9, f"Утренний пик должен быть высокий, получили {p}"


def test_default_pattern_low_at_night():
    """F6: дефолт-паттерн даёт ~0.005 в 3:00 (ночь — почти невидим)."""
    p = _active_probability({}, {}, hour=3)
    assert p < 0.05, f"Ночь должна быть низкой, получили {p}"


def test_default_pattern_medium_at_lunch():
    """F6: 12:00 — обед, prob ~0.55 (ниже утреннего пика 0.95)."""
    p = _active_probability({}, {}, hour=12)
    assert 0.30 < p < 0.70, f"Обед должен быть средним, получили {p}"


def test_per_account_pattern_overrides_default():
    """F6: account.activity_pattern перебивает дефолт."""
    custom = {0: 0.99, 1: 0.99, 12: 0.01}  # инвертированный паттерн
    account = {"activity_pattern": custom}
    # В дефолтном паттерне 0:00 = 0.02, тут — 0.99.
    assert _active_probability(account, {}, hour=0) == 0.99
    # В дефолтном паттерне 12:00 = 0.55, тут — 0.01.
    assert _active_probability(account, {}, hour=12) == 0.01


def test_cfg_pattern_used_when_no_account_override():
    """F6: cfg.activity_pattern используется если per-account не задан."""
    cfg = {"activity_pattern": {10: 0.10}}  # утром почти не активен
    assert _active_probability({}, cfg, hour=10) == 0.10


def test_account_pattern_wins_over_cfg():
    """F6: per-account имеет приоритет над cfg."""
    account = {"activity_pattern": {10: 0.99}}
    cfg = {"activity_pattern": {10: 0.10}}
    assert _active_probability(account, cfg, hour=10) == 0.99


def test_hard_window_forces_zero_outside():
    """F6 ← B2 совместимость: active_hours_start/end задано → вне окна prob=0,
    независимо от activity_pattern."""
    account = {
        "active_hours_start": 10,
        "active_hours_end": 20,
        "activity_pattern": {3: 0.99, 9: 0.99, 21: 0.99},  # высокие prob, но...
    }
    # 3:00 — вне окна [10, 20).
    assert _active_probability(account, {}, hour=3) == 0.0
    # 9:00 — тоже вне (start=10 включающее).
    assert _active_probability(account, {}, hour=9) == 0.0
    # 21:00 — тоже вне (end=20 исключающее).
    assert _active_probability(account, {}, hour=21) == 0.0
    # 15:00 — в окне → берётся pattern (по дефолту 0.85, у нас нет 15 в pattern → fallback 0.5).
    assert _active_probability(account, {}, hour=15) == 0.5


def test_hard_window_allows_pattern_inside():
    """F6: внутри окна активного — pattern применяется как обычно."""
    account = {"active_hours_start": 9, "active_hours_end": 23}
    # 10:00 ∈ [9, 23) → дефолтный pattern → 0.95.
    p = _active_probability(account, {}, hour=10)
    assert p == _ACTIVITY_BY_HOUR[10]


def test_pattern_falls_back_to_default_for_missing_hour():
    """F6: если в pattern нет ключа для этого часа — 0.5 (среднее)."""
    custom = {0: 0.99}  # только полночь известна
    account = {"activity_pattern": custom}
    assert _active_probability(account, {}, hour=14) == 0.5


def test_json_string_keys_supported():
    """F6: pattern из JSON будет иметь ключи-строки — нормализуем."""
    # При загрузке из JSON dict ключи всегда строки.
    pattern_from_json = {"0": 0.99, "10": 0.10}
    account = {"activity_pattern": pattern_from_json}
    # Int hour=10 → ищем pattern[10], потом pattern["10"]=0.10.
    assert _active_probability(account, {}, hour=10) == 0.10


def test_default_pattern_covers_all_24_hours():
    """F6 sanity: дефолт-паттерн содержит все 24 часа [0..23]."""
    assert set(_ACTIVITY_BY_HOUR.keys()) == set(range(24))
    # Все значения — валидные вероятности.
    for h, p in _ACTIVITY_BY_HOUR.items():
        assert 0.0 <= p <= 1.0, f"hour={h}: prob={p} вне [0,1]"
