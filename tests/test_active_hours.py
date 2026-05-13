"""
B2: тесты активного окна времени.

Проверяем:
- _is_in_active_hours: True внутри окна, False снаружи.
- _is_in_active_hours: берёт per-account override над глобальным cfg.
- _seconds_until_active_hours: возвращает > 0 когда вне окна.
- _seconds_until_active_hours: возвращает 0 или переход на следующий день.
"""

from unittest.mock import patch

import pytest

from bot import _is_in_active_hours, _seconds_until_active_hours

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
