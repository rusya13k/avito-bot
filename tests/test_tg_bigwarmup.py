"""
T12: тесты для TG-кнопки и команды /bigwarmup.

Проверяем:
- Callback-роутинг: acc_bigwarmup_ok_<idx> выигрывает у acc_bigwarmup_<idx>.
- Кнопка `🔥 Большой прогрев` присутствует в kb_account_detail.
- Confirm-step показывает kb_confirm с правильными callback_data.
- OK-step добавляет account в _big_warmup_running и стартует thread.
- Двойной запуск отбивается (warning без второй thread).
- Запуск конфликтует с активной thread бота (acc-<name>) → отказ.
- Команда /bigwarmup <name> работает аналогично кнопке.
- Фоновая задача (_run_big_warmup) корректно дёргает run_big_warmup_for_account
  и шлёт notify; в любом случае удаляет имя из _big_warmup_running.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Фикстуры ────────────────────────────────────────────────────────────────


@pytest.fixture
def tg_ctrl(tmp_path, monkeypatch):
    """TelegramController с замоканным TeleBot."""
    import telebot

    mock_bot = MagicMock()
    monkeypatch.setattr(telebot, "TeleBot", lambda *a, **kw: mock_bot)

    from tg_bot import TelegramController

    monkeypatch.setattr(TelegramController, "BASE", tmp_path)
    ctrl = TelegramController(token="test", admin_id=42)
    ctrl._allowed = lambda uid: not ctrl.admin_id or uid == ctrl.admin_id
    return ctrl


@pytest.fixture
def fake_accounts(tg_ctrl, monkeypatch):
    """Подменяем _accounts на фиксированный список из 2 аккаунтов."""
    accs = [
        {"name": "acc_a", "user_id": "u1", "cookies_path": "accounts/acc_a/cookies.json"},
        {"name": "acc_b", "user_id": "u2", "cookies_path": "accounts/acc_b/cookies.json"},
    ]
    monkeypatch.setattr(tg_ctrl, "_accounts", lambda: list(accs))
    return accs


def _make_call(chat_id=123, user_id=42, callback_data="", message_id=999):
    c = MagicMock()
    c.id = "call-id"
    c.data = callback_data
    c.from_user.id = user_id
    c.message.chat.id = chat_id
    c.message.message_id = message_id
    return c


def _make_message(chat_id=123, user_id=42, text=""):
    m = MagicMock()
    m.chat.id = chat_id
    m.from_user.id = user_id
    m.text = text
    m.content_type = "text"
    return m


# ── kb_account_detail ───────────────────────────────────────────────────────


def test_kb_account_detail_has_bigwarmup_button():
    """Кнопка `🔥 Большой прогрев` есть в детальном меню аккаунта."""
    from tg_bot import kb_account_detail

    kb = kb_account_detail(3)
    # Собираем все callback_data из всех рядов.
    cbs = [b.callback_data for row in kb.keyboard for b in row]
    assert "acc_bigwarmup_3" in cbs
    # Проверяем что text-метка действительно содержит "Большой прогрев".
    labels = [b.text for row in kb.keyboard for b in row]
    assert any("Большой прогрев" in lbl for lbl in labels), labels


# ── Callback router: prefix-resolution ──────────────────────────────────────


def test_acc_bigwarmup_ok_wins_over_bigwarmup(tg_ctrl, monkeypatch):
    """T12: acc_bigwarmup_ok_5 → _cb_acc_bigwarmup_ok, НЕ _cb_acc_bigwarmup_confirm."""
    confirm_calls = []
    ok_calls = []
    monkeypatch.setattr(
        tg_ctrl, "_cb_acc_bigwarmup_confirm", lambda c: confirm_calls.append(c.data)
    )
    monkeypatch.setattr(tg_ctrl, "_cb_acc_bigwarmup_ok", lambda c: ok_calls.append(c.data))

    tg_ctrl._on_callback(_make_call(callback_data="acc_bigwarmup_ok_5"))
    tg_ctrl._on_callback(_make_call(callback_data="acc_bigwarmup_7"))

    assert ok_calls == ["acc_bigwarmup_ok_5"]
    assert confirm_calls == ["acc_bigwarmup_7"]


def test_acc_bigwarmup_does_not_match_acc_detail(tg_ctrl, monkeypatch):
    """acc_bigwarmup_5 не должен заматчиться в acc_detail_ или acc_del_."""
    detail_calls = []
    del_calls = []
    bw_calls = []
    monkeypatch.setattr(tg_ctrl, "_cb_acc_detail", lambda c: detail_calls.append(c.data))
    monkeypatch.setattr(tg_ctrl, "_cb_acc_del_confirm", lambda c: del_calls.append(c.data))
    monkeypatch.setattr(tg_ctrl, "_cb_acc_bigwarmup_confirm", lambda c: bw_calls.append(c.data))

    tg_ctrl._on_callback(_make_call(callback_data="acc_bigwarmup_5"))

    assert detail_calls == []
    assert del_calls == []
    assert bw_calls == ["acc_bigwarmup_5"]


# ── _cb_acc_bigwarmup_confirm ────────────────────────────────────────────────


def test_confirm_shows_kb_confirm_with_proper_callback(tg_ctrl, fake_accounts, monkeypatch):
    """Confirm-step вызывает _edit_or_send с kb_confirm(acc_bigwarmup_ok_<idx>, ...)."""
    edits = []
    monkeypatch.setattr(
        tg_ctrl,
        "_edit_or_send",
        lambda cid, mid, text, kb=None: edits.append((text, kb)),
    )

    tg_ctrl._cb_acc_bigwarmup_confirm(_make_call(callback_data="acc_bigwarmup_1"))

    assert len(edits) == 1
    text, kb = edits[0]
    assert "acc_b" in text  # имя аккаунта (idx=1) попадает в текст
    cbs = [b.callback_data for row in kb.keyboard for b in row]
    assert "acc_bigwarmup_ok_1" in cbs


def test_confirm_blocks_if_already_running(tg_ctrl, fake_accounts, monkeypatch):
    """Если acc уже в _big_warmup_running — confirm не показывается."""
    edits = []
    sends = []
    monkeypatch.setattr(tg_ctrl, "_edit_or_send", lambda *a, **kw: edits.append(a))
    monkeypatch.setattr(tg_ctrl, "_send", lambda *a, **kw: sends.append(a))

    tg_ctrl._big_warmup_running.add("acc_a")

    tg_ctrl._cb_acc_bigwarmup_confirm(_make_call(callback_data="acc_bigwarmup_0"))

    # _edit_or_send (для confirmation) НЕ вызван.
    assert edits == []
    # _send (для warning) вызван.
    assert sends, sends
    text = sends[0][1]
    assert "уже идёт" in text or "уже" in text


def test_confirm_blocks_if_active_thread_for_account(tg_ctrl, fake_accounts, monkeypatch):
    """Если acc-<name> в active_threads (бот крутит этот аккаунт) — отказ."""
    edits = []
    sends = []
    monkeypatch.setattr(tg_ctrl, "_edit_or_send", lambda *a, **kw: edits.append(a))
    monkeypatch.setattr(tg_ctrl, "_send", lambda *a, **kw: sends.append(a))

    # Подменяем active_threads на список с одним thread с именем 'acc-acc_a'
    fake_thread = MagicMock()
    fake_thread.is_alive.return_value = True
    fake_thread.name = "acc-acc_a"

    import tg_bot

    monkeypatch.setattr(tg_bot, "active_threads", [fake_thread])

    tg_ctrl._cb_acc_bigwarmup_confirm(_make_call(callback_data="acc_bigwarmup_0"))

    assert edits == []
    assert sends, sends
    text = sends[0][1]
    assert "основном цикле" in text or "Останови" in text


def test_confirm_unknown_index(tg_ctrl, fake_accounts, monkeypatch):
    """idx за пределами списка → 'Аккаунт не найден.'"""
    sent = []
    monkeypatch.setattr(tg_ctrl, "_send", lambda *a, **kw: sent.append(a))

    tg_ctrl._cb_acc_bigwarmup_confirm(_make_call(callback_data="acc_bigwarmup_42"))

    assert sent
    assert "не найден" in sent[0][1]


# ── _cb_acc_bigwarmup_ok ────────────────────────────────────────────────────


def test_ok_starts_thread_and_marks_running(tg_ctrl, fake_accounts, monkeypatch):
    """OK-step: добавляет name в _big_warmup_running и стартует daemon-thread."""
    threads_started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=False, name=""):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name

        def start(self):
            threads_started.append(self)

    import tg_bot

    monkeypatch.setattr(tg_bot.threading, "Thread", FakeThread)
    monkeypatch.setattr(tg_ctrl, "_send", lambda *a, **kw: None)

    tg_ctrl._cb_acc_bigwarmup_ok(_make_call(callback_data="acc_bigwarmup_ok_0"))

    assert "acc_a" in tg_ctrl._big_warmup_running
    assert len(threads_started) == 1
    t = threads_started[0]
    assert t.daemon is True
    assert t.name == "tg-bigwarmup-acc_a"
    # Args: (account_dict,)
    assert t.args[0]["name"] == "acc_a"
    # target — instance method bound to ctrl
    assert t.target.__func__ is tg_ctrl._run_big_warmup.__func__


def test_ok_blocks_double_run(tg_ctrl, fake_accounts, monkeypatch):
    """Если acc_a уже в _big_warmup_running, повторный OK не стартует thread."""
    threads_started = []

    class FakeThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            threads_started.append(1)

    import tg_bot

    monkeypatch.setattr(tg_bot.threading, "Thread", FakeThread)
    monkeypatch.setattr(tg_ctrl, "_send", lambda *a, **kw: None)

    tg_ctrl._big_warmup_running.add("acc_a")
    tg_ctrl._cb_acc_bigwarmup_ok(_make_call(callback_data="acc_bigwarmup_ok_0"))

    assert threads_started == []


# ── _run_big_warmup (background task) ────────────────────────────────────────


def test_run_big_warmup_calls_runner_and_sends_notify(tg_ctrl, monkeypatch):
    """Happy path: result.ok=True → notify со статистикой, _big_warmup_running очищается."""
    notify_calls = []
    monkeypatch.setattr(tg_ctrl, "notify", lambda text: notify_calls.append(text))

    fake_runner = MagicMock(
        return_value={
            "ok": True,
            "stats": {
                "sites_visited": 8,
                "sites_failed": 2,
                "yandex_ok": True,
                "duration_seconds": 1234.0,
            },
            "error": None,
        }
    )
    fake_adspower_class = MagicMock()
    fake_cfg = {
        "adspower_api_url": "http://localhost:50325",
        "adspower_api_key": "test",
    }

    # Подменяем _cfg (lazy-import возвращает копию).
    monkeypatch.setattr(tg_ctrl, "_cfg", lambda: dict(fake_cfg))
    # Симулируем `from bot import AdsPowerAPI, run_big_warmup_for_account`.
    fake_bot_module = MagicMock()
    fake_bot_module.AdsPowerAPI = fake_adspower_class
    fake_bot_module.run_big_warmup_for_account = fake_runner
    monkeypatch.setitem(sys.modules, "bot", fake_bot_module)

    tg_ctrl._big_warmup_running.add("acc_x")
    tg_ctrl._run_big_warmup({"name": "acc_x", "user_id": "u1"})

    # AdsPowerAPI(constructor)
    fake_adspower_class.assert_called_once_with("http://localhost:50325", "test")
    # run_big_warmup_for_account(account, cfg, adspower)
    assert fake_runner.call_count == 1
    args, _ = fake_runner.call_args
    assert args[0] == {"name": "acc_x", "user_id": "u1"}
    assert args[1] == fake_cfg
    assert args[2] is fake_adspower_class.return_value

    # notify со статистикой
    assert len(notify_calls) == 1
    text = notify_calls[0]
    assert "✅" in text
    assert "acc_x" in text
    assert "8" in text  # sites_visited
    assert "1234" in text  # duration

    # _big_warmup_running очищен.
    assert "acc_x" not in tg_ctrl._big_warmup_running


def test_run_big_warmup_failure_path(tg_ctrl, monkeypatch):
    """result.ok=False → notify с ❌ и _big_warmup_running очищается."""
    notify_calls = []
    monkeypatch.setattr(tg_ctrl, "notify", lambda text: notify_calls.append(text))

    fake_runner = MagicMock(return_value={"ok": False, "stats": None, "error": "no proxy"})
    fake_bot_module = MagicMock()
    fake_bot_module.AdsPowerAPI = MagicMock()
    fake_bot_module.run_big_warmup_for_account = fake_runner
    monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
    monkeypatch.setattr(tg_ctrl, "_cfg", lambda: {"adspower_api_url": "x", "adspower_api_key": ""})

    tg_ctrl._big_warmup_running.add("acc_x")
    tg_ctrl._run_big_warmup({"name": "acc_x"})

    assert len(notify_calls) == 1
    assert "❌" in notify_calls[0]
    assert "no proxy" in notify_calls[0]
    assert "acc_x" not in tg_ctrl._big_warmup_running


def test_run_big_warmup_exception_clears_state(tg_ctrl, monkeypatch):
    """Если внутри упало необработанное исключение — всё равно clean-up."""
    notify_calls = []
    monkeypatch.setattr(tg_ctrl, "notify", lambda text: notify_calls.append(text))

    fake_bot_module = MagicMock()
    fake_bot_module.AdsPowerAPI = MagicMock(side_effect=RuntimeError("boom"))
    fake_bot_module.run_big_warmup_for_account = MagicMock()
    monkeypatch.setitem(sys.modules, "bot", fake_bot_module)
    monkeypatch.setattr(tg_ctrl, "_cfg", lambda: {"adspower_api_url": "x", "adspower_api_key": ""})

    tg_ctrl._big_warmup_running.add("acc_x")
    tg_ctrl._run_big_warmup({"name": "acc_x"})

    assert "acc_x" not in tg_ctrl._big_warmup_running
    # Должны были послать "упал — см. логи."
    assert len(notify_calls) == 1
    assert "упал" in notify_calls[0]


# ── /bigwarmup command ──────────────────────────────────────────────────────


def test_cmd_bigwarmup_no_args(tg_ctrl):
    """/bigwarmup без args → сообщение с usage."""
    msg = _make_message(text="/bigwarmup")
    tg_ctrl._cmd_bigwarmup(msg)
    tg_ctrl.bot.reply_to.assert_called()
    text = tg_ctrl.bot.reply_to.call_args[0][1]
    assert "Использование" in text


def test_cmd_bigwarmup_unknown_account(tg_ctrl, fake_accounts):
    """/bigwarmup nonexistent → 'не найден'."""
    msg = _make_message(text="/bigwarmup nonexistent")
    tg_ctrl._cmd_bigwarmup(msg)
    tg_ctrl.bot.reply_to.assert_called()
    text = tg_ctrl.bot.reply_to.call_args[0][1]
    assert "не найден" in text


def test_cmd_bigwarmup_starts_thread(tg_ctrl, fake_accounts, monkeypatch):
    """/bigwarmup acc_a → стартует thread, добавляет в _big_warmup_running."""
    threads_started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=False, name=""):
            self.target = target
            self.args = args
            self.name = name

        def start(self):
            threads_started.append(self)

    import tg_bot

    monkeypatch.setattr(tg_bot.threading, "Thread", FakeThread)

    msg = _make_message(text="/bigwarmup acc_a")
    tg_ctrl._cmd_bigwarmup(msg)

    assert "acc_a" in tg_ctrl._big_warmup_running
    assert len(threads_started) == 1
    assert threads_started[0].name == "tg-bigwarmup-acc_a"


def test_cmd_bigwarmup_blocks_when_active_thread(tg_ctrl, fake_accounts, monkeypatch):
    """/bigwarmup acc_a, но acc-acc_a в active_threads → отказ, thread не стартует."""
    threads_started = []

    class FakeThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            threads_started.append(1)

    import tg_bot

    monkeypatch.setattr(tg_bot.threading, "Thread", FakeThread)

    fake_thread = MagicMock()
    fake_thread.is_alive.return_value = True
    fake_thread.name = "acc-acc_a"
    monkeypatch.setattr(tg_bot, "active_threads", [fake_thread])

    msg = _make_message(text="/bigwarmup acc_a")
    tg_ctrl._cmd_bigwarmup(msg)

    assert threads_started == []
    text = tg_ctrl.bot.reply_to.call_args[0][1]
    assert "основном цикле" in text


def test_cmd_bigwarmup_no_access(tg_ctrl, fake_accounts):
    """Не-admin → 'Нет доступа.'"""
    msg = _make_message(user_id=999, text="/bigwarmup acc_a")
    tg_ctrl._cmd_bigwarmup(msg)
    text = tg_ctrl.bot.reply_to.call_args[0][1]
    assert "Нет доступа" in text


# ── /bigwarmup команда зарегистрирована ─────────────────────────────────────


def test_bigwarmup_command_registered(tg_ctrl):
    """В _setup() регистрируется bot.message_handler(commands=['bigwarmup'])."""
    # tg_ctrl.bot — MagicMock, ловим все вызовы message_handler.
    calls_with_bigwarmup = [
        c
        for c in tg_ctrl.bot.message_handler.call_args_list
        if c.kwargs.get("commands") == ["bigwarmup"]
        or (c.args and isinstance(c.args, tuple) and "bigwarmup" in str(c))
    ]
    # Должен быть как минимум один такой вызов.
    assert calls_with_bigwarmup, "/bigwarmup не зарегистрирован в _setup()"
