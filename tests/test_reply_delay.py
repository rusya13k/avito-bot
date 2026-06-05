"""
F5: тесты для AvitoMessenger._should_reply_now (реалистичная задержка ответа).

Логика, которую тестируем:
  1. Если диалог уже помечен ignored в account_state — снимаем пометку
     и продолжаем (ignore отключён — отвечаем всем).
  2. Считаем возраст последнего in-сообщения через БД и сравниваем
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


# ── Legacy ignored dialogs — now unignored ──────────────────────────────


def test_ignored_dialog_gets_unignored(log):
    """Если диалог был помечен ignored ранее — снимаем пометку и отвечаем
    (ignore отключён — отвечаем всем)."""
    messenger = AvitoMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
        account_name="acc_f5",
    )
    messenger.db.get_first_in_message_age_seconds.return_value = 60 * 60
    chat = [{"direction": "in", "text": "привет"}]
    # Помечаем диалог как ignored вручную
    account_state.mark_dialog_ignored("acc_f5", 42)
    assert account_state.is_dialog_ignored("acc_f5", 42) is True
    # Теперь _should_reply_now должен снять ignore и продолжить
    with patch("avito_messenger.random.lognormvariate", return_value=10.0):
        assert messenger._should_reply_now(dialog_id=42, chat_history=chat, log_func=log) is True
    # Ignore снят
    assert account_state.is_dialog_ignored("acc_f5", 42) is False


def test_new_dialog_always_answered(log):
    """Новый диалог (без out-сообщений) — отвечаем, ignore отключён.
    Раньше был 5% шанс игнора, теперь всегда True."""
    messenger = AvitoMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        db_manager=MagicMock(),
        llm_classifier=MagicMock(),
        account_name="acc_f5",
    )
    messenger.db.get_first_in_message_age_seconds.return_value = 60 * 60
    chat = [{"direction": "in", "text": "первое сообщение"}]
    with patch("avito_messenger.random.lognormvariate", return_value=10.0):
        assert messenger._should_reply_now(dialog_id=99, chat_history=chat, log_func=log) is True
    assert account_state.is_dialog_ignored("acc_f5", 99) is False


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


def test_existing_persistent_ignore_gets_unignored(log, messenger):
    """Если диалог уже помечен ignored ранее — снимаем пометку и продолжаем
    к задержке. БД и lognormal вызываются (ignore не short-circuits)."""
    account_state.mark_dialog_ignored("acc_f5", 555)
    chat = [{"direction": "in", "text": "x"}]
    messenger.db.get_first_in_message_age_seconds.return_value = 60 * 60
    with patch("avito_messenger.random.lognormvariate", return_value=10.0):
        assert messenger._should_reply_now(dialog_id=555, chat_history=chat, log_func=log) is True
    # Ignore снят
    assert account_state.is_dialog_ignored("acc_f5", 555) is False


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
