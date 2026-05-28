"""
S1: тесты для private helpers в bot.py, которые получились при декомпозиции
run_thread (~370 → ~70 строк). Покрываем те, что легко тестируются без
полного Selenium-mock-а:

- _apply_per_account_overrides — G2/F7/A2 setters на account_state
- _apply_warmup_if_new — B1: warmup-режим (валидный/невалидный/отсутствующий
  created_at)
- _check_health_and_log — C1: degraded/critical/healthy ветки, поглощение
  ошибок compute_account_health

_connect_with_retry / _build_avito_client / _run_main_loop требуют тяжёлого
Selenium/AvitoClient-mock'а и покрыты неявно через test_avito_client.py +
smoke-тесты + ручные прогоны.
"""

from unittest.mock import MagicMock, patch

import pytest

from account_state import account_state
from bot import (
    AdsPowerAPI,
    _apply_per_account_overrides,
    _apply_warmup_if_new,
    _check_health_and_log,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Сбрасываем состояние account_state до и после каждого теста, чтобы
    не было перетекания между тестами (account_state — глобальный singleton).
    """
    account_state.reset_all()
    yield
    account_state.reset_all()


# ── _apply_per_account_overrides ─────────────────────────────────────────────


def test_apply_per_account_overrides_calls_all_three_setters():
    """G2/F7/A2: все три setter'а вызываются с account_name."""
    account = {
        "name": "acc1",
        "captcha_cooldown_minutes": 60,
        "dead_day_rate": 0.10,
        "daily_budget_listings": 30,
        "daily_budget_messages": 5,
        "daily_budget_phone": 2,
    }
    with (
        patch.object(account_state, "set_account_cooldown_minutes") as m_cooldown,
        patch.object(account_state, "set_account_dead_day_rate") as m_dead,
        patch.object(account_state, "set_daily_budget_limits") as m_budget,
    ):
        _apply_per_account_overrides(account)

    m_cooldown.assert_called_once_with("acc1", 60)
    m_dead.assert_called_once_with("acc1", 0.10)
    m_budget.assert_called_once_with(
        "acc1",
        {"listings": 30, "messages": 5, "phone": 2},
    )


def test_apply_per_account_overrides_passes_none_for_missing_keys():
    """Отсутствующие per-account ключи передаются как None — это «не трогать
    глобальный дефолт» (см. account_state.set_*)."""
    account = {"name": "acc2"}  # никаких overrides
    with (
        patch.object(account_state, "set_account_cooldown_minutes") as m_cooldown,
        patch.object(account_state, "set_account_dead_day_rate") as m_dead,
        patch.object(account_state, "set_daily_budget_limits") as m_budget,
    ):
        _apply_per_account_overrides(account)

    m_cooldown.assert_called_once_with("acc2", None)
    m_dead.assert_called_once_with("acc2", None)
    m_budget.assert_called_once_with(
        "acc2",
        {"listings": None, "messages": None, "phone": None},
    )


# ── _apply_warmup_if_new ─────────────────────────────────────────────────────


def test_apply_warmup_no_created_at_does_nothing():
    """B1: если created_at не задан — не трогаем warmup-state."""
    account = {"name": "acc3"}
    with patch.object(account_state, "set_warmup_until") as m_set:
        _apply_warmup_if_new(account, "acc3")
    m_set.assert_not_called()


def test_apply_warmup_invalid_created_at_logs_and_continues():
    """B1: невалидный created_at — warning в лог, исключение не пробрасывается."""
    account = {"name": "acc4", "created_at": "not-a-date"}
    with (
        patch.object(account_state, "set_warmup_until") as m_set,
        patch("bot._bot_logger") as m_logger,
    ):
        _apply_warmup_if_new(account, "acc4")
    m_set.assert_not_called()
    m_logger.warning.assert_called_once()


def test_apply_warmup_valid_created_at_sets_warmup_until():
    """B1: валидный created_at → set_warmup_until вызывается с timestamp."""
    account = {
        "name": "acc5",
        "created_at": "2025-01-01",
        "warmup_days": 5,
    }
    with patch.object(account_state, "set_warmup_until") as m_set:
        _apply_warmup_if_new(account, "acc5")
    m_set.assert_called_once()
    args = m_set.call_args.args
    assert args[0] == "acc5"
    # Timestamp 2025-01-01 + 5 дней = 2025-01-06
    import datetime as _dt

    expected_end = _dt.datetime(2025, 1, 1).timestamp() + 5 * 86400
    assert abs(args[1] - expected_end) < 1


def test_apply_warmup_when_in_warmup_sets_listings_limit():
    """B1: если is_in_warmup() True → выставляется warmup_daily_listings."""
    # Кладём created_at на завтра, чтобы аккаунт точно был в warmup
    import datetime as _dt

    tomorrow = (_dt.datetime.now() + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    account = {
        "name": "acc6",
        "created_at": tomorrow,
        "warmup_days": 3,
        "warmup_daily_listings": 10,
    }
    with patch.object(account_state, "set_daily_budget_limits") as m_budget:
        _apply_warmup_if_new(account, "acc6")

    # set_daily_budget_limits должен быть вызван с лимитом на listings = 10
    m_budget.assert_called_once_with("acc6", {"listings": 10})


def test_apply_warmup_when_warmup_already_ended_no_listings_limit():
    """B1: если warmup закончился (created_at в далёком прошлом) — лимит на
    listings НЕ выставляется (только set_warmup_until для актуальности)."""
    account = {
        "name": "acc7",
        "created_at": "2020-01-01",  # давно
        "warmup_days": 3,
    }
    with patch.object(account_state, "set_daily_budget_limits") as m_budget:
        _apply_warmup_if_new(account, "acc7")
    m_budget.assert_not_called()


# ── _check_health_and_log ────────────────────────────────────────────────────


def test_check_health_healthy_does_not_apply_restrictions():
    """C1: mode='healthy' — не вызываем apply_health_restrictions."""
    db = MagicMock()
    fake_health = {"mode": "healthy", "score": 0.0, "captchas_7d": 0, "listings_7d": 100}
    with (
        patch("account_state.compute_account_health", return_value=fake_health),
        patch("account_state.apply_health_restrictions") as m_apply,
    ):
        _check_health_and_log("acc1", db)
    m_apply.assert_not_called()


def test_check_health_degraded_applies_restrictions():
    """C1: mode='degraded' — вызываем apply_health_restrictions."""
    db = MagicMock()
    fake_health = {
        "mode": "degraded",
        "score": 0.05,
        "captchas_7d": 5,
        "listings_7d": 100,
    }
    with (
        patch("account_state.compute_account_health", return_value=fake_health),
        patch("account_state.apply_health_restrictions") as m_apply,
    ):
        _check_health_and_log("acc1", db)
    m_apply.assert_called_once_with("acc1", fake_health)


def test_check_health_critical_applies_restrictions():
    """C1: mode='critical' — вызываем apply_health_restrictions."""
    db = MagicMock()
    fake_health = {
        "mode": "critical",
        "score": 0.20,
        "captchas_7d": 20,
        "listings_7d": 100,
    }
    with (
        patch("account_state.compute_account_health", return_value=fake_health),
        patch("account_state.apply_health_restrictions") as m_apply,
    ):
        _check_health_and_log("acc1", db)
    m_apply.assert_called_once_with("acc1", fake_health)


def test_check_health_swallows_exceptions():
    """C1: ошибки в compute_account_health НЕ должны блокировать запуск
    потока. Helper тихо проглатывает — лог попадёт в exception-handler выше."""
    db = MagicMock()
    with patch(
        "account_state.compute_account_health",
        side_effect=RuntimeError("DB down"),
    ):
        # Ошибка не должна пробрасываться
        _check_health_and_log("acc1", db)


# ── AdsPowerAPI.is_profile_running ─────────────────────────────────────────


def test_is_profile_running_true_when_active(monkeypatch):
    """API вернул status=Active → True."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"code": 0, "data": {"status": "Active"}}
    monkeypatch.setattr("requests.get", lambda *a, **kw: mock_resp)

    api = AdsPowerAPI("http://localhost:50325")
    assert api.is_profile_running("uid1") is True


def test_is_profile_running_false_when_inactive(monkeypatch):
    """API вернул status=Inactive → False."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"code": 0, "data": {"status": "Inactive"}}
    monkeypatch.setattr("requests.get", lambda *a, **kw: mock_resp)

    api = AdsPowerAPI("http://localhost:50325")
    assert api.is_profile_running("uid1") is False


def test_is_profile_running_false_on_error(monkeypatch):
    """API недоступен → False (не падаем)."""
    import requests

    monkeypatch.setattr(
        "requests.get", lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError())
    )

    api = AdsPowerAPI("http://localhost:50325")
    assert api.is_profile_running("uid1") is False


def test_is_profile_running_false_on_nonzero_code(monkeypatch):
    """API вернул code != 0 → False."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"code": -1, "msg": "not found"}
    monkeypatch.setattr("requests.get", lambda *a, **kw: mock_resp)

    api = AdsPowerAPI("http://localhost:50325")
    assert api.is_profile_running("uid1") is False
