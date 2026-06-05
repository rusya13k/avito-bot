"""
Telegram-бот для управления Avito-ботом.
Inline-кнопки, управление аккаунтами/прокси/настройками без редактирования файлов.
"""

import copy
import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

# E1: модульный logger
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Общее состояние (разделяется с bot.py)
# ══════════════════════════════════════════════════════════════════════════════

# Global event for shutting down the entire bot
stop_event = threading.Event()
# Per-account stop events: account_name -> threading.Event
account_stop_events: dict[str, threading.Event] = {}
_stop_events_lock = threading.Lock()

# Per-account log buffers: account_name -> deque
account_log_buffers: dict[str, deque] = {}
_log_buffers_lock = threading.Lock()
# Global log buffer for system messages
log_buffer: deque = deque(maxlen=200)

active_threads: list = []
_threads_lock = threading.Lock()
_tg_controller = None  # устанавливается в main() из bot.py

# C1-fix: rate limiting для TG-алертов (H9)
_last_alert_time: float = 0.0
_last_alert_lock = threading.Lock()
_ALERT_MIN_INTERVAL: float = 5.0  # секунд между алертами


def get_account_stop_event(account_name: str) -> threading.Event:
    with _stop_events_lock:
        if account_name not in account_stop_events:
            account_stop_events[account_name] = threading.Event()
        return account_stop_events[account_name]


def clear_account_stop_event(account_name: str) -> None:
    """Явно сбрасывает per-account stop event (используется при TG /start)."""
    with _stop_events_lock:
        ev = account_stop_events.get(account_name)
        if ev is not None:
            ev.clear()


def is_stop_requested(account_name: str | None = None) -> bool:
    if stop_event.is_set():
        return True
    if account_name and get_account_stop_event(account_name).is_set():
        return True
    return False


def add_log(line: str, account_name: str | None = None):
    log_buffer.append(line)
    if account_name:
        with _log_buffers_lock:
            if account_name not in account_log_buffers:
                account_log_buffers[account_name] = deque(maxlen=200)
            account_log_buffers[account_name].append(line)


def is_running() -> bool:
    with _threads_lock:
        return any(t.is_alive() for t in active_threads)


def _count_alive_threads() -> int:
    """Потокобезопасный подсчёт живых потоков."""
    with _threads_lock:
        return sum(1 for t in active_threads if t.is_alive())


def _is_account_thread_alive(account_name: str) -> bool:
    """Потокобезопасная проверка: работает ли поток данного аккаунта."""
    with _threads_lock:
        return any(t.is_alive() and t.name == f"acc-{account_name}" for t in active_threads)


def _send_message(text: str) -> None:
    """Отправляет сообщение админам через TG-контроллер (если настроен).

    H9-fix: rate limiting — не чаще 1 сообщения в _ALERT_MIN_INTERVAL секунд.
    """
    global _last_alert_time
    with _last_alert_lock:
        now = time.time()
        if now - _last_alert_time < _ALERT_MIN_INTERVAL:
            return
    ctrl = _tg_controller
    targets = (
        ctrl.admin_ids
        if ctrl and ctrl.admin_ids
        else ({ctrl.admin_id} if ctrl and ctrl.admin_id else set())
    )
    if targets:
        sent = False
        for aid in targets:
            try:
                ctrl.bot.send_message(aid, text)
                sent = True
            except Exception:
                pass
        if sent:
            with _last_alert_lock:
                _last_alert_time = now


def send_user_action_request(account_name: str, request_id: str, prompt: str) -> bool:
    """
    B1: уведомить админа в Telegram о том, что нужно ручное действие
    (например, ввести SMS-код или решить капчу). Создаёт сообщение с inline-
    кнопками "▶️ Продолжить" / "❌ Отмена".

    Returns True, если сообщение действительно отправлено (TG-контроллер
    инициализирован и admin_id задан), иначе False.

    Не блокирует поток. Поток должен потом вызвать
    `account_state.wait_user_resume(...)` для ожидания ответа админа.
    """
    ctrl = _tg_controller
    targets = (
        ctrl.admin_ids
        if ctrl and ctrl.admin_ids
        else ({ctrl.admin_id} if ctrl and ctrl.admin_id else set())
    )
    if not targets:
        add_log(
            f"[{account_name}] send_user_action_request: TG не настроен — "
            f"запрос {request_id!s} не доставлен"
        )
        return False
    try:
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("▶️ Продолжить", callback_data=f"b1_res_{request_id}_c"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"b1_res_{request_id}_x"),
        )
        text = (
            f"⚠️ Аккаунту «{account_name}» требуется ручное действие.\n\n"
            f"{prompt}\n\n"
            f"После того как разберёшься в браузере (AdsPower), нажми «Продолжить» "
            f"или «Отмена», чтобы прервать поток."
        )
        for aid in targets:
            try:
                ctrl.bot.send_message(aid, text, reply_markup=kb)
            except Exception:
                pass
        add_log(f"[{account_name}] user-resume запрос отправлен админу (id={request_id})")
        return True
    except Exception as exc:
        add_log(f"[{account_name}] send_user_action_request failed: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Inline-клавиатуры
# ══════════════════════════════════════════════════════════════════════════════


def kb_main() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=2)
    m.add(
        InlineKeyboardButton("▶️ Запустить", callback_data="run"),
        InlineKeyboardButton("⏹ Остановить", callback_data="stop"),
        InlineKeyboardButton("📊 Отчёт", callback_data="report"),
        InlineKeyboardButton("📋 Логи", callback_data="logs"),
        InlineKeyboardButton("👤 Аккаунты", callback_data="accounts_menu"),
        InlineKeyboardButton("➕ Добавить аккаунт", callback_data="acc_add"),
        InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu"),
    )
    return m


def kb_back(target: str = "menu_main") -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("◀️ Назад", callback_data=target))
    return m


def kb_accounts(accounts: list) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    for i, acc in enumerate(accounts):
        cookie_ok = Path(acc.get("cookies_path", "")).exists()
        icon = "✅" if cookie_ok else "❌"
        m.add(InlineKeyboardButton(f"{icon} {acc['name']}", callback_data=f"acc_detail_{i}"))
    m.row(
        InlineKeyboardButton("➕ Добавить аккаунт", callback_data="acc_add"),
        InlineKeyboardButton("◀️ Назад", callback_data="menu_main"),
    )
    return m


def kb_account_detail(idx: int) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=2)
    m.add(
        InlineKeyboardButton("📞 Телефон", callback_data=f"acc_phone_{idx}"),
        InlineKeyboardButton("🔑 Пароль", callback_data=f"acc_password_{idx}"),
        InlineKeyboardButton("🆔 AdsPower ID", callback_data=f"acc_userid_{idx}"),
        InlineKeyboardButton("👤 Персона", callback_data=f"acc_persona_{idx}"),
        InlineKeyboardButton("🧊 Капча кулдаун (мин)", callback_data=f"acc_captcha_cd_{idx}"),
        InlineKeyboardButton("🔥 Большой прогрев", callback_data=f"acc_bigwarmup_{idx}"),
        InlineKeyboardButton("💤 Отключить/Включить", callback_data=f"acc_toggle_{idx}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"acc_del_{idx}"),
        InlineKeyboardButton("◀️ Назад", callback_data="accounts_menu"),
    )
    return m


def kb_proxies(proxies: list) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    for i, p in enumerate(proxies):
        # Поддержка IPv6 [::1]:port:user:pass и обычных host:port
        try:
            if p.startswith("["):
                bracket_end = p.index("]")
                label = f"[{p[1:bracket_end]}]:{p[bracket_end + 2 :].split(':')[0]}"
            else:
                parts = p.split(":")
                label = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else p
        except (ValueError, IndexError):
            label = p
        m.add(InlineKeyboardButton(f"🔒 {i + 1}. {label}", callback_data=f"proxy_del_confirm_{i}"))
    m.row(
        InlineKeyboardButton("➕ Добавить прокси", callback_data="proxy_add"),
        InlineKeyboardButton("📋 Заменить все", callback_data="proxy_replace"),
    )
    m.add(InlineKeyboardButton("◀️ Назад", callback_data="menu_main"))
    return m


def kb_settings() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        InlineKeyboardButton("🔗 Ссылка на объявление", callback_data="set_url"),
        InlineKeyboardButton("🤖 DeepSeek API Key", callback_data="set_openai_key"),
        InlineKeyboardButton("🧠 LLM Model", callback_data="set_openai_model"),
        InlineKeyboardButton("🌐 AdsPower API URL", callback_data="set_adspower_url"),
        InlineKeyboardButton("🔑 AdsPower API Key", callback_data="set_adspower_key"),
        InlineKeyboardButton("🧵 Кол-во потоков", callback_data="set_threads"),
        InlineKeyboardButton("🔤 Ключевые слова", callback_data="set_keywords"),
        InlineKeyboardButton("◀️ Назад", callback_data="menu_main"),
    )
    return m


def kb_confirm(yes_cb: str, no_cb: str = "menu_main") -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=2)
    m.add(
        InlineKeyboardButton("✅ Да", callback_data=yes_cb),
        InlineKeyboardButton("❌ Нет", callback_data=no_cb),
    )
    return m


def _format_behavior_histogram(histogram: list[dict]) -> str:
    """T20: компактная ASCII-гистограмма по bins ('▁' .. '█').

    Каждый bin → один блочный символ, высота пропорциональна count
    относительно максимального bin. Пустой histogram / max_count==0 →
    пустая строка.
    """
    if not histogram:
        return ""
    counts = [b.get("count", 0) for b in histogram]
    max_c = max(counts) if counts else 0
    if max_c == 0:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(7, int(c / max_c * 7.99))] for c in counts)


def _format_seconds_compact(seconds: float) -> str:
    """T20: компактный формат для секунд: 45s / 12m / 3.5h."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


# ══════════════════════════════════════════════════════════════════════════════
# Контроллер
# ══════════════════════════════════════════════════════════════════════════════


class TelegramController:
    BASE = Path(__file__).parent

    def __init__(self, token: str, admin_id: int = 0):
        self.token = token
        self.admin_id = int(admin_id) if admin_id else 0
        # Поддержка нескольких админов: telegram_admin_ids из config.json
        # Если задан — _allowed проверяет по списку. Иначе — по self.admin_id.
        self.admin_ids: set[int] = set()
        self.bot = telebot.TeleBot(token, parse_mode=None)
        self._run_callback = None
        # Shared DatabaseManager — передаётся через set_db_manager() из bot.main().
        # До вызова set_db_manager() команды создадут свой экземпляр (fallback).
        self._db = None
        # Состояние диалога: {chat_id: {"state": str, "data": dict}}
        self._state: dict = {}
        # L5: cfg-кэш с mtime-инвалидацией. _cfg() раньше читал config.json
        # на каждом callback-вызове (24+ мест → диск каждый раз). Кэш
        # перечитывает только если файл был изменён извне.
        self._cfg_cache: dict | None = None
        self._cfg_cache_mtime: float = 0.0
        # T12: набор имён аккаунтов, для которых сейчас крутится фоновый
        # большой прогрев. Защита от двойного запуска.
        self._big_warmup_running: set[str] = set()
        self._big_warmup_lock = threading.Lock()
        self._last_msg_time: dict[int, float] = {}
        self._setup()

    # ── Утилиты ──────────────────────────────────────────────────────────────

    def set_run_callback(self, fn):
        self._run_callback = fn

    def set_admin_ids(self, ids: list[int] | list[float]) -> None:
        """Установить список разрешённых Telegram user ID из config.json."""
        self.admin_ids = {int(i) for i in ids if int(i) != 0}

    def set_db_manager(self, db_manager) -> None:
        """Передать shared DatabaseManager из bot.main() — один на весь процесс."""
        self._db = db_manager

    def _get_db(self):
        """Возвращает shared DatabaseManager или создаёт fallback."""
        if self._db is not None:
            return self._db
        from database import DatabaseManager

        self._db = DatabaseManager()
        return self._db

    def notify(self, text: str):
        targets = (
            self.admin_ids if self.admin_ids else ({self.admin_id} if self.admin_id else set())
        )
        for aid in targets:
            try:
                self.bot.send_message(aid, text)
            except Exception:
                pass

    def _allowed(self, uid: int) -> bool:
        import time as _time

        now = _time.time()
        if now - self._last_msg_time.get(uid, 0.0) < 0.5:
            return False
        self._last_msg_time[uid] = now
        # Если есть admin_ids — проверяем по списку. Иначе — по admin_id.
        if self.admin_ids:
            return uid in self.admin_ids
        return not self.admin_id or uid == self.admin_id

    def _cfg(self) -> dict:
        # L5: mtime-кэш. Проверяем os.stat — если mtime совпадает с тем,
        # что мы помним, отдаём deepcopy кэша (чтобы вызывающий код не
        # мутировал состояние). Если файл был изменён внешне (или мы ещё
        # не читали), перечитываем.
        path = self.BASE / "config.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._cfg_cache is not None and mtime == self._cfg_cache_mtime:
            return copy.deepcopy(self._cfg_cache)
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        self._cfg_cache = cfg
        self._cfg_cache_mtime = mtime
        # возвращаем копию, чтобы кэш не пострадал от мутаций вызывающего.
        return copy.deepcopy(cfg)

    def _save_cfg(self, cfg: dict):
        # L8: атомарная запись через tempfile + os.replace — как в
        # accounts.save_accounts (K1). Гарантирует, что bot.py / другой
        # читатель не увидит partial-write при крэше.
        # L5: после успешной записи обновляем кэш и mtime.
        path = self.BASE / "config.json"
        fd, tmp_path = tempfile.mkstemp(
            prefix=".config-",
            suffix=".tmp",
            dir=str(self.BASE),
        )
        try:
            os.chmod(tmp_path, 0o600)  # Только владелец может читать
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(cfg, tmp, ensure_ascii=False, indent=2)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            os.replace(tmp_path, path)
        except OSError:
            # Windows: файл может быть заблокирован другим процессом.
            # Пробуем direct write как fallback.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        try:
            self._cfg_cache_mtime = path.stat().st_mtime
            self._cfg_cache = copy.deepcopy(cfg)
        except OSError:
            self._cfg_cache = None
            self._cfg_cache_mtime = 0.0

    def _proxies(self) -> list:
        cfg = self._cfg()
        path = self.BASE / cfg.get("proxies_file", "proxies.txt")
        if not path.exists():
            return []
        return [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    def _save_proxies(self, proxies: list):
        cfg = self._cfg()
        path = self.BASE / cfg.get("proxies_file", "proxies.txt")
        # Atomic write: tempfile + os.replace
        fd, tmp_path = tempfile.mkstemp(prefix=".proxies-", suffix=".tmp", dir=str(self.BASE))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write("\n".join(proxies) + ("\n" if proxies else ""))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            os.replace(tmp_path, str(path))
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            path.write_text("\n".join(proxies) + ("\n" if proxies else ""), encoding="utf-8")

    def _keywords(self) -> list:
        cfg = self._cfg()
        path = self.BASE / cfg.get("keywords_file", "keywords.txt")
        if not path.exists():
            return []
        return [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    def _save_keywords(self, keywords: list):
        cfg = self._cfg()
        path = self.BASE / cfg.get("keywords_file", "keywords.txt")
        # Atomic write: tempfile + os.replace
        fd, tmp_path = tempfile.mkstemp(prefix=".keywords-", suffix=".tmp", dir=str(self.BASE))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write("\n".join(keywords) + "\n")
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            os.replace(tmp_path, str(path))
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            path.write_text("\n".join(keywords) + "\n", encoding="utf-8")

    def _set_dialog(self, chat_id: int, state: str, data: dict = None):
        self._state[chat_id] = {"state": state, "data": dict(data) if data else {}}

    def _clear_dialog(self, chat_id: int):
        # Зануляем sensitive data перед удалением (password может висеть в памяти)
        entry = self._state.pop(chat_id, None)
        if entry and isinstance(entry.get("data"), dict):
            entry["data"].pop("password", None)

    def _get_dialog(self, chat_id: int) -> dict:
        return self._state.get(chat_id, {})

    def _send(self, chat_id, text, markup=None, md=False):
        kwargs = {}
        if markup:
            kwargs["reply_markup"] = markup
        if md:
            kwargs["parse_mode"] = "Markdown"
        if len(text) > 4000:
            # Обрезаем, не ломая многобайтовый UTF-8 символ на границе.
            text = "...\n" + text[-3997:]
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                # Убираем битый trailing byte
                while len(text) > 4000:
                    text = text[:-1]
                text = text.encode("utf-8", errors="ignore").decode("utf-8")
        try:
            self.bot.send_message(chat_id, text, **kwargs)
        except Exception:
            logger.debug("_send failed for chat_id=%s", chat_id, exc_info=True)

    def _edit_or_send(self, chat_id, message_id, text, kb=None) -> None:
        """S2: пытается edit_message_text для inline-кнопок (обновляет
        существующее сообщение); при провале — _send нового сообщения.

        Telegram падает при попытке edit'а старого сообщения (старше 48ч),
        чужого сообщения, или при отсутствии permissions. В этих случаях
        просто отправляем новое — пользователь увидит ту же информацию.

        Используется в callback-handler'ах (после клика inline-кнопки)
        и в _show_*-методах (когда edit_msg передан).
        """
        try:
            self.bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except Exception:
            self._send(chat_id, text, kb)

    # ── Экраны-меню (вызываются из команд и callback) ─────────────────────────

    def _show_main(self, chat_id, edit_msg=None):
        running = is_running()
        text = (
            f"Avito-бот\n"
            f"Статус: {'🟢 работает' if running else '🔴 остановлен'}\n"
            f"Потоков: {_count_alive_threads()}"
        )
        if edit_msg:
            self._edit_or_send(edit_msg.chat.id, edit_msg.message_id, text, kb_main())
            return
        self._send(chat_id, text, kb_main())

    def _accounts(self) -> list:
        """
        K1: единая точка чтения аккаунтов в TG-боте.

        Источник правды — `accounts.json` (G2). Если его нет, читаем из
        `config.json["accounts"]` (legacy). При записи в любом случае
        пишем в `accounts.json` через accounts.add_account/remove/update.

        Возвращаем НЕ-фильтрованный список (вместе с disabled), чтобы
        пользователь видел все аккаунты в TG-меню — даже временно
        отключённые `enabled=false`.

        Результат кэшируется до изменения файла (mtime-check).
        """
        from accounts import load_all_accounts

        accs_path = self.BASE / "accounts.json"
        try:
            mtime = accs_path.stat().st_mtime
        except OSError:
            mtime = 0
        cached = getattr(self, "_accounts_cache", None)
        if cached is not None and getattr(self, "_accounts_mtime", 0) == mtime:
            return cached
        result = load_all_accounts(self.BASE, self._cfg())
        self._accounts_cache = result
        self._accounts_mtime = mtime
        return result

    def _show_accounts(self, chat_id, edit_msg=None):
        accs = self._accounts()
        text = f"Аккаунты ({len(accs)}):"
        if edit_msg:
            self._edit_or_send(edit_msg.chat.id, edit_msg.message_id, text, kb_accounts(accs))
            return
        self._send(chat_id, text, kb_accounts(accs))

    def _show_proxies(self, chat_id, edit_msg=None):
        proxies = self._proxies()
        text = f"Прокси ({len(proxies)}):\n(нажми на прокси чтобы удалить)"
        if edit_msg:
            self._edit_or_send(edit_msg.chat.id, edit_msg.message_id, text, kb_proxies(proxies))
            return
        self._send(chat_id, text, kb_proxies(proxies))

    def _show_settings(self, chat_id, edit_msg=None):
        cfg = self._cfg()
        text = (
            f"⚙️ Настройки\n\n"
            f"Потоков: {cfg.get('threads', 0) or 'без ограничений'}\n"
            f"URL: {cfg.get('target_url', '—')[:60]}...\n"
            f"Ключевых слов: {len(self._keywords())}\n"
            f"LLM Key: {'✅ задан' if cfg.get('openai_api_key', '') else '❌ не задан'}\n"
            f"LLM Model: {cfg.get('openai_model', 'deepseek-v4-flash')}\n"
            f"LLM API: {cfg.get('openai_api_base', 'https://api.deepseek.com/v1')}\n"
            f"AdsPower URL: {cfg.get('adspower_api_url', '—')}\n"
            f"AdsPower Key: {'✅ задан' if cfg.get('adspower_api_key', '') else '❌ не задан'}"
        )
        if edit_msg:
            self._edit_or_send(edit_msg.chat.id, edit_msg.message_id, text, kb_settings())
            return
        self._send(chat_id, text, kb_settings())

    # ══════════════════════════════════════════════════════════════════════════
    # S2 Stage 1: Message-handler'ы как методы класса. Регистрируются в _setup
    # через `self.bot.message_handler(commands=[...])(self._cmd_X)`.
    # Каждый метод сам проверяет _allowed и сам обрабатывает Exception, чтобы
    # ошибка одной команды не убивала polling.
    # ══════════════════════════════════════════════════════════════════════════

    def _cmd_start(self, message):
        """Команда /start или /menu — открывает главное меню."""
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        self._clear_dialog(message.chat.id)
        self._show_main(message.chat.id)

    def _cmd_report(self, message):
        """E3: краткая сводка за сутки или за всё время.
        /report      — за сегодня
        /report all  — за всё время
        """
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        try:
            arg = (message.text or "").split(maxsplit=1)
            arg = arg[1].strip().lower() if len(arg) > 1 else ""
            if arg == "all":
                since = "1970-01-01 00:00:00"
                title = "за всё время"
            else:
                since = time.strftime("%Y-%m-%d 00:00:00", time.localtime())
                title = f"за сегодня ({since[:10]})"

            db = self._get_db()
            s = db.get_daily_summary(since)
            lines = [
                f"📊 Сводка {title}",
                "",
                f"Листингов распарсено: {s.get('listings_parsed', 0)}",
                f"  ok: {s.get('listings_ok', 0)}  "
                f"captcha: {s.get('listings_captcha', 0)}  "
                f"error: {s.get('listings_error', 0)}",
                "",
                "Классификация:",
                f"  собственники: {s.get('classified_owner', 0)}",
                f"  агенты: {s.get('classified_agent', 0)}",
                f"  uncertain: {s.get('classified_uncertain', 0)}",
                "",
                f"Активных диалогов: {s.get('dialogs_active', 0)}",
                f"Сообщений всего: {s.get('messages_total', 0)}",
                "",
                # E2: счётчики per period (берутся из metrics-таблицы)
                f"Диалогов обработано: {s.get('dialogs_handled', 0)}",
                f"Сообщений отправлено: {s.get('messages_sent', 0)}",
                f"LLM ошибок: {s.get('llm_errors', 0)}",
                f"Капчей поймано: {s.get('captcha_hits', 0)}",
            ]
            self.bot.reply_to(message, "\n".join(lines))
        except Exception as exc:
            logger.exception("cmd_report failed")
            self.bot.reply_to(message, f"Ошибка отчёта: {exc}")

    def _cmd_budget(self, message):
        """C2: статус дневных бюджетов по всем аккаунтам с цветными индикаторами."""
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        try:
            from account_state import account_state as _astate
            from accounts import load_accounts

            cfg = self._cfg()
            accounts_list = load_accounts(self.BASE, cfg)
            db = self._get_db()
            today = time.strftime("%Y-%m-%d 00:00:00")
            lines = ["💰 Бюджет аккаунтов на сегодня", ""]
            if not accounts_list:
                lines.append("Нет активных аккаунтов.")
            # E2-opt: один запрос вместо N+1 — забираем все метрики за сегодня
            all_metrics_rows = db.get_metrics(since=today, group_by="metric")
            # Строим lookup: (account_name, metric) → value
            metrics_lookup: dict[tuple[str, str], int] = {}
            for r in all_metrics_rows:
                key = (r.get("account_name", ""), r.get("metric", ""))
                metrics_lookup[key] = int(r.get("value", 0))
            for acc in accounts_list:
                name = acc["name"]
                lines.append(f"▪ {name}:")
                for action, metric in [
                    ("listings", "listings_parsed"),
                    ("messages", "messages_sent"),
                    ("phone", "phone_clicks"),
                ]:
                    if action == "phone":
                        used = _astate.phone_clicks_today(name)
                    else:
                        used = metrics_lookup.get((name, metric), 0)
                    limit = _astate.get_effective_limit(name, action)
                    pct = used * 100 // limit if limit > 0 else 0
                    icon = "🔴" if pct >= 100 else "🟡" if pct >= 80 else "🟢"
                    lines.append(f"  {icon} {action}: {used}/{limit} ({pct}%)")
                warmup = "⏳ warmup" if _astate.is_in_warmup(name) else ""
                if warmup:
                    lines.append(f"  {warmup}")
                lines.append("")
            self.bot.reply_to(message, "\n".join(lines))
        except Exception as exc:
            logger.exception("cmd_budget failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")

    def _cmd_lastcaptcha(self, message):
        """C3: последние капча-инциденты по аккаунту.
        /lastcaptcha <name> [N=5]
        """
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        try:
            parts = (message.text or "").split()
            if len(parts) < 2:
                self.bot.reply_to(message, "Использование: /lastcaptcha <имя_аккаунта> [N=5]")
                return
            name = parts[1]
            limit = int(parts[2]) if len(parts) > 2 else 5

            db = self._get_db()
            rows = db.get_captcha_log(name, limit=limit)
            if not rows:
                self.bot.reply_to(message, f"Нет капча-инцидентов для '{name}'.")
                return
            lines = [f"🚨 Последние капчи — {name}:", ""]
            for r in rows:
                lines.append(
                    f"{r['ts']}  {r['action']}  {r['captcha_type']}\n  {r['page_url'] or '—'}"
                )
            self.bot.reply_to(message, "\n".join(lines))
        except Exception as exc:
            logger.exception("cmd_lastcaptcha failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")

    def _format_behavior_pattern(self, db, account_name: str) -> str:
        """T20: блок «📊 Pattern (7д)» для /health <name>.

        Для каждого event_type из (cycle_pause_sec, dwell_sec,
        long_break_sec) — count + median + p95 + stddev + ASCII-histogram.
        Возвращает пустую строку если для аккаунта вообще нет sample'ов.
        """
        types = [
            ("cycle_pause_sec", "паузы цикла"),
            ("dwell_sec", "dwell листингов"),
            ("long_break_sec", "длинные перерывы"),
        ]
        since_ts = time.time() - 7 * 86400
        out: list[str] = []
        any_samples = False
        for event_type, label in types:
            try:
                stats = db.get_behavioral_stats(
                    account_name=account_name,
                    event_type=event_type,
                    since_ts=since_ts,
                    bins=12,
                )
            except Exception as e:
                logger.warning("get_behavioral_stats(%s) failed: %s", event_type, e)
                continue
            if stats["count"] == 0:
                continue
            any_samples = True
            med = _format_seconds_compact(stats["median"])
            p95 = _format_seconds_compact(stats["p95"])
            sigma = _format_seconds_compact(stats["stddev"])
            out.append(f"  {label}: n={stats['count']}, med={med}, p95={p95}, σ={sigma}")
            hist = _format_behavior_histogram(stats["histogram"])
            if hist:
                out.append(f"  {hist}")
        if not any_samples:
            return ""
        return "📊 Pattern (7д):\n" + "\n".join(out)

    def _cmd_health(self, message):
        """C1: health score аккаунта (или всех аккаунтов) за 7 дней.
        /health [name] — если name не указан, выводит для всех.
        T20: при /health <name> добавляется блок Pattern с гистограммами
        cycle_pause_sec / dwell_sec / long_break_sec.
        """
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        try:
            from account_state import account_state as _astate
            from account_state import compute_account_health
            from accounts import load_accounts

            parts = (message.text or "").split()
            cfg = self._cfg()
            db = self._get_db()

            if len(parts) > 1:
                target_accounts = [{"name": parts[1]}]
            else:
                target_accounts = load_accounts(self.BASE, cfg) or []

            if not target_accounts:
                self.bot.reply_to(message, "Нет аккаунтов.")
                return

            lines = ["🏥 Health score аккаунтов (7 дней)", ""]
            mode_icon = {"healthy": "✅", "warning": "⚠️", "degraded": "🔴", "critical": "💀"}
            single_account = len(target_accounts) == 1
            for acc in target_accounts:
                name = acc["name"]
                h = compute_account_health(name, db)
                icon = mode_icon.get(h["mode"], "❓")
                warmup = " ⏳warmup" if _astate.is_in_warmup(name) else ""
                lines.append(
                    f"{icon} {name}{warmup}\n"
                    f"  режим: {h['mode']}  score: {h['score']:.3f}\n"
                    f"  листингов: {h['listings_7d']}  капч: {h['captchas_7d']}\n"
                    f"  (с {h['since']})"
                )
                # T20: behavioral pattern (только при /health <name>) — иначе
                # сообщение переполнится при N>3 аккаунтов.
                if single_account:
                    pattern_block = self._format_behavior_pattern(db, name)
                    if pattern_block:
                        lines.append(pattern_block)
                lines.append("")
            self.bot.reply_to(message, "\n".join(lines))
        except Exception as exc:
            logger.exception("cmd_health failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")

    def _cmd_warmup(self, message):
        """B1: продлить warmup-период аккаунта на N дней от текущего момента.
        /warmup <name>      — продлить на 3 дня
        /warmup <name> 7    — продлить на 7 дней
        /warmup <name> 0    — немедленно завершить warmup
        """
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        try:
            import time as _time

            from account_state import account_state as _astate

            parts = (message.text or "").split()
            if len(parts) < 2:
                self.bot.reply_to(
                    message,
                    "Использование: /warmup <имя_аккаунта> [дни=3]\n"
                    "  /warmup acc1     — продлить на 3 дня\n"
                    "  /warmup acc1 7   — продлить на 7 дней\n"
                    "  /warmup acc1 0   — завершить warmup немедленно",
                )
                return
            name = parts[1]
            days = int(parts[2]) if len(parts) > 2 else 3
            if days < 0:
                self.bot.reply_to(message, "Число дней должно быть >= 0.")
                return
            new_until = _time.time() + days * 86400
            _astate.set_warmup_until(name, new_until)
            if days == 0:
                self.bot.reply_to(message, f"✅ Warmup для '{name}' завершён — нормальный режим.")
            else:
                import datetime as _dt

                until_str = _dt.datetime.fromtimestamp(new_until).strftime("%Y-%m-%d %H:%M")
                self.bot.reply_to(
                    message,
                    f"⏳ Warmup для '{name}' продлён на {days} дн. до {until_str}.",
                )
        except ValueError:
            self.bot.reply_to(message, "Число дней должно быть целым числом.")
        except Exception as exc:
            logger.exception("cmd_warmup failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")

    def _cmd_skipday(self, message):
        """F7: пометить сегодняшний день для аккаунта как dead-day.
        /skipday <name> — следующая итерация увидит is_dead_day=True
        и проспит до завтрашнего active_hours_start.
        """
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        try:
            from account_state import account_state as _astate

            parts = (message.text or "").split()
            if len(parts) < 2:
                self.bot.reply_to(
                    message,
                    "Использование: /skipday <имя_аккаунта>\n"
                    "Помечает сегодняшний день как «выходной» — аккаунт "
                    "проспит до завтрашнего active_hours_start.",
                )
                return
            name = parts[1]
            _astate.force_dead_day(name)
            self.bot.reply_to(
                message,
                f"😴 '{name}': сегодня dead-day. Пропуск до завтра.",
            )
        except Exception as exc:
            logger.exception("cmd_skipday failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")

    def _cmd_cancel(self, message):
        """Отменить текущий диалог-state и вернуться в главное меню."""
        self._clear_dialog(message.chat.id)
        self.bot.reply_to(message, "Отменено.", reply_markup=ReplyKeyboardRemove())
        self._show_main(message.chat.id)

    # ══════════════════════════════════════════════════════════════════════════
    # S2 Stage 2: Dialog state-machine. handle_dialog раньше был ~280 строк
    # if/elif по 13 состояниям. Сейчас — dispatch-table _DIALOG_HANDLERS,
    # каждое состояние в своём _dialog_X методе. Состояния выставляются
    # в on_callback (`self._set_dialog(cid, "acc_add_name")`) и читаются
    # в _handle_dialog. Когда _set_dialog очищается (clear_dialog) —
    # сообщение перестаёт попадать в этот handler (его func= видит, что
    # chat.id больше нет в self._state).
    # ══════════════════════════════════════════════════════════════════════════

    def _dialog_acc_add_name(self, message, data):
        """State: ввод имени нового аккаунта."""
        cid = message.chat.id
        name = (message.text or "").strip()
        if not name:
            self.bot.reply_to(message, "Имя не может быть пустым.")
            return
        # K1: проверяем по реальному источнику — accounts.json.
        if any(a["name"] == name for a in self._accounts()):
            self.bot.reply_to(message, f"Аккаунт '{name}' уже существует.")
            return
        self._set_dialog(cid, "acc_add_phone", {"name": name})
        self.bot.reply_to(
            message,
            f"Аккаунт: {name}\n\nОтправь номер телефона (например +79673639403).\n/cancel — отмена.",
        )

    def _dialog_acc_add_phone(self, message, data):
        """State: ввод телефона для нового аккаунта."""
        cid = message.chat.id
        phone = (message.text or "").strip()
        if not phone:
            self.bot.reply_to(message, "Телефон не может быть пустым. Отправь номер или /cancel.")
            return
        data["phone"] = phone
        self._set_dialog(cid, "acc_add_password", data)
        self.bot.reply_to(
            message,
            f"Телефон: {phone}\n\nОтправь пароль от аккаунта Avito.\n/cancel — отмена.",
        )

    def _dialog_acc_add_password(self, message, data):
        """State: ввод пароля для нового аккаунта."""
        cid = message.chat.id
        password = (message.text or "").strip()
        if not password:
            self.bot.reply_to(message, "Пароль не может быть пустым. Отправь пароль или /cancel.")
            return
        data["password"] = password
        self._set_dialog(cid, "acc_add_adspower", data)
        self.bot.reply_to(
            message,
            "Пароль сохранён.\n\nОтправь AdsPower ID профиля "
            "(например k1c2utgb).\nИли отправь /skip если пока нет.\n/cancel — отмена.",
        )

    def _dialog_acc_add_adspower(self, message, data):
        """State: ввод AdsPower ID (опционально)."""
        cid = message.chat.id
        text = (message.text or "").strip()
        name = data["name"]
        phone = data["phone"]
        password = data["password"]

        # /skip = без AdsPower ID
        if text.lower() != "/skip":
            data["adspower_id"] = text
        else:
            data["adspower_id"] = ""

        # Сохраняем аккаунт в accounts.json
        try:
            from accounts import add_account

            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
            cookies_path = f"accounts/{safe_name}/cookies.json"
            account_data = {
                "name": name,
                "phone": phone,
                "password": password,
                "cookies_path": cookies_path,
                "enabled": True,
            }
            if data.get("adspower_id"):
                account_data["adspower_id"] = data["adspower_id"]
            add_account(self.BASE, account_data, cfg=self._cfg())
        except ValueError as exc:
            self.bot.reply_to(message, f"Не удалось добавить аккаунт: {exc}")
            self._clear_dialog(cid)
            return
        except Exception as exc:
            logger.exception("add_account failed")
            self.bot.reply_to(message, f"Ошибка записи accounts.json: {exc}")
            self._clear_dialog(cid)
            return

        self._clear_dialog(cid)
        adspower_info = (
            f"\nAdsPower ID: {data.get('adspower_id')}" if data.get("adspower_id") else ""
        )
        self.bot.reply_to(
            message,
            f"✅ Аккаунт добавлен!\n"
            f"Имя: {name}\n"
            f"Телефон: {phone}\n"
            f"Пароль: {'*' * len(password)}{adspower_info}",
        )
        self._show_accounts(cid)

    def _dialog_proxy_add(self, message, data):
        """State: добавление одной строки прокси."""
        cid = message.chat.id
        line = (message.text or "").strip()
        if not line:
            self.bot.reply_to(message, "Пустая строка.")
            return
        proxies = self._proxies()
        proxies.append(line)
        self._save_proxies(proxies)
        self._clear_dialog(cid)
        self.bot.reply_to(message, f"✅ Прокси добавлен: {line}")
        self._show_proxies(cid)

    def _dialog_proxy_replace(self, message, data):
        """State: полная замена списка прокси (multi-line)."""
        cid = message.chat.id
        lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
        if not lines:
            self.bot.reply_to(message, "Список пустой.")
            return
        self._save_proxies(lines)
        self._clear_dialog(cid)
        self.bot.reply_to(message, f"✅ Сохранено {len(lines)} прокси.")
        self._show_proxies(cid)

    def _dialog_set_keywords(self, message, data):
        """State: замена списка ключевых слов (multi-line)."""
        cid = message.chat.id
        lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
        if not lines:
            self.bot.reply_to(message, "Список пустой.")
            return
        self._save_keywords(lines)
        self._clear_dialog(cid)
        self.bot.reply_to(message, f"✅ Сохранено {len(lines)} ключевых слов.")
        self._show_settings(cid)

    def _save_cfg_text_field(
        self, message, cfg_key: str, success_text: str, *, require_url: bool = False
    ):
        """Helper для set_url/set_sphere_key/set_openai_*/set_adspower_*:
        читает message.text, валидирует (URL prefix если require_url),
        сохраняет в cfg[cfg_key], показывает settings-меню.
        """
        cid = message.chat.id
        text = (message.text or "").strip()
        if require_url:
            if not text.startswith("http"):
                self.bot.reply_to(message, "Не похоже на URL.")
                return
        else:
            if not text:
                self.bot.reply_to(message, "Значение не может быть пустым.")
                return
        cfg = self._cfg()
        cfg[cfg_key] = text
        self._save_cfg(cfg)
        self._clear_dialog(cid)
        self.bot.reply_to(message, success_text)
        self._show_settings(cid)

    def _dialog_set_url(self, message, data):
        self._save_cfg_text_field(message, "target_url", "✅ URL обновлён.", require_url=True)

    def _dialog_set_openai_key(self, message, data):
        self._save_cfg_text_field(message, "openai_api_key", "✅ DeepSeek API Key обновлён.")

    def _dialog_set_openai_model(self, message, data):
        # Особый success-text с показом модели — не подходит generic helper.
        cid = message.chat.id
        model = (message.text or "").strip()
        if not model:
            self.bot.reply_to(message, "Модель не может быть пустой.")
            return
        cfg = self._cfg()
        cfg["openai_model"] = model
        self._save_cfg(cfg)
        self._clear_dialog(cid)
        self.bot.reply_to(message, f"✅ LLM Model установлена: {model}")
        self._show_settings(cid)

    def _dialog_set_adspower_url(self, message, data):
        self._save_cfg_text_field(
            message, "adspower_api_url", "✅ AdsPower API URL обновлён.", require_url=True
        )

    def _dialog_set_adspower_key(self, message, data):
        self._save_cfg_text_field(message, "adspower_api_key", "✅ AdsPower API Key обновлён.")

    def _dialog_set_threads(self, message, data):
        """State: число потоков (0..50)."""
        cid = message.chat.id
        text = (message.text or "").strip()
        if not text.isdigit():
            self.bot.reply_to(message, "Введи число (0 = без ограничений).")
            return
        n = int(text)
        if n > 50:
            self.bot.reply_to(message, "Максимум 50.")
            return
        cfg = self._cfg()
        cfg["threads"] = n
        self._save_cfg(cfg)
        self._clear_dialog(cid)
        self.bot.reply_to(message, f"✅ Потоков: {n or 'без ограничений'}")
        self._show_settings(cid)

    def _dialog_acc_set_userid(self, message, data):
        """State: AdsPower User ID для существующего аккаунта."""
        cid = message.chat.id
        idx = data.get("idx")
        user_id = (message.text or "").strip()
        if not user_id:
            self.bot.reply_to(message, "ID не может быть пустым.")
            return
        accs = self._accounts()
        if idx is None or idx >= len(accs):
            self.bot.reply_to(message, "Аккаунт не найден.")
            self._clear_dialog(cid)
            return
        acc_name = accs[idx]["name"]
        # K1: пишем в accounts.json. Поле adspower_id — новое каноническое
        # имя (G2), user_id поддерживается как alias и проставляется
        # автоматически в _normalize_only.
        try:
            from accounts import update_account

            updated = update_account(
                self.BASE,
                acc_name,
                {"adspower_id": user_id, "user_id": user_id},
                cfg=self._cfg(),
            )
        except Exception as exc:
            logger.exception("update_account failed")
            self.bot.reply_to(message, f"Ошибка записи accounts.json: {exc}")
            self._clear_dialog(cid)
            return
        if updated is None:
            self.bot.reply_to(message, f"Аккаунт '{acc_name}' не найден.")
            self._clear_dialog(cid)
            return
        self._show_accounts(cid)

    def _dialog_acc_set_phone(self, message, data):
        """State: обновление телефона для существующего аккаунта."""
        cid = message.chat.id
        idx = data.get("idx")
        phone = (message.text or "").strip()
        if not phone:
            self.bot.reply_to(message, "Телефон не может быть пустым.")
            return
        accs = self._accounts()
        if idx is None or idx >= len(accs):
            self.bot.reply_to(message, "Аккаунт не найден.")
            self._clear_dialog(cid)
            return
        acc_name = accs[idx]["name"]
        try:
            from accounts import update_account

            update_account(self.BASE, acc_name, {"phone": phone}, cfg=self._cfg())
        except Exception as exc:
            logger.exception("update_account phone failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")
            self._clear_dialog(cid)
            return
        self._clear_dialog(cid)
        self.bot.reply_to(message, f"✅ Телефон для '{acc_name}' обновлён: {phone}")
        self._show_accounts(cid)

    def _dialog_acc_set_password(self, message, data):
        """State: обновление пароля для существующего аккаунта."""
        cid = message.chat.id
        idx = data.get("idx")
        password = (message.text or "").strip()
        if not password:
            self.bot.reply_to(message, "Пароль не может быть пустым.")
            return
        accs = self._accounts()
        if idx is None or idx >= len(accs):
            self.bot.reply_to(message, "Аккаунт не найден.")
            self._clear_dialog(cid)
            return
        acc_name = accs[idx]["name"]
        try:
            from accounts import update_account

            update_account(self.BASE, acc_name, {"password": password}, cfg=self._cfg())
        except Exception as exc:
            logger.exception("update_account password failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")
            self._clear_dialog(cid)
            return
        self._clear_dialog(cid)
        self.bot.reply_to(message, f"✅ Пароль для '{acc_name}' обновлён.")
        self._show_accounts(cid)

    def _dialog_acc_set_persona(self, message, data):
        """State: обновление персоны для аккаунта."""
        cid = message.chat.id
        idx = data.get("idx")
        persona = (message.text or "").strip()
        if not persona:
            persona = None  # Сброс на дефолт
        accs = self._accounts()
        if idx is None or idx >= len(accs):
            self.bot.reply_to(message, "Аккаунт не найден.")
            self._clear_dialog(cid)
            return
        acc_name = accs[idx]["name"]
        try:
            from accounts import update_account

            update_account(self.BASE, acc_name, {"persona": persona}, cfg=self._cfg())
        except Exception as exc:
            logger.exception("update_account persona failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")
            self._clear_dialog(cid)
            return
        self._clear_dialog(cid)
        display = persona or "по умолчанию"
        self.bot.reply_to(message, f"✅ Персона для '{acc_name}': {display}")
        self._show_accounts(cid)

    def _dialog_acc_set_captcha_cd(self, message, data):
        """State: обновление captcha_cooldown_minutes для аккаунта."""
        cid = message.chat.id
        idx = data.get("idx")
        text = (message.text or "").strip()
        if not text.isdigit():
            self.bot.reply_to(
                message, "Введи число минут (напр. 30). Или 0 для сброса на глобальный."
            )
            return
        minutes = int(text)
        accs = self._accounts()
        if idx is None or idx >= len(accs):
            self.bot.reply_to(message, "Аккаунт не найден.")
            self._clear_dialog(cid)
            return
        acc_name = accs[idx]["name"]
        try:
            from accounts import update_account

            # 0 = сброс (вернётся к глобальному из config.json)
            val = minutes if minutes > 0 else None
            update_account(self.BASE, acc_name, {"captcha_cooldown_minutes": val}, cfg=self._cfg())
        except Exception as exc:
            logger.exception("update_account captcha_cd failed")
            self.bot.reply_to(message, f"Ошибка: {exc}")
            self._clear_dialog(cid)
            return
        self._clear_dialog(cid)
        display = f"{minutes} мин" if minutes > 0 else "глобальный"
        self.bot.reply_to(message, f"✅ Капча кулдаун для '{acc_name}': {display}")
        self._show_accounts(cid)

    def _handle_dialog(self, message):
        """S2 Stage 2: dispatch-table вместо большого if/elif. Маппинг
        state → method, неизвестные state'ы (легаси) тихо игнорируются —
        пользователь увидит, что бот не реагирует, и может написать /cancel.
        """
        if not self._allowed(message.from_user.id):
            return
        # /cancel может попасть сюда если пользователь написал /cancel
        # во время диалога — передаём в _cmd_cancel
        if (message.text or "").strip().lower() == "/cancel":
            self._cmd_cancel(message)
            return
        dialog = self._get_dialog(message.chat.id)
        state = dialog.get("state")
        data = dialog.get("data", {})

        # acc_add_cookies и acc_update_cookies — один и тот же handler,
        # отличаются только через data["idx"]: None vs число.
        handlers = {
            "acc_add_name": self._dialog_acc_add_name,
            "acc_add_phone": self._dialog_acc_add_phone,
            "acc_add_password": self._dialog_acc_add_password,
            "acc_add_adspower": self._dialog_acc_add_adspower,
            "acc_set_phone": self._dialog_acc_set_phone,
            "acc_set_password": self._dialog_acc_set_password,
            "acc_set_persona": self._dialog_acc_set_persona,
            "acc_set_captcha_cd": self._dialog_acc_set_captcha_cd,
            "proxy_add": self._dialog_proxy_add,
            "proxy_replace": self._dialog_proxy_replace,
            "set_keywords": self._dialog_set_keywords,
            "set_url": self._dialog_set_url,
            "set_openai_key": self._dialog_set_openai_key,
            "set_openai_model": self._dialog_set_openai_model,
            "set_adspower_url": self._dialog_set_adspower_url,
            "set_adspower_key": self._dialog_set_adspower_key,
            "set_threads": self._dialog_set_threads,
            "acc_set_userid": self._dialog_acc_set_userid,
        }
        handler = handlers.get(state)
        if handler:
            handler(message, data)
        elif state:
            logger.warning("Неизвестный dialog state: %s (chat_id=%s)", state, message.chat.id)
            self.bot.reply_to(
                message,
                "❓ Неизвестное состояние диалога. Отправь /cancel чтобы начать заново.",
            )

    # ══════════════════════════════════════════════════════════════════════════
    # S2 Stage 3: Callback router. on_callback раньше был ~370 строк if/elif
    # по callback-data строкам. Сейчас — диспетчер _on_callback, который:
    #   1) ловит B1 user-resume (особый формат b1_res_<id>_<c|x>)
    #   2) проходит по prefix-таблице (acc_del_ok_, acc_del_, acc_detail_, ...)
    #   3) ищет точное совпадение в exact-таблице (menu_main, run, stop, ...)
    # Каждая ветка — отдельный _cb_<name>(call) метод.
    # ══════════════════════════════════════════════════════════════════════════

    # ── B1: специальный handler (формат b1_res_<id>_<c|x>) ─────────────────

    def _cb_b1_res(self, call):
        """B1: ответ admin'а на user-resume запрос (SMS-код / login-captcha).
        Формат callback_data: b1_res_<request_id>_<c|x>, где c=continue, x=cancel.
        """
        cid = call.message.chat.id
        d = call.data
        try:
            payload = d[len("b1_res_") :]
            # Парсим с конца: последний _c/_x — это response, всё до него — request_id.
            # Защита от request_id, содержащего _c/_x внутри.
            last_underscore = payload.rfind("_")
            if last_underscore < 0:
                self.bot.answer_callback_query(call.id, "Bad payload")
                return
            suffix = payload[last_underscore + 1 :]
            request_id = payload[:last_underscore]
            if suffix == "c":
                response = "continue"
            elif suffix == "x":
                response = "cancel"
            else:
                self.bot.answer_callback_query(call.id, "Bad payload")
                return
        except Exception:
            self.bot.answer_callback_query(call.id, "Bad payload")
            return

        from account_state import account_state as _astate

        req = _astate.find_request(request_id)
        if req is None:
            self._send(cid, f"Запрос {request_id} не найден или уже закрыт.")
            return

        ok = _astate.notify_user_resumed(req.account_name, request_id, response)
        if ok:
            add_log(f"[{req.account_name}] admin -> {response} (id={request_id})")
            self._edit_or_send(
                cid,
                call.message.message_id,
                f"✅ Ответ принят: {response}\nАккаунт «{req.account_name}», kind={req.kind}",
            )
        else:
            self._send(cid, f"Не удалось закрыть запрос {request_id}.")

    # ── Навигация ───────────────────────────────────────────────────────────

    def _cb_menu_main(self, call):
        self._clear_dialog(call.message.chat.id)
        self._show_main(call.message.chat.id, call.message)

    def _cb_accounts_menu(self, call):
        self._show_accounts(call.message.chat.id, call.message)

    def _cb_proxies_menu(self, call):
        self._show_proxies(call.message.chat.id, call.message)

    def _cb_settings_menu(self, call):
        self._show_settings(call.message.chat.id, call.message)

    # ── Запуск/стоп/логи ────────────────────────────────────────────

    def _cb_run(self, call):
        cid = call.message.chat.id

        # Если бот уже работает — сначала останавливаем, потом запускаем.
        if is_running():
            self.bot.answer_callback_query(call.id, "⏳ Останавливаю и перезапускаю...")
            stop_event.set()
            # Ждём завершения до 15s
            deadline = time.time() + 15
            with _threads_lock:
                old_threads = list(active_threads)
                # Не clear() — Supervisor thread (bot.py) ещё может делать remove().
                # Вместо этого убираем только те, что уже завершились.
                active_threads[:] = [t for t in active_threads if not t.is_alive()]
            for t in old_threads:
                remaining = max(0.5, deadline - time.time())
                try:
                    t.join(timeout=remaining)
                except Exception:
                    pass
            # Убиваем AdsPower-профили перед перезапуском
            self._cleanup_adspower()

        if not self._run_callback:
            self._send(cid, "Ошибка: run_callback не задан.")
            return
        stop_event.clear()
        with _stop_events_lock:
            for ev in account_stop_events.values():
                ev.clear()
        self._send(cid, "🚀 Запускаю потоки...")
        threading.Thread(target=self._run_callback, daemon=True, name="tg-runner").start()
        self._show_main(cid, call.message)

    def _cb_stop(self, call):
        cid = call.message.chat.id
        if not is_running():
            self.bot.answer_callback_query(call.id, "🔴 Бот не запущен.")
            self._show_main(cid, call.message)
            return
        add_log("🛑 Stop signal sent. Threads will exit at next checkpoint.")
        stop_event.set()
        self.bot.answer_callback_query(call.id, "🛑 Останавливаю...")
        self._send(
            cid,
            "Сигнал остановки отправлен. Жду завершения потоков (до 30s)...",
        )
        # C2: ждём фактического завершения потоков в отдельном потоке,
        # чтобы не блокировать handler TG (callback должен возвращаться быстро).
        threading.Thread(
            target=self._join_threads_and_report,
            args=(list(active_threads), cid),
            daemon=True,
            name="tg-stop-joiner",
        ).start()

    def _cleanup_adspower(self):
        """Остановить AdsPower-профили всех аккаунтов (параллельно)."""
        try:
            cfg = self._cfg()
            from accounts import load_all_accounts

            accs = load_all_accounts(self.BASE, cfg)
            adspower_url = cfg.get("adspower_api_url", "")
            adspower_key = cfg.get("adspower_api_key") or os.environ.get("ADSPOWER_API_KEY", "")
            if not adspower_url:
                return
            from bot import AdsPowerAPI

            api = AdsPowerAPI(adspower_url, adspower_key or None)
            uids = [uid for acc in accs if (uid := acc.get("adspower_id") or acc.get("user_id"))]
            if not uids:
                return
            # Параллельная остановка — быстрее чем последовательная
            threads = []
            for uid in uids:
                t = threading.Thread(target=api.stop_profile, args=(uid,), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=10)
        except Exception:
            pass

    def _join_threads_and_report(self, threads, chat_id):
        """C2: дожидаемся завершения потоков (до 30s) и отправляем итоговый
        отчёт пользователю. Запускается из _cb_stop в отдельном потоке.
        """
        deadline = time.time() + 30
        for t in threads:
            remaining = max(0.5, deadline - time.time())
            try:
                t.join(timeout=remaining)
            except Exception:
                pass
        alive = [t.name for t in threads if t.is_alive()]
        if alive:
            text = (
                "⚠️ Не все потоки завершились за 30s. Висят: "
                + ", ".join(alive)
                + ".\nВозможно, заблокированы на Selenium-операции "
                "(driver.get / WebDriverWait). Они выйдут при "
                "ближайшей точке проверки stop_event."
            )
        else:
            text = "✅ Все потоки завершились."
        add_log(text)
        try:
            self._send(chat_id, text, kb_back())
        except Exception:
            pass

    def _cb_report(self, call):
        """📊 Отчёт — кнопка-обёртка над _cmd_report."""
        cid = call.message.chat.id
        try:
            since = time.strftime("%Y-%m-%d 00:00:00", time.localtime())
            title = f"за сегодня ({since[:10]})"
            db = self._get_db()
            s = db.get_daily_summary(since)
            lines = [
                f"📊 Сводка {title}",
                "",
                f"Листингов распарсено: {s.get('listings_parsed', 0)}",
                f"  ok: {s.get('listings_ok', 0)}  "
                f"captcha: {s.get('listings_captcha', 0)}  "
                f"error: {s.get('listings_error', 0)}",
                "",
                "Классификация:",
                f"  собственники: {s.get('classified_owner', 0)}",
                f"  агенты: {s.get('classified_agent', 0)}",
                f"  uncertain: {s.get('classified_uncertain', 0)}",
                "",
                f"Активных диалогов: {s.get('dialogs_active', 0)}",
                f"Сообщений всего: {s.get('messages_total', 0)}",
                "",
                f"Диалогов обработано: {s.get('dialogs_handled', 0)}",
                f"Сообщений отправлено: {s.get('messages_sent', 0)}",
                f"LLM ошибок: {s.get('llm_errors', 0)}",
                f"Капчей поймано: {s.get('captcha_hits', 0)}",
            ]
            self._edit_or_send(cid, call.message.message_id, "\n".join(lines), kb_back())
        except Exception as exc:
            logger.exception("cb_report failed")
            self._send(cid, f"Ошибка отчёта: {exc}", kb_back())

    def _cb_logs(self, call):
        recent = list(log_buffer)[-30:]
        text = "\n".join(recent) if recent else "Лог пуст."
        self._edit_or_send(call.message.chat.id, call.message.message_id, text, kb_back())

    # ── Аккаунты (K1: все CRUD идёт через accounts.json) ──────────────────

    def _cb_acc_detail(self, call, force_idx=None):
        """Показать карточку аккаунта (callback_data: acc_detail_<idx>)."""
        cid = call.message.chat.id
        idx = force_idx if force_idx is not None else int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        acc = accs[idx]
        phone = acc.get("phone") or "❌ не задан"
        password = acc.get("password", "")
        pwd_display = "✅ задан" if password else "❌ не задан"
        adspower = acc.get("adspower_id") or acc.get("user_id") or "❌ не задан"
        persona = acc.get("persona") or "по умолчанию"
        captcha_cd = acc.get("captcha_cooldown_minutes") or "глобальный"
        enabled = "✅ включён" if acc.get("enabled", True) else "💤 disabled"
        text = (
            f"👤 {acc['name']}\n"
            f"📞 Телефон: {phone}\n"
            f"🔑 Пароль: {pwd_display}\n"
            f"🆔 AdsPower: {adspower}\n"
            f"👤 Персона: {persona}\n"
            f"🧊 Капча кулдаун: {captcha_cd} мин\n"
            f"💬 Статус: {enabled}"
        )
        self._edit_or_send(cid, call.message.message_id, text, kb_account_detail(idx))

    def _cb_acc_add(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "acc_add_name")
        self._send(cid, "Введи имя нового аккаунта:")

    def _cb_acc_userid(self, call):
        """Запрос ввода AdsPower User ID для аккаунта (acc_userid_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        self._set_dialog(cid, "acc_set_userid", {"idx": idx})
        self._send(cid, f"Введите AdsPower User ID для аккаунта '{accs[idx]['name']}':")

    def _cb_acc_phone(self, call):
        """Запрос ввода телефона для аккаунта (acc_phone_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        self._set_dialog(cid, "acc_set_phone", {"idx": idx})
        self._send(cid, f"Введите телефон для '{accs[idx]['name']}' (например +79673639403):")

    def _cb_acc_password(self, call):
        """Запрос ввода пароля для аккаунта (acc_password_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        self._set_dialog(cid, "acc_set_password", {"idx": idx})
        self._send(cid, f"Введите пароль для '{accs[idx]['name']}':")

    def _cb_acc_persona(self, call):
        """Запрос ввода персоны для аккаунта (acc_persona_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        current = accs[idx].get("persona") or "по умолчанию"
        self._set_dialog(cid, "acc_set_persona", {"idx": idx})
        self._send(
            cid,
            f"Текущая персона: {current}\n\n"
            f"Введите имя персоны для '{accs[idx]['name']}' "
            f"(стиль общения в чате). Или /skip для сброса:",
        )

    def _cb_acc_captcha_cd(self, call):
        """Запрос ввода captcha_cooldown_minutes (acc_captcha_cd_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        current = accs[idx].get("captcha_cooldown_minutes") or "глобальный"
        self._set_dialog(cid, "acc_set_captcha_cd", {"idx": idx})
        self._send(
            cid,
            f"Текущий кулдаун: {current}\n\n"
            f"Введите минуты (напр. 30). 0 = сброс на глобальный из config.json:",
        )

    def _cb_acc_toggle(self, call):
        """Переключить enabled/disabled для аккаунта (acc_toggle_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        acc = accs[idx]
        new_state = not acc.get("enabled", True)
        from accounts import update_account

        update_account(self.BASE, acc["name"], {"enabled": new_state}, cfg=self._cfg())
        status = "включён ✅" if new_state else "отключён 💤"
        self.bot.answer_callback_query(call.id, f"Аккаунт {status}")
        # Обновляем карточку — вызываем напрямую вместо мутации call.data
        self._cb_acc_detail(call, force_idx=idx)

    def _cb_acc_del_confirm(self, call):
        """Подтверждение удаления аккаунта (acc_del_<idx>, НЕ acc_del_ok_)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        self._edit_or_send(
            cid,
            call.message.message_id,
            f"Удалить аккаунт '{accs[idx]['name']}'?",
            kb_confirm(f"acc_del_ok_{idx}", "accounts_menu"),
        )

    def _cb_acc_del_ok(self, call):
        """Подтверждённое удаление аккаунта (acc_del_ok_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        acc_name = accs[idx]["name"]
        logger.info("Removing account index=%s name=%s", idx, acc_name)
        # K1: удаление через accounts.remove_account (пишет в accounts.json).
        # Раньше: cfg["accounts"].pop(idx) + save_cfg → bot.py всё равно
        # читал accounts.json и видел удалённый аккаунт как "живой".
        try:
            from accounts import remove_account

            removed = remove_account(self.BASE, acc_name, cfg=self._cfg())
        except Exception as exc:
            logger.exception("remove_account failed")
            self._send(cid, f"Ошибка удаления: {exc}", kb_back("accounts_menu"))
            return
        if not removed:
            self._send(cid, f"Аккаунт '{acc_name}' не найден.", kb_back("accounts_menu"))
            return
        self._show_accounts(cid, call.message)

    # ── T12: Большой прогрев аккаунта ────────────────────────────────────

    def _cb_acc_bigwarmup_confirm(self, call):
        """T12: подтверждение запуска большого прогрева (acc_bigwarmup_<idx>).

        Открывает kb_confirm. На "✅ Да" ─ дёргается _cb_acc_bigwarmup_ok.
        """
        cid = call.message.chat.id
        try:
            idx = int(call.data.split("_")[-1])
        except ValueError:
            self._send(cid, "Некорректный индекс аккаунта.")
            return
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        name = accs[idx]["name"]
        with self._big_warmup_lock:
            if name in self._big_warmup_running:
                self._send(
                    cid,
                    f"⚠️ Большой прогрев для '{name}' уже идёт.",
                    kb_account_detail(idx),
                )
                return
        # Если основной цикл бота крутится для этого аккаунта — нельзя:
        # AdsPower-профиль будет занят, start_profile упадёт.
        if _is_account_thread_alive(name):
            self._send(
                cid,
                (
                    f"⚠️ Аккаунт '{name}' сейчас работает в основном цикле бота.\n"
                    f"Останови бота через ⏹ и затем запусти прогрев."
                ),
                kb_account_detail(idx),
            )
            return
        self._edit_or_send(
            cid,
            call.message.message_id,
            (
                f"🔥 Запустить большой прогрев для '{name}'?\n\n"
                f"• ~10 нейтральных сайтов + Yandex queries\n"
                f"• Длительность: ~15-30 минут\n"
                f"• AdsPower-профиль будет занят на это время\n"
                f"• Не запускай основной цикл бота для этого аккаунта,\n"
                f"  пока идёт прогрев"
            ),
            kb_confirm(f"acc_bigwarmup_ok_{idx}", f"acc_detail_{idx}"),
        )

    def _cb_acc_bigwarmup_ok(self, call):
        """T12: подтверждённый запуск (acc_bigwarmup_ok_<idx>) — в фоне."""
        cid = call.message.chat.id
        try:
            idx = int(call.data.split("_")[-1])
        except ValueError:
            self._send(cid, "Некорректный индекс аккаунта.")
            return
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        account = accs[idx]
        name = account["name"]
        # Атомарная проверка+add: защищаем от двойного клика (TOCTOU)
        with self._big_warmup_lock:
            if name in self._big_warmup_running:
                self._send(cid, f"⚠️ Прогрев для '{name}' уже идёт.")
                return
            self._big_warmup_running.add(name)
        self._send(
            cid,
            (
                f"🔥 Большой прогрев для '{name}' запущен в фоне (~15-30 минут).\n"
                f"Уведомлю в этот чат по завершении."
            ),
            kb_account_detail(idx),
        )
        threading.Thread(
            target=self._run_big_warmup,
            args=(account,),
            daemon=True,
            name=f"tg-bigwarmup-{name}",
        ).start()

    def _run_big_warmup(self, account: dict):
        """T12: фоновая задача — крутит run_big_warmup_for_account и шлёт notify."""
        name = account["name"]
        try:
            # Ленивый импорт: bot.py тащит много (Selenium, AdsPower) и при
            # старте контроллера может быть ещё не готов.
            from bot import AdsPowerAPI, run_big_warmup_for_account

            cfg = self._cfg()
            adspower_url = cfg.get("adspower_api_url", "")
            if not adspower_url:
                raise RuntimeError("adspower_api_url не задан в конфиге")
            adspower = AdsPowerAPI(adspower_url, cfg.get("adspower_api_key"))
            result = run_big_warmup_for_account(account, cfg, adspower)
            if result.get("ok"):
                stats = result.get("stats") or {}
                visited = int(stats.get("sites_visited", 0))
                failed = int(stats.get("sites_failed", 0))
                total = visited + failed
                self.notify(
                    "✅ Большой прогрев '{name}' завершён.\n"
                    "  sites_visited: {visited}/{total}\n"
                    "  yandex_ok: {yan}\n"
                    "  duration: {dur:.0f}s".format(
                        name=name,
                        visited=visited,
                        total=total or visited,
                        yan=stats.get("yandex_ok"),
                        dur=float(stats.get("duration_seconds", 0.0)),
                    )
                )
            else:
                self.notify(
                    f"❌ Большой прогрев '{name}' не сработал: "
                    f"{result.get('error') or 'unknown error'}"
                )
        except Exception:
            logger.exception("big_warmup background task failed")
            self.notify(f"❌ Большой прогрев '{name}' упал — см. логи.")
        finally:
            self._big_warmup_running.discard(name)

    def _cmd_bigwarmup(self, message):
        """T12: /bigwarmup <name> — запустить большой прогрев в фоне."""
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            self.bot.reply_to(
                message,
                (
                    "Использование: /bigwarmup <имя_аккаунта>\n"
                    "Запускает ~10-сайтовый прогрев + Yandex (15-30 минут) в фоне."
                ),
            )
            return
        name = parts[1]
        accs = self._accounts()
        account = next((a for a in accs if a["name"] == name), None)
        if account is None:
            self.bot.reply_to(message, f"Аккаунт '{name}' не найден.")
            return
        if name in self._big_warmup_running:
            self.bot.reply_to(message, f"⚠️ Прогрев '{name}' уже идёт.")
            return
        if _is_account_thread_alive(name):
            self.bot.reply_to(
                message,
                f"⚠️ Аккаунт '{name}' сейчас работает в основном цикле бота. "
                f"Останови бота и запусти прогрев заново.",
            )
            return
        # Атомарная проверка+add под локом — убираем TOCTOU
        with self._big_warmup_lock:
            if name in self._big_warmup_running:
                self.bot.reply_to(message, f"⚠️ Прогрев '{name}' уже идёт.")
                return
            self._big_warmup_running.add(name)
        self.bot.reply_to(
            message,
            (
                f"🔥 Большой прогрев для '{name}' запущен в фоне (~15-30 минут).\n"
                f"Уведомлю по завершении."
            ),
        )
        threading.Thread(
            target=self._run_big_warmup,
            args=(account,),
            daemon=True,
            name=f"tg-bigwarmup-{name}",
        ).start()

    # ── Прокси ──────────────────────────────────────────────────────────────

    def _cb_proxy_add(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "proxy_add")
        self._send(cid, "Введи прокси в формате:\nip:port:user:pass\n\nИли: /cancel для отмены")

    def _cb_proxy_replace(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "proxy_replace")
        self._send(
            cid,
            "Отправь список прокси (по одному на строку):\n"
            "ip:port:user:pass\n\n"
            "Они ЗАМЕНЯТ текущий список.\n/cancel — отмена",
        )

    def _cb_proxy_del_confirm(self, call):
        """Подтверждение удаления прокси (proxy_del_confirm_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        proxies = self._proxies()
        if idx >= len(proxies):
            self._send(cid, "Прокси не найден.")
            return
        self._edit_or_send(
            cid,
            call.message.message_id,
            f"Удалить прокси?\n{proxies[idx]}",
            kb_confirm(f"proxy_del_ok_{idx}", "proxies_menu"),
        )

    def _cb_proxy_del_ok(self, call):
        """Подтверждённое удаление прокси (proxy_del_ok_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        proxies = self._proxies()
        if idx >= len(proxies):
            self._send(cid, "Прокси не найден.")
            return
        proxies.pop(idx)
        self._save_proxies(proxies)
        self._show_proxies(cid, call.message)

    # ── Настройки (открывают dialog-state, далее _handle_dialog) ──────────

    def _cb_set_url(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_url")
        cfg = self._cfg()
        self._send(
            cid,
            f"Текущий URL:\n{cfg.get('target_url', '—')}\n\nОтправь новую ссылку на объявление:",
        )

    def _cb_set_openai_key(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_openai_key")
        self._send(cid, "Отправь DeepSeek API Key:")

    def _cb_set_openai_model(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_openai_model")
        cfg = self._cfg()
        cur = cfg.get("openai_model", "deepseek-v4-flash")
        self._send(
            cid,
            f"Текущая модель: {cur}\n\nОтправь название новой модели (напр. deepseek-v4-flash, deepseek-chat):",
        )

    def _cb_set_adspower_url(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_adspower_url")
        cfg = self._cfg()
        cur = cfg.get("adspower_api_url", "—")
        self._send(cid, f"Текущий URL: {cur}\n\nОтправь новый AdsPower API URL:")

    def _cb_set_adspower_key(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_adspower_key")
        self._send(cid, "Отправь AdsPower API Key:")

    def _cb_set_threads(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_threads")
        cfg = self._cfg()
        cur = cfg.get("threads", 0) or "без ограничений"
        self._send(
            cid,
            f"Текущее кол-во потоков: {cur}\n\n"
            f"Введи новое значение (0 = без ограничений, макс. 50):",
        )

    def _cb_set_keywords(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_keywords")
        kws = self._keywords()
        current = "\n".join(f"{i + 1}. {k}" for i, k in enumerate(kws)) if kws else "(пусто)"
        self._send(
            cid,
            f"Текущие ключевые слова:\n{current}\n\nОтправь новый список (по одному на строку):",
        )

    # ── Главный диспетчер callback-кнопок ─────────────────────────────────

    def _on_callback(self, call):
        """S2 Stage 3: главный callback-router. Был ~370 строк if/elif —
        стал диспетчером ~30 строк, который ищет handler в трёх таблицах:
        b1 special / prefix-таблица (длинные prefix'ы первыми) / exact-map.
        """
        if not self._allowed(call.from_user.id):
            self.bot.answer_callback_query(call.id, "Нет доступа.")
            return

        # Отвечаем Telegram СРАЗУ — иначе кнопка "зависает" со спиннером.
        self.bot.answer_callback_query(call.id)

        d = call.data

        # 1. B1 special handler (формат b1_res_<id>_<c|x>)
        if d.startswith("b1_res_"):
            self._cb_b1_res(call)
            return

        # 2. Prefix-handlers. ВАЖНО: длинный prefix должен идти ПЕРЕД коротким
        # (acc_del_ok_ перед acc_del_, иначе acc_del_ok_5 заматчится как acc_del_).
        prefix_handlers = (
            ("acc_del_ok_", self._cb_acc_del_ok),
            ("acc_del_", self._cb_acc_del_confirm),
            ("acc_detail_", self._cb_acc_detail),
            # T12: bigwarmup_ok_ должен идти ПЕРЕД bigwarmup_, иначе
            # acc_bigwarmup_ok_5 заматчится как acc_bigwarmup_ с idx="ok".
            ("acc_bigwarmup_ok_", self._cb_acc_bigwarmup_ok),
            ("acc_bigwarmup_", self._cb_acc_bigwarmup_confirm),
            ("acc_userid_", self._cb_acc_userid),
            ("acc_phone_", self._cb_acc_phone),
            ("acc_password_", self._cb_acc_password),
            ("acc_persona_", self._cb_acc_persona),
            ("acc_captcha_cd_", self._cb_acc_captcha_cd),
            ("acc_toggle_", self._cb_acc_toggle),
            ("proxy_del_ok_", self._cb_proxy_del_ok),
            ("proxy_del_confirm_", self._cb_proxy_del_confirm),
        )
        for prefix, handler in prefix_handlers:
            if d.startswith(prefix):
                handler(call)
                return

        # 3. Exact-match handlers
        exact_handlers = {
            # Навигация
            "menu_main": self._cb_menu_main,
            "accounts_menu": self._cb_accounts_menu,
            "proxies_menu": self._cb_proxies_menu,
            "settings_menu": self._cb_settings_menu,
            # Управление
            "run": self._cb_run,
            "stop": self._cb_stop,
            "report": self._cb_report,
            "logs": self._cb_logs,
            # Аккаунты
            "acc_add": self._cb_acc_add,
            # Прокси
            "proxy_add": self._cb_proxy_add,
            "proxy_replace": self._cb_proxy_replace,
            # Настройки
            "set_url": self._cb_set_url,
            "set_openai_key": self._cb_set_openai_key,
            "set_openai_model": self._cb_set_openai_model,
            "set_adspower_url": self._cb_set_adspower_url,
            "set_adspower_key": self._cb_set_adspower_key,
            "set_threads": self._cb_set_threads,
            "set_keywords": self._cb_set_keywords,
        }
        handler = exact_handlers.get(d)
        if handler:
            handler(call)

    # ══════════════════════════════════════════════════════════════════════════
    # Регистрация хендлеров
    # ══════════════════════════════════════════════════════════════════════════

    def _setup(self):
        bot = self.bot

        # ── Простые message-команды (S2 Stage 1: вынесены в _cmd_* методы) ───
        # Регистрируем через бы functional API: handler-decorator вызывается
        # как обычная функция и принимает callable.
        bot.message_handler(commands=["start", "menu"])(self._cmd_start)
        bot.message_handler(commands=["report"])(self._cmd_report)
        bot.message_handler(commands=["budget"])(self._cmd_budget)
        bot.message_handler(commands=["lastcaptcha"])(self._cmd_lastcaptcha)
        bot.message_handler(commands=["health"])(self._cmd_health)
        bot.message_handler(commands=["warmup"])(self._cmd_warmup)
        bot.message_handler(commands=["bigwarmup"])(self._cmd_bigwarmup)
        bot.message_handler(commands=["skipday"])(self._cmd_skipday)

        # ── Текстовый ввод / документы (S2 Stage 2: dispatch by state) ──────
        # _handle_dialog читает self._state[chat_id] и роутит по state →
        # _dialog_<state> методу. content_types включают document для
        # _dialog_acc_cookies (можно прислать cookies.json как файл).
        bot.message_handler(
            content_types=["text", "document"], func=lambda m: m.chat.id in self._state
        )(self._handle_dialog)

        # ── /cancel (S2 Stage 1: вынесено в _cmd_cancel) ───────────────────
        bot.message_handler(commands=["cancel"])(self._cmd_cancel)

        # ── Callback-кнопки (S2 Stage 3: dispatch table в _on_callback) ───
        bot.callback_query_handler(func=lambda c: True)(self._on_callback)

    # ── Polling ───────────────────────────────────────────────────────────────

    def start_polling(self):
        self.bot.infinity_polling(timeout=20, long_polling_timeout=10, logger_level=None)
