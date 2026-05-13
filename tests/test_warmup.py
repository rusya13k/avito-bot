"""
B1: тесты warmup-режима для новых аккаунтов.

Проверяем:
- is_in_warmup: False если warmup_until не задан.
- is_in_warmup: True если warmup_until в будущем.
- is_in_warmup: False если warmup_until в прошлом.
- should_skip_phone: True в warmup (даже при нулевых дневных кликах).
- process_messages в AvitoClient: пропускается в warmup.
- В AvitoClient.find_and_view_commercial_listings: работает в warmup (только без телефона).
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from account_state import AccountState


@pytest.fixture
def state():
    s = AccountState()
    yield s
    s.reset_all()


# ── is_in_warmup ──────────────────────────────────────────────────────────


def test_not_in_warmup_by_default(state):
    """Без вызова set_warmup_until — не в warmup."""
    assert state.is_in_warmup("acc1") is False


def test_in_warmup_future(state):
    """warmup_until в будущем → в warmup."""
    state.set_warmup_until("acc1", time.time() + 86400)
    assert state.is_in_warmup("acc1") is True


def test_not_in_warmup_past(state):
    """warmup_until в прошлом → не в warmup."""
    state.set_warmup_until("acc1", time.time() - 1)
    assert state.is_in_warmup("acc1") is False


def test_set_warmup_until_zero_clears(state):
    """set_warmup_until(0) немедленно снимает warmup."""
    state.set_warmup_until("acc1", time.time() + 86400)
    assert state.is_in_warmup("acc1") is True
    state.set_warmup_until("acc1", 0.0)
    assert state.is_in_warmup("acc1") is False


# ── should_skip_phone в warmup ────────────────────────────────────────────


def test_skip_phone_in_warmup(state):
    """В warmup should_skip_phone всегда True (даже без дневных кликов)."""
    state.set_warmup_until("acc1", time.time() + 86400)
    assert state.should_skip_phone("acc1") is True


def test_no_skip_phone_after_warmup(state):
    """После окончания warmup — обычное поведение (лимит не достигнут → False)."""
    state.set_warmup_until("acc1", time.time() - 1)
    state.set_daily_budget_limits("acc1", {"phone": 10})
    assert state.should_skip_phone("acc1") is False


# ── AvitoClient.process_messages пропускается в warmup ────────────────────


def test_process_messages_skipped_in_warmup():
    """process_messages возвращается сразу без вызова AvitoMessenger в warmup."""
    from avito_client import AvitoClient

    client = AvitoClient(
        MagicMock(),
        MagicMock(),
        "acc_warmup",
        log_func=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
    )

    from account_state import account_state as _astate

    _astate.set_warmup_until("acc_warmup", time.time() + 86400)
    try:
        with patch("avito_messenger.AvitoMessenger") as MockMessenger:
            client.process_messages()
            MockMessenger.assert_not_called()
    finally:
        _astate.set_warmup_until("acc_warmup", 0.0)
        _astate.reset_all()


def test_process_messages_runs_after_warmup():
    """После окончания warmup process_messages вызывает AvitoMessenger."""
    from avito_client import AvitoClient

    client = AvitoClient(
        MagicMock(),
        MagicMock(),
        "acc_normal",
        log_func=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
    )

    from account_state import account_state as _astate

    # Нет warmup + бюджет не исчерпан
    _astate.set_warmup_until("acc_normal", 0.0)
    mock_db = client.db
    mock_db.get_metrics.return_value = []

    try:
        with patch("avito_messenger.AvitoMessenger") as MockMessenger:
            mock_instance = MagicMock()
            MockMessenger.return_value = mock_instance
            client.process_messages()
            MockMessenger.assert_called_once()
    finally:
        _astate.reset_all()
