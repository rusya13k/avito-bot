"""
S2: тесты для _edit_or_send helper в TelegramController.

Helper заменил 11 копипастов try-edit-except-send в tg_bot.py. Проверяем:
- Успешный edit → НЕ должен дёргать send_message.
- Edit падает → должен сделать send_message с теми же chat_id и kb.
- markup=None корректно прокидывается (kb=None в _edit_or_send).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tg_ctrl(tmp_path, monkeypatch):
    """TelegramController с замоканным TeleBot. _setup() регистрирует
    handler'ы на mock-объекте — это безопасно для нашего теста.
    """
    import telebot

    mock_bot = MagicMock()
    monkeypatch.setattr(telebot, "TeleBot", lambda *a, **kw: mock_bot)

    from tg_bot import TelegramController

    monkeypatch.setattr(TelegramController, "BASE", tmp_path)
    ctrl = TelegramController(token="test", admin_id=0)
    return ctrl, mock_bot


def test_edit_or_send_success_no_send(tg_ctrl):
    """Успешный edit → send_message НЕ должен вызываться."""
    ctrl, mock_bot = tg_ctrl
    mock_bot.edit_message_text.return_value = MagicMock()  # success
    mock_bot.send_message.reset_mock()  # forget _setup() side-effects

    ctrl._edit_or_send(chat_id=123, message_id=456, text="hello", kb=None)

    mock_bot.edit_message_text.assert_called_once_with("hello", 123, 456, reply_markup=None)
    mock_bot.send_message.assert_not_called()


def test_edit_or_send_failure_falls_back_to_send(tg_ctrl):
    """Если edit бросил Exception — должен вызваться send_message с
    тем же chat_id, текстом и markup."""
    ctrl, mock_bot = tg_ctrl
    mock_bot.edit_message_text.side_effect = RuntimeError("message too old")
    mock_bot.send_message.reset_mock()

    fake_kb = MagicMock(name="kb")
    ctrl._edit_or_send(chat_id=42, message_id=99, text="hi", kb=fake_kb)

    mock_bot.edit_message_text.assert_called_once_with("hi", 42, 99, reply_markup=fake_kb)
    # send_message должен быть вызван через _send → его последний positional
    # arg — chat_id, текст. reply_markup передаётся через kwargs.
    mock_bot.send_message.assert_called_once()
    args, kwargs = mock_bot.send_message.call_args
    assert args[0] == 42  # chat_id
    assert args[1] == "hi"  # text
    assert kwargs.get("reply_markup") is fake_kb


def test_edit_or_send_with_no_keyboard(tg_ctrl):
    """kb=None работает: edit получает reply_markup=None, fallback тоже без kb."""
    ctrl, mock_bot = tg_ctrl
    mock_bot.edit_message_text.side_effect = RuntimeError("nope")
    mock_bot.send_message.reset_mock()

    ctrl._edit_or_send(chat_id=1, message_id=2, text="text only")

    mock_bot.send_message.assert_called_once()
    args, kwargs = mock_bot.send_message.call_args
    assert args[0] == 1
    assert args[1] == "text only"
    # _send добавляет reply_markup в kwargs только если markup не-None.
    assert "reply_markup" not in kwargs


def test_edit_or_send_swallows_any_exception_type(tg_ctrl):
    """Telegram-ошибки бывают разных классов (ApiException, ConnectionError, ...)
    — helper должен ловить базовый Exception, а не конкретный класс.
    """
    ctrl, mock_bot = tg_ctrl

    for exc_class in (RuntimeError, ValueError, ConnectionError, KeyError):
        mock_bot.edit_message_text.side_effect = exc_class("test")
        mock_bot.send_message.reset_mock()

        # Не должно бросить — проглотим и упадём на send_message
        ctrl._edit_or_send(chat_id=1, message_id=2, text="t")

        mock_bot.send_message.assert_called_once()
