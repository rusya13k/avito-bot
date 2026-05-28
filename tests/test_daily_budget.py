"""
A2: тесты дневных бюджетов.

Проверяем:
- check_daily_budget("acc1", "listings") возвращает False если лимит достигнут.
- Per-account override перекрывает глобальный дефолт.
- "phone" action работает через in-memory счётчик (без DB).
- remaining_budget корректно считает остаток.
- A2-guard в AvitoClient.find_and_view_commercial_listings и process_messages.
"""

from unittest.mock import MagicMock, patch

import pytest

from account_state import DEFAULT_DAILY_BUDGET, AccountState


@pytest.fixture
def state():
    s = AccountState()
    yield s
    s.reset_all()


# ── check_daily_budget: listings / messages (DB-backed) ───────────────────


def test_check_listings_within_budget(state):
    db = MagicMock()
    db.get_metrics.return_value = [{"value": 50}]
    assert state.check_daily_budget("acc1", "listings", db) is True


def test_check_listings_over_budget(state):
    db = MagicMock()
    db.get_metrics.return_value = [{"value": 80}]  # == default limit
    assert state.check_daily_budget("acc1", "listings", db) is False


def test_check_listings_over_budget_per_account_override(state):
    """Per-account override = 40, 40 достигнуто → False."""
    state.set_daily_budget_limits("acc1", {"listings": 40})
    db = MagicMock()
    db.get_metrics.return_value = [{"value": 40}]
    assert state.check_daily_budget("acc1", "listings", db) is False


def test_check_listings_under_override(state):
    """Per-account override = 40, 39 → True."""
    state.set_daily_budget_limits("acc1", {"listings": 40})
    db = MagicMock()
    db.get_metrics.return_value = [{"value": 39}]
    assert state.check_daily_budget("acc1", "listings", db) is True


def test_check_messages_budget(state):
    db = MagicMock()
    db.get_metrics.return_value = [{"value": 30}]  # == default 30
    assert state.check_daily_budget("acc1", "messages", db) is False


def test_db_error_assumes_within_budget(state):
    """При ошибке БД — считаем бюджет в порядке (не блокируем)."""
    db = MagicMock()
    db.get_metrics.side_effect = Exception("db error")
    assert state.check_daily_budget("acc1", "listings", db) is True


def test_no_db_assumes_within_budget(state):
    """Если db_manager не передан — считаем бюджет в порядке."""
    assert state.check_daily_budget("acc1", "listings") is True


# ── check_daily_budget: phone (in-memory) ─────────────────────────────────


def test_phone_within_budget(state):
    # 0 кликов → OK
    assert state.check_daily_budget("acc1", "phone") is True


def test_phone_over_budget(state):
    limit = DEFAULT_DAILY_BUDGET["phone"]  # 25
    for _ in range(limit):
        state.record_phone_click("acc1")
    assert state.check_daily_budget("acc1", "phone") is False


def test_phone_custom_limit(state):
    state.set_daily_budget_limits("acc1", {"phone": 3})
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    assert state.check_daily_budget("acc1", "phone") is False
    # другой аккаунт не затронут
    assert state.check_daily_budget("acc2", "phone") is True


# ── remaining_budget ───────────────────────────────────────────────────────


def test_remaining_budget_full(state):
    db = MagicMock()
    db.get_metrics.return_value = []  # нет данных → 0 использовано
    rem = state.remaining_budget("acc1", "listings", db)
    assert rem == DEFAULT_DAILY_BUDGET["listings"]


def test_remaining_budget_partial(state):
    db = MagicMock()
    db.get_metrics.return_value = [{"value": 50}]
    rem = state.remaining_budget("acc1", "listings", db)
    assert rem == DEFAULT_DAILY_BUDGET["listings"] - 50


def test_remaining_budget_phone(state):
    state.record_phone_click("acc1")
    state.record_phone_click("acc1")
    rem = state.remaining_budget("acc1", "phone")
    assert rem == DEFAULT_DAILY_BUDGET["phone"] - 2


# ── AvitoClient guard: find_and_view ──────────────────────────────────────


def test_avito_client_listings_budget_guard():
    """A2: find_and_view_commercial_listings возвращает (0,0,0) при исчерпанном бюджете."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()
    log = MagicMock()

    client = AvitoClient(driver, wait, "acc1", log_func=log, db_manager=db)

    with patch("account_state.account_state") as mock_state:
        mock_state.check_daily_budget.return_value = False
        mock_state.remaining_budget.return_value = 0
        result = client.find_and_view_commercial_listings()

    assert result == (0, 0, 0)
    mock_state.check_daily_budget.assert_called_once_with("acc1", "listings", db)
    log.assert_called()  # должен был залогировать


def test_avito_client_listings_proceeds_if_within_budget():
    """A2: при бюджете в норме — делегирует в bot.find_and_view_commercial_listings."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()

    client = AvitoClient(driver, wait, "acc1", db_manager=db)

    with (
        patch("account_state.account_state") as mock_state,
        patch("bot.find_and_view_commercial_listings", return_value=(3, 1, 0)) as mock_fn,
    ):
        mock_state.check_daily_budget.return_value = True
        result = client.find_and_view_commercial_listings()

    assert result == (3, 1, 0)
    mock_fn.assert_called_once()


def test_avito_client_browse_budget_guard():
    """F12: browse_commercial_categories не открывает browse при исчерпанном listings-бюджете."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()
    log = MagicMock()

    client = AvitoClient(driver, wait, "acc1", log_func=log, db_manager=db)

    with (
        patch("account_state.account_state") as mock_state,
        patch("bot.browse_commercial_categories") as mock_fn,
    ):
        mock_state.check_daily_budget.return_value = False
        result = client.browse_commercial_categories()

    assert result is None
    mock_state.check_daily_budget.assert_called_once_with("acc1", "listings", db)
    mock_fn.assert_not_called()
    log.assert_called_once()


def test_avito_client_browse_proceeds_if_within_budget():
    """F12: если listings-бюджет доступен — browse делегирует в bot.browse_commercial_categories."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()

    client = AvitoClient(driver, wait, "acc1", db_manager=db)

    with (
        patch("account_state.account_state") as mock_state,
        patch("bot.browse_commercial_categories", return_value="ok") as mock_fn,
    ):
        mock_state.check_daily_budget.return_value = True
        result = client.browse_commercial_categories(num_categories=1, ads_per_category=1)

    assert result == "ok"
    mock_state.check_daily_budget.assert_called_once_with("acc1", "listings", db)
    mock_fn.assert_called_once()
    assert mock_fn.call_args.args[:3] == (driver, wait, "acc1")
    assert mock_fn.call_args.kwargs["num_categories"] == 1
    assert mock_fn.call_args.kwargs["ads_per_category"] == 1


def test_avito_client_messages_budget_guard():
    """A2: process_messages возвращает без работы при исчерпанном бюджете."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()
    llm = MagicMock()
    log = MagicMock()

    client = AvitoClient(driver, wait, "acc1", log_func=log, db_manager=db, llm_classifier=llm)

    with (
        patch("account_state.account_state") as mock_state,
        patch("avito_messenger.AvitoMessenger") as mock_messenger,
    ):
        mock_state.check_daily_budget.return_value = False
        mock_state.remaining_budget.return_value = 0
        client.process_messages()

    mock_messenger.assert_not_called()
    log.assert_called()


# ── C2: 100% budget alert ──────────────────────────────────────────────────


def test_avito_client_listings_100_alert_triggers_warning():
    """C2: при alert==100 для листингов → logger.warning (однократно, de-dup)."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()
    log = MagicMock()

    client = AvitoClient(driver, wait, "acc1", log_func=log, db_manager=db)

    with (
        patch("account_state.account_state") as mock_state,
        patch("avito_client.logger") as mock_logger,
    ):
        mock_state._get_daily_total_from_db.return_value = 80  # лимит исчерпан
        mock_state.check_budget_alert.return_value = "100"
        mock_state.check_daily_budget.return_value = False
        mock_state.remaining_budget.return_value = 0
        mock_state.get_effective_limit.return_value = 80

        client.find_and_view_commercial_listings()

    mock_logger.warning.assert_called_once()
    call_args = mock_logger.warning.call_args[0]
    assert "100" in call_args[0] or "исчерпан" in call_args[0]


def test_avito_client_messages_100_alert_triggers_warning():
    """C2: при alert==100 для сообщений → logger.warning (однократно, de-dup)."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()
    llm = MagicMock()
    log = MagicMock()

    client = AvitoClient(driver, wait, "acc1", log_func=log, db_manager=db, llm_classifier=llm)

    with (
        patch("account_state.account_state") as mock_state,
        patch("avito_client.logger") as mock_logger,
        patch("avito_messenger.AvitoMessenger"),
    ):
        mock_state.is_in_warmup.return_value = False
        mock_state._get_daily_total_from_db.return_value = 30  # лимит исчерпан
        mock_state.check_budget_alert.return_value = "100"
        mock_state.check_daily_budget.return_value = False
        mock_state.remaining_budget.return_value = 0
        mock_state.get_effective_limit.return_value = 30

        client.process_messages()

    mock_logger.warning.assert_called_once()
    call_args = mock_logger.warning.call_args[0]
    assert "100" in call_args[0] or "исчерпан" in call_args[0]
