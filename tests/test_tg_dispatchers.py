"""
S2 Stage 2-3: тесты на диспетчеры _handle_dialog (state-machine) и
_on_callback (callback-router).

handle_dialog/on_callback раньше были ~280 + ~370 строк if/elif. Сейчас —
небольшие диспетчеры, которые ищут handler в dict-таблице. Тестируем что:
- Известный state/callback роутится в нужный метод.
- Неизвестный state/callback не падает (silently ignored).
- _allowed=False блокирует обработку.
- callback prefix-резолюция отдаёт приоритет более длинному prefix
  (acc_del_ok_ выигрывает у acc_del_).
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
    """TelegramController с замоканным TeleBot и tmp_path как BASE."""
    import telebot

    mock_bot = MagicMock()
    monkeypatch.setattr(telebot, "TeleBot", lambda *a, **kw: mock_bot)

    from tg_bot import TelegramController

    monkeypatch.setattr(TelegramController, "BASE", tmp_path)
    ctrl = TelegramController(token="test", admin_id=42)
    return ctrl


def _make_message(chat_id=123, user_id=42, text=""):
    """telebot.Message-like mock для passing в handle_dialog."""
    m = MagicMock()
    m.chat.id = chat_id
    m.from_user.id = user_id
    m.text = text
    m.content_type = "text"
    return m


def _make_call(chat_id=123, user_id=42, callback_data="", message_id=999):
    """telebot.CallbackQuery-like mock."""
    c = MagicMock()
    c.id = "call-id"
    c.data = callback_data
    c.from_user.id = user_id
    c.message.chat.id = chat_id
    c.message.message_id = message_id
    return c


# ── _handle_dialog ───────────────────────────────────────────────────────────


def test_handle_dialog_unknown_state_silently_ignored(tg_ctrl):
    """Неизвестный state — диспетчер просто не вызывает handler. Никаких
    Exception'ов, никаких send_message — пользователь увидит, что бот
    «молчит», и может написать /cancel."""
    tg_ctrl._state[123] = {"state": "completely_unknown_state", "data": {}}

    # Не должно бросить
    tg_ctrl._handle_dialog(_make_message())


def test_handle_dialog_dispatches_by_state(tg_ctrl, monkeypatch):
    """Известный state → вызывается соответствующий _dialog_X метод."""
    called = {"name": None, "data": None}

    def fake_handler(message, data):
        called["name"] = "proxy_add"
        called["data"] = data

    monkeypatch.setattr(tg_ctrl, "_dialog_proxy_add", fake_handler)

    tg_ctrl._state[123] = {"state": "proxy_add", "data": {"x": 1}}
    tg_ctrl._handle_dialog(_make_message(text="my-proxy"))

    assert called["name"] == "proxy_add"
    assert called["data"] == {"x": 1}


def test_handle_dialog_blocks_when_not_allowed(tg_ctrl, monkeypatch):
    """_allowed=False → handler НЕ вызывается."""
    called = []
    monkeypatch.setattr(tg_ctrl, "_dialog_proxy_add", lambda m, d: called.append("called"))

    tg_ctrl._state[123] = {"state": "proxy_add", "data": {}}
    msg = _make_message(user_id=999)  # not admin_id=42
    tg_ctrl._handle_dialog(msg)

    assert called == []


def test_handle_dialog_acc_add_cookies_and_update_share_handler(tg_ctrl, monkeypatch):
    """acc_add_cookies и acc_update_cookies маппятся на ОДИН метод
    _dialog_acc_cookies (отличаются только data["idx"])."""
    calls = []

    def fake_handler(message, data):
        calls.append(data.get("idx"))

    monkeypatch.setattr(tg_ctrl, "_dialog_acc_cookies", fake_handler)

    # acc_add_cookies → idx=None
    tg_ctrl._state[100] = {"state": "acc_add_cookies", "data": {"name": "newone"}}
    tg_ctrl._handle_dialog(_make_message(chat_id=100))
    # acc_update_cookies → idx=число
    tg_ctrl._state[200] = {"state": "acc_update_cookies", "data": {"idx": 5}}
    tg_ctrl._handle_dialog(_make_message(chat_id=200))

    assert calls == [None, 5]


# ── _on_callback ─────────────────────────────────────────────────────────────


def test_on_callback_unknown_data_does_nothing(tg_ctrl):
    """Неизвестный callback_data — диспетчер ничего не делает (silent)."""
    tg_ctrl._on_callback(_make_call(callback_data="totally_unknown_data"))
    # Должен ответить TG (answer_callback_query), но дальше handler не вызван.
    assert tg_ctrl.bot.answer_callback_query.called


def test_on_callback_blocks_when_not_allowed(tg_ctrl, monkeypatch):
    """_allowed=False → answer_callback_query("Нет доступа.") и return."""
    called_handler = []
    monkeypatch.setattr(tg_ctrl, "_cb_menu_main", lambda c: called_handler.append("yes"))

    tg_ctrl._on_callback(_make_call(user_id=999, callback_data="menu_main"))
    assert called_handler == []
    # Was answered with "Нет доступа.":
    tg_ctrl.bot.answer_callback_query.assert_called_with("call-id", "Нет доступа.")


def test_on_callback_dispatches_exact_match(tg_ctrl, monkeypatch):
    """Exact match callback_data → вызывается соответствующий _cb_X."""
    called = []
    monkeypatch.setattr(tg_ctrl, "_cb_menu_main", lambda c: called.append("menu_main"))
    monkeypatch.setattr(tg_ctrl, "_cb_run", lambda c: called.append("run"))

    tg_ctrl._on_callback(_make_call(callback_data="menu_main"))
    tg_ctrl._on_callback(_make_call(callback_data="run"))

    assert called == ["menu_main", "run"]


def test_on_callback_dispatches_prefix(tg_ctrl, monkeypatch):
    """callback_data acc_detail_5 → _cb_acc_detail."""
    received_calls = []
    monkeypatch.setattr(tg_ctrl, "_cb_acc_detail", lambda c: received_calls.append(c.data))

    tg_ctrl._on_callback(_make_call(callback_data="acc_detail_5"))
    assert received_calls == ["acc_detail_5"]


def test_on_callback_acc_del_ok_wins_over_acc_del(tg_ctrl, monkeypatch):
    """ВАЖНО: acc_del_ok_5 должен попасть в _cb_acc_del_ok, не в
    _cb_acc_del_confirm. Длинный prefix должен идти ПЕРЕД коротким
    в prefix-таблице."""
    confirm_calls = []
    ok_calls = []
    monkeypatch.setattr(tg_ctrl, "_cb_acc_del_confirm", lambda c: confirm_calls.append(c.data))
    monkeypatch.setattr(tg_ctrl, "_cb_acc_del_ok", lambda c: ok_calls.append(c.data))

    tg_ctrl._on_callback(_make_call(callback_data="acc_del_ok_5"))
    tg_ctrl._on_callback(_make_call(callback_data="acc_del_5"))

    # acc_del_ok_5 → ok handler (НЕ confirm)
    assert ok_calls == ["acc_del_ok_5"]
    # acc_del_5 → confirm handler (НЕ ok)
    assert confirm_calls == ["acc_del_5"]


def test_on_callback_b1_res_routed_to_special_handler(tg_ctrl, monkeypatch):
    """b1_res_<id>_<c|x> — особый формат, отдельный handler."""
    received = []
    monkeypatch.setattr(tg_ctrl, "_cb_b1_res", lambda c: received.append(c.data))

    tg_ctrl._on_callback(_make_call(callback_data="b1_res_abc123_c"))
    assert received == ["b1_res_abc123_c"]


def test_on_callback_proxy_del_ok_wins_over_proxy_del_confirm(tg_ctrl, monkeypatch):
    """proxy_del_ok_5 → _cb_proxy_del_ok (НЕ proxy_del_confirm).
    Хотя proxy_del_confirm длиннее, чем proxy_del_, разница в том что у
    нас нет prefix 'proxy_del_' — есть 'proxy_del_ok_' и 'proxy_del_confirm_'.
    Тест проверяет что они корректно разделены."""
    confirm_calls = []
    ok_calls = []
    monkeypatch.setattr(
        tg_ctrl, "_cb_proxy_del_confirm", lambda c: confirm_calls.append(c.data)
    )
    monkeypatch.setattr(tg_ctrl, "_cb_proxy_del_ok", lambda c: ok_calls.append(c.data))

    tg_ctrl._on_callback(_make_call(callback_data="proxy_del_ok_5"))
    tg_ctrl._on_callback(_make_call(callback_data="proxy_del_confirm_5"))

    assert ok_calls == ["proxy_del_ok_5"]
    assert confirm_calls == ["proxy_del_confirm_5"]
