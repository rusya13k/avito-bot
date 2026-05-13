"""
F8: тесты для _pick_cycle_kind / _do_profile_check.

Покрытие:
- Default распределение (full/messenger_only/browse_only/profile_check
  ≈ 0.55/0.20/0.15/0.10) — статистический тест на 10000 выборок ±5%.
- Per-account override через account["cycle_distribution"].
- Cfg override через cfg["cycle_distribution"] (срабатывает только если
  per-account override не задан).
- Warmup-режим: используется _CYCLE_KINDS_WARMUP, messenger_only=0.
- _do_profile_check: визит /profile + опциональный /profile/favorites
  (через random.random() < 0.3).
"""

from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

from bot import (
    _CYCLE_KINDS_DEFAULT,
    _CYCLE_KINDS_WARMUP,
    _do_profile_check,
    _pick_cycle_kind,
)

# ── _pick_cycle_kind: распределение ────────────────────────────────────────


def test_pick_cycle_kind_returns_known_kind():
    """Базовое: возвращаемое значение всегда из набора 4 kinds."""
    valid = set(_CYCLE_KINDS_DEFAULT.keys())
    for _ in range(50):
        kind = _pick_cycle_kind({}, {})
        assert kind in valid


def test_pick_cycle_kind_default_distribution_statistical():
    """10000 выборок без override'ов → пропорции близки к default ±5%."""
    counter: Counter[str] = Counter()
    for _ in range(10000):
        counter[_pick_cycle_kind({}, {})] += 1

    for kind, expected in _CYCLE_KINDS_DEFAULT.items():
        observed = counter[kind] / 10000
        # ±5% — широкая лента, чтобы тест не флакал на CI.
        assert abs(observed - expected) < 0.05, (
            f"{kind}: ожидали {expected:.2f}, получили {observed:.3f}"
        )


def test_per_account_override_takes_precedence():
    """account["cycle_distribution"] перебивает cfg и default."""
    account = {"cycle_distribution": {"full": 1.0}}
    cfg = {"cycle_distribution": {"messenger_only": 1.0}}
    # Все 100 выборок дают "full", т.к. остальные kinds не упомянуты.
    for _ in range(100):
        assert _pick_cycle_kind(account, cfg) == "full"


def test_cfg_override_used_when_no_account_override():
    """Если у account нет cycle_distribution, используется cfg-override."""
    cfg = {"cycle_distribution": {"messenger_only": 1.0}}
    for _ in range(100):
        assert _pick_cycle_kind({}, cfg) == "messenger_only"


def test_warmup_uses_warmup_distribution():
    """is_warmup=True → используется _CYCLE_KINDS_WARMUP, не default."""
    counter: Counter[str] = Counter()
    for _ in range(2000):
        counter[_pick_cycle_kind({}, {}, is_warmup=True)] += 1

    # messenger_only = 0 в warmup → НЕ должен выпадать вообще.
    assert counter["messenger_only"] == 0
    # browse_only / profile_check имеют по 0.40 → ~800 каждый ±10%.
    assert 600 < counter["browse_only"] < 1000
    assert 600 < counter["profile_check"] < 1000
    # full = 0.20 → ~400.
    assert 300 < counter["full"] < 500


def test_warmup_ignores_account_override():
    """В warmup-режиме per-account cycle_distribution игнорируется —
    мы строго используем _CYCLE_KINDS_WARMUP, чтобы не сломать B1."""
    account = {"cycle_distribution": {"messenger_only": 1.0}}
    counter: Counter[str] = Counter()
    for _ in range(500):
        counter[_pick_cycle_kind(account, {}, is_warmup=True)] += 1

    # messenger_only=0 в warmup всегда побеждает над per-account 1.0.
    assert counter["messenger_only"] == 0


def test_pick_cycle_kind_handles_partial_override():
    """Override словарь может содержать только подмножество kinds —
    остальные kinds просто не выпадают (нулевой вес)."""
    account = {"cycle_distribution": {"browse_only": 1.0, "full": 0.0}}
    for _ in range(100):
        kind = _pick_cycle_kind(account, {})
        assert kind in ("browse_only", "full")


# ── _do_profile_check ──────────────────────────────────────────────────────


@pytest.fixture
def driver():
    return MagicMock(name="driver")


def test_do_profile_check_visits_profile(driver):
    """В каждом вызове должен быть GET на /profile."""
    with (
        patch("bot.safe_get", return_value=True) as mock_get,
        patch("bot.hp"),
        patch("bot.random.random", return_value=0.99),  # > 0.3 → favorites НЕ вызовется
    ):
        _do_profile_check(driver, "acc1")

    assert mock_get.call_count == 1
    assert mock_get.call_args.args[1] == "https://www.avito.ru/profile"


def test_do_profile_check_visits_favorites_when_lucky(driver):
    """Если random < 0.3 → дополнительно заходим в /profile/favorites."""
    with (
        patch("bot.safe_get", return_value=True) as mock_get,
        patch("bot.hp"),
        patch("bot.random.random", return_value=0.1),  # < 0.3 → favorites
    ):
        _do_profile_check(driver, "acc1")

    urls = [c.args[1] for c in mock_get.call_args_list]
    assert urls == [
        "https://www.avito.ru/profile",
        "https://www.avito.ru/profile/favorites",
    ]


def test_do_profile_check_skips_favorites_above_threshold(driver):
    """Граничный случай: random ровно 0.3 → не заходит в favorites
    (используем '<', не '<=')."""
    with (
        patch("bot.safe_get", return_value=True) as mock_get,
        patch("bot.hp"),
        patch("bot.random.random", return_value=0.3),
    ):
        _do_profile_check(driver, "acc1")

    assert mock_get.call_count == 1


# ── Sanity: warmup distribution не позволяет messenger_only ──────────────


def test_warmup_kinds_dict_messenger_only_zero():
    """Sanity: _CYCLE_KINDS_WARMUP['messenger_only'] = 0.0 (защита от регрессии)."""
    assert _CYCLE_KINDS_WARMUP["messenger_only"] == 0.0


def test_default_kinds_dict_sums_to_one():
    """Default-веса должны суммироваться к ~1.0 — иначе будет искажение
    в распределении (random.choices нормализует, но удобнее иметь явные 1.0)."""
    total = sum(_CYCLE_KINDS_DEFAULT.values())
    assert abs(total - 1.0) < 0.001
