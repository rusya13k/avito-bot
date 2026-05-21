"""
T17: тесты для smarter captcha cooldown политик.

- mark_captcha принимает captcha_type (avito_phone / avito_listing /
  avito_message_send / yandex_search / generic) и применяет multiplier.
- После Avito-капчи (любой type кроме yandex_search) outbound отключается
  на 24h: is_outbound_disabled() возвращает True.
- _pick_cycle_kind при outbound_disabled=True зануляет outbound_only.
- configure_from_cfg парсит captcha_cooldown_multipliers и
  outbound_disable_hours_after_captcha.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account_state as as_module  # noqa: E402
from account_state import (  # noqa: E402
    CAPTCHA_COOLDOWN_MULTIPLIERS,
    DEFAULT_CAPTCHA_COOLDOWN_MINUTES,
    AccountState,
    configure_from_cfg,
)

# ── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def state():
    """Свежий AccountState на каждый тест."""
    return AccountState()


@pytest.fixture
def reset_globals():
    """Backup и restore module-level globals между тестами."""
    saved_mults = dict(CAPTCHA_COOLDOWN_MULTIPLIERS)
    saved_disable = as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA
    saved_default = as_module.DEFAULT_CAPTCHA_COOLDOWN_MINUTES
    yield
    CAPTCHA_COOLDOWN_MULTIPLIERS.clear()
    CAPTCHA_COOLDOWN_MULTIPLIERS.update(saved_mults)
    as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA = saved_disable
    as_module.DEFAULT_CAPTCHA_COOLDOWN_MINUTES = saved_default


# ── mark_captcha с captcha_type ──────────────────────────────────────────────


def test_mark_captcha_default_type_is_generic(state):
    """Без аргумента captcha_type — multiplier = generic = 1.0 → стандартный cooldown."""
    now = time.time()
    until = state.mark_captcha("acc1")
    expected = now + DEFAULT_CAPTCHA_COOLDOWN_MINUTES * 60.0
    assert abs(until - expected) < 5


def test_mark_captcha_avito_phone_doubles_cooldown(state):
    """avito_phone (multiplier 2.0) → cooldown в 2 раза дольше generic."""
    now = time.time()
    until = state.mark_captcha("acc1", captcha_type="avito_phone")
    expected = now + DEFAULT_CAPTCHA_COOLDOWN_MINUTES * 60.0 * 2.0
    assert abs(until - expected) < 5


def test_mark_captcha_yandex_search_halves_cooldown(state):
    """yandex_search (multiplier 0.5) → cooldown в 2 раза короче generic."""
    now = time.time()
    until = state.mark_captcha("acc1", captcha_type="yandex_search")
    expected = now + DEFAULT_CAPTCHA_COOLDOWN_MINUTES * 60.0 * 0.5
    assert abs(until - expected) < 5


def test_mark_captcha_avito_listing_uses_1_5x(state):
    """avito_listing (multiplier 1.5) → cooldown 1.5x от generic."""
    now = time.time()
    until = state.mark_captcha("acc1", captcha_type="avito_listing")
    expected = now + DEFAULT_CAPTCHA_COOLDOWN_MINUTES * 60.0 * 1.5
    assert abs(until - expected) < 5


def test_mark_captcha_unknown_type_falls_back_to_1x(state):
    """Неизвестный captcha_type → multiplier 1.0 (default fallback)."""
    now = time.time()
    until = state.mark_captcha("acc1", captcha_type="weirdo_unknown")
    expected = now + DEFAULT_CAPTCHA_COOLDOWN_MINUTES * 60.0 * 1.0
    assert abs(until - expected) < 5


def test_mark_captcha_explicit_minutes_combined_with_multiplier(state):
    """cooldown_minutes=10 + avito_phone (2x) → 20 мин."""
    now = time.time()
    until = state.mark_captcha("acc1", cooldown_minutes=10, captcha_type="avito_phone")
    expected = now + 10 * 60.0 * 2.0
    assert abs(until - expected) < 5


# ── outbound_disable после avito-капчи ──────────────────────────────────────


def test_avito_phone_disables_outbound(state):
    """avito_phone → is_outbound_disabled() True на 24h."""
    assert state.is_outbound_disabled("acc1") is False
    state.mark_captcha("acc1", captcha_type="avito_phone")
    assert state.is_outbound_disabled("acc1") is True


def test_avito_listing_disables_outbound(state):
    state.mark_captcha("acc1", captcha_type="avito_listing")
    assert state.is_outbound_disabled("acc1") is True


def test_avito_message_send_disables_outbound(state):
    state.mark_captcha("acc1", captcha_type="avito_message_send")
    assert state.is_outbound_disabled("acc1") is True


def test_generic_disables_outbound(state):
    """Generic (default) тоже триггерит — back-compat для старых вызовов."""
    state.mark_captcha("acc1")  # default = generic
    assert state.is_outbound_disabled("acc1") is True


def test_yandex_search_does_NOT_disable_outbound(state):
    """yandex_search НЕ блокирует outbound — это прокси-issue."""
    state.mark_captcha("acc1", captcha_type="yandex_search")
    assert state.is_outbound_disabled("acc1") is False


def test_outbound_disable_24h_default(state):
    """get_outbound_disabled_until ≈ now + 24h."""
    now = time.time()
    state.mark_captcha("acc1", captcha_type="avito_phone")
    until = state.get_outbound_disabled_until("acc1")
    expected = now + 24 * 3600
    # 24h = 86400 sec, погрешность 5 сек.
    assert abs(until - expected) < 5


def test_outbound_disabled_for_unknown_account_returns_false(state):
    """Аккаунт без записи в state — outbound НЕ disabled."""
    assert state.is_outbound_disabled("never_seen") is False
    assert state.get_outbound_disabled_until("never_seen") == 0.0


def test_yandex_search_does_not_overwrite_existing_disable(state):
    """Avito-капча → 24h disable. Yandex-капча — не сбросит её."""
    state.mark_captcha("acc1", captcha_type="avito_phone")
    until_after_avito = state.get_outbound_disabled_until("acc1")
    assert until_after_avito > 0

    state.mark_captcha("acc1", captcha_type="yandex_search")
    until_after_yandex = state.get_outbound_disabled_until("acc1")
    # Yandex не трогает поле — должно остаться.
    assert until_after_yandex == until_after_avito


def test_repeated_avito_captcha_extends_disable(state):
    """Повторный avito_phone после 1h → max(prev, now+24h) = новый позже."""
    # Первая капча.
    state.mark_captcha("acc1", captcha_type="avito_phone")
    first_until = state.get_outbound_disabled_until("acc1")

    # Через "час" — fake time +3600.
    with patch.object(as_module.time, "time", return_value=time.time() + 3600):
        state.mark_captcha("acc1", captcha_type="avito_phone")
        new_until = state.get_outbound_disabled_until("acc1")
    # Новый дедлайн — позже первого (на час).
    assert new_until > first_until


# ── outbound_disable expires after 24h ──────────────────────────────────────


def test_outbound_disable_expires_after_24h(state):
    """После прохождения 24h is_outbound_disabled() возвращает False."""
    state.mark_captcha("acc1", captcha_type="avito_phone")
    assert state.is_outbound_disabled("acc1") is True

    # Симулируем "через 25 часов".
    future = time.time() + 25 * 3600
    with patch.object(as_module.time, "time", return_value=future):
        assert state.is_outbound_disabled("acc1") is False


# ── _pick_cycle_kind с outbound_disabled ────────────────────────────────────


def test_pick_cycle_kind_zeros_outbound_when_disabled():
    """outbound_disabled=True → outbound_only НИКОГДА не выпадает."""
    from bot import _pick_cycle_kind

    counts = {
        "full": 0,
        "messenger_only": 0,
        "browse_only": 0,
        "profile_check": 0,
        "outbound_only": 0,
    }
    for _ in range(2000):
        kind = _pick_cycle_kind({}, {}, is_warmup=False, outbound_disabled=True)
        counts[kind] += 1
    assert counts["outbound_only"] == 0
    # Остальные kinds разбираются.
    others = sum(v for k, v in counts.items() if k != "outbound_only")
    assert others == 2000


def test_pick_cycle_kind_outbound_present_when_enabled():
    """outbound_disabled=False → outbound_only иногда выпадает."""
    from bot import _pick_cycle_kind

    counts = {"outbound_only": 0}
    for _ in range(500):
        kind = _pick_cycle_kind({}, {}, is_warmup=False, outbound_disabled=False)
        if kind == "outbound_only":
            counts["outbound_only"] += 1
    # При weight 0.30 в 500 итераций ожидаем ~150 выпадений.
    assert counts["outbound_only"] > 50


def test_pick_cycle_kind_warmup_already_zero_outbound():
    """В warmup outbound_only=0 уже изначально. outbound_disabled не должно
    ломать поведение (просто та же ноль)."""
    from bot import _pick_cycle_kind

    for _ in range(500):
        kind = _pick_cycle_kind({}, {}, is_warmup=True, outbound_disabled=True)
        assert kind != "outbound_only"


# ── configure_from_cfg ──────────────────────────────────────────────────────


def test_configure_parses_multipliers(reset_globals):
    """captcha_cooldown_multipliers подхватываются."""
    configure_from_cfg(
        {
            "captcha_cooldown_multipliers": {
                "avito_phone": 3.0,
                "yandex_search": 0.25,
            }
        }
    )
    assert CAPTCHA_COOLDOWN_MULTIPLIERS["avito_phone"] == 3.0
    assert CAPTCHA_COOLDOWN_MULTIPLIERS["yandex_search"] == 0.25
    # Не трогаемые остаются на default'е.
    assert CAPTCHA_COOLDOWN_MULTIPLIERS["avito_listing"] == 1.5


def test_configure_ignores_unknown_type(reset_globals, caplog):
    """Неизвестный captcha_type → warning, multiplier не добавляется."""
    import logging

    with caplog.at_level(logging.WARNING):
        configure_from_cfg({"captcha_cooldown_multipliers": {"weirdo_unknown": 5.0}})
    assert "weirdo_unknown" not in CAPTCHA_COOLDOWN_MULTIPLIERS


def test_configure_ignores_negative_multiplier(reset_globals):
    """Multiplier <= 0 ignored."""
    saved = CAPTCHA_COOLDOWN_MULTIPLIERS["avito_phone"]
    configure_from_cfg({"captcha_cooldown_multipliers": {"avito_phone": -1.0}})
    # Не изменился.
    assert CAPTCHA_COOLDOWN_MULTIPLIERS["avito_phone"] == saved


def test_configure_outbound_disable_hours(reset_globals):
    """outbound_disable_hours_after_captcha подхватывается."""
    configure_from_cfg({"outbound_disable_hours_after_captcha": 12})
    assert as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA == 12.0


def test_configure_outbound_disable_zero_means_disabled(reset_globals):
    """outbound_disable_hours_after_captcha=0 → disable выключен."""
    configure_from_cfg({"outbound_disable_hours_after_captcha": 0})
    assert as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA == 0.0

    state = AccountState()
    state.mark_captcha("acc1", captcha_type="avito_phone")
    assert state.is_outbound_disabled("acc1") is False


def test_configure_negative_disable_hours_ignored(reset_globals):
    """Отрицательное значение → не применяется."""
    saved = as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA
    configure_from_cfg({"outbound_disable_hours_after_captcha": -5})
    assert as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA == saved


def test_configure_invalid_disable_hours_ignored(reset_globals):
    """Невалидное значение → не падает, не меняет default."""
    saved = as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA
    configure_from_cfg({"outbound_disable_hours_after_captcha": "abc"})
    assert as_module.OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA == saved


# ── Back-compat: existing tests should still pass ────────────────────────────


def test_mark_captcha_no_args_preserves_old_behavior(state):
    """Тесты в test_long_cooldown.py / test_accounts.py не должны падать."""
    until = state.mark_captcha("acc1")
    assert until > time.time()
    # captcha_hits увеличивается.
    until2 = state.mark_captcha("acc2")
    assert until2 > time.time()


def test_b4_long_cooldown_still_works_with_types(state):
    """B4 long-cooldown (3 капчи за 24h → 4-8h cooldown) работает с типами."""
    now = time.time()
    for _ in range(3):
        until = state.mark_captcha("acc1", captcha_type="avito_phone")
    # 3 капчи → long cooldown >= 4ч (B4 ВЫИГРЫВАЕТ multiplier'у).
    assert until - now >= 4 * 3600 - 1
