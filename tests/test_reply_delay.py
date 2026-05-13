"""
F5: тесты для AvitoMessenger._should_reply_now (реалистичная задержка ответа).

Логика, которую тестируем:
  1. Если диалог уже помечен ignored в account_state — False.
  2. Новый диалог (без out-сообщений) с шансом `ignore_new_dialog_chance`
     помечается ignored и возвращает False.
  3. Иначе считаем возраст последнего in-сообщения через БД и сравниваем
     с lognormal target (clamped к [min_reply_age_min, max_reply_age_min]).

LLM не вызывается (вся логика — до `generate_response`). БД и random мокаем.
"""

from unittest.mock import MagicMock, patch

import pytest

from account_state import account_state
from avito_messenger import AvitoMessenger


@pytest.fixture
def messenger():
    """AvitoMessenger с замоканной БД и нулевым ignore-шансом по умолчанию.
    Каждый тест может явно переопределить kwargs."""
    db = MagicMock(name="db")
    return AvitoMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        db_manager=db,
        llm_classifier=MagicMock(),
        account_name="acc_f5",
        # по умолчанию: ignore выключен, чтобы тесты задержек были детерминированы
        ignore_new_dialog_chance=0.0,
    )


@pytest.fixture
def log():
    """no-op log_func — мы не проверяем строки логов, только поведение."""
    return MagicMock(name="log")


@pytest.fixture(autouse=True)
def reset_account_state():
    """F5b: чистим ignored_dialogs между тестами, чтобы один тест не текал в другой."""
    yield
    # Удаляем _Entry для тестового account, чтобы set был пустым.
    with account_state._lock:
        account_state._entries.pop("acc_f5", None)


# ── Возраст и lognormal target ────────────────────────────────────────────


def test_replies_to_old_message(messenger, log):
    """Возраст 60 мин > max(15, lognormal=20) → отвечаем."""
    messenger.db.get_first_in_message_age_seconds.return_value = 60 * 60  # 60 минут
    chat = [{"direction": "in", "text": "hello"}]
    with patch("avito_messenger.random.lognormvariate", return_value=20.0):
        assert messenger._should_reply_now(dialog_id=1, chat_history=chat, log_func=log) is True


def test_skips_recent_in_message(messenger, log):
    """Возраст 5 мин < target=30 мин → False (отложить)."""
    messenger.db.get_first_in_message_age_seconds.return_value = 5 * 60
    chat = [{"direction": "in", "text": "hello"}]
    with patch("avito_messenger.random.lognormvariate", return_value=30.0):
        assert messenger._should_reply_now(dialog_id=1, chat_history=chat, log_func=log) is False


def test_target_clamped_to_min(messenger, log):
    """Lognormal даёт 1 → clamped к min_reply_age_min (15). Возраст 10 < 15 → False."""
    messenger.db.get_first_in_message_age_seconds.return_value = 10 * 60
    chat = [{"direction": "in", "text": "x"}]
    with patch("avito_messenger.random.lognormvariate", return_value=1.0):
        assert messenger._should_reply_now(dialog_id=1, chat_history=chat, log_func=log) is False


def test_target_clamped_to_max(messenger, log):
    """Lognormal даёт 99999 → clamped к max_reply_age_min (600).
    Возраст 700 мин > 600 → True."""
    messenger.db.get_first_in_message_age_seconds.return_value = 700 * 60
    chat = [{"direction": "in", "text": "x"}]
    with patch("avito_messenger.random.lognormvariate", return_value=99999.0):
        assert messenger._should_reply_now(dialog_id=1, chat_history=chat, log_func=log) is True


def test_db_returns_none_means_reply(messenger, log):
    """Если БД не знает первого появления (например, новая запись) — отвечаем."""
    messenger.db.get_first_in_message_age_seconds.return_value = None
    chat = [{"direction": "in", "text": "x"}]
    with patch("avito_messenger.random.lognormvariate", return_value=99999.0):
        # lognormal не должен влиять — мы возвращаем True ДО его проверки.
        assert messenger._should_reply_now(dialog_id=1, chat_history=chat, log_func=log) is True


# ── F5b: 5% ignore для новых диалогов ─────────────────────────────────────


def test_ignore_rolls_only_for_new_dialogs(log):
    """Если в чате уже есть наш OUT — ignore-roll НЕ должен срабатывать
    (даже при 100% chance), мы продолжаем переписку."""
    messenger = AvitoMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
        account_name="acc_f5",
        ignore_new_dialog_chance=1.0,  # должен бы игнорить ВСЁ — но only new
    )
    messenger.db.get_first_in_message_age_seconds.return_value = 60 * 60
    chat = [
        {"direction": "in", "text": "привет"},
        {"direction": "out", "text": "здравствуйте"},
        {"direction": "in", "text": "вопрос"},
    ]
    with patch("avito_messenger.random.lognormvariate", return_value=10.0):
        # min_reply_age_min=15 → target=15, age=60 → ОК → True
        assert messenger._should_reply_now(dialog_id=42, chat_history=chat, log_func=log) is True
    # Проверим что ignore флага НЕ выставился.
    assert account_state.is_dialog_ignored("acc_f5", 42) is False


def test_ignore_marks_new_dialog_when_unlucky(log):
    """random < ignore_chance + chat без out → mark + False, и в следующий
    раз тот же диалог тоже False (даже если повезло)."""
    messenger = AvitoMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
        account_name="acc_f5",
        ignore_new_dialog_chance=0.5,
    )
    chat = [{"direction": "in", "text": "первое сообщение"}]

    # Первый вызов: random=0.1 < 0.5 → ignore.
    with patch("avito_messenger.random.random", return_value=0.1):
        assert messenger._should_reply_now(dialog_id=99, chat_history=chat, log_func=log) is False

    # account_state теперь помнит ignore → даже если повезёт, всё равно False.
    assert account_state.is_dialog_ignored("acc_f5", 99) is True
    with patch("avito_messenger.random.random", return_value=0.99):
        assert messenger._should_reply_now(dialog_id=99, chat_history=chat, log_func=log) is False


def test_ignore_does_not_mark_when_lucky(log):
    """random >= ignore_chance → НЕ помечаем; решение делегируется задержке."""
    messenger = AvitoMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
        account_name="acc_f5",
        ignore_new_dialog_chance=0.5,
    )
    messenger.db.get_first_in_message_age_seconds.return_value = 60 * 60
    chat = [{"direction": "in", "text": "новый"}]
    with (
        patch("avito_messenger.random.random", return_value=0.99),  # > 0.5 → не игнорим
        patch("avito_messenger.random.lognormvariate", return_value=10.0),  # target=15 (min)
    ):
        assert messenger._should_reply_now(dialog_id=7, chat_history=chat, log_func=log) is True
    assert account_state.is_dialog_ignored("acc_f5", 7) is False


def test_existing_persistent_ignore_short_circuits(log, messenger):
    """Если диалог уже помечен ignored ранее (в этом процессе) — мгновенно
    возвращаем False, не трогая БД и не делая random-броски."""
    account_state.mark_dialog_ignored("acc_f5", 555)
    chat = [{"direction": "in", "text": "x"}]
    with patch("avito_messenger.random.lognormvariate") as mock_log:
        assert messenger._should_reply_now(dialog_id=555, chat_history=chat, log_func=log) is False
    # БД и lognormal даже не должны вызываться.
    messenger.db.get_first_in_message_age_seconds.assert_not_called()
    mock_log.assert_not_called()


# ── Default — F5 выключаем, поведение совместимо со старым ───────────────


def test_zero_chance_zero_min_age_replies_always(log):
    """Если все F5-параметры обнулены — всегда отвечаем (back-compat
    для пользователя, который не хочет F5)."""
    messenger = AvitoMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
        account_name="acc_f5",
        min_reply_age_min=0.0,
        max_reply_age_min=0.0,
        ignore_new_dialog_chance=0.0,
    )
    messenger.db.get_first_in_message_age_seconds.return_value = 0.0  # только что
    chat = [{"direction": "in", "text": "x"}]
    # max=0 → target clamped к 0 → age=0 >= 0 → True.
    assert messenger._should_reply_now(dialog_id=1, chat_history=chat, log_func=log) is True
