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

log_buffer: deque = deque(maxlen=200)
active_threads: list = []
stop_event = threading.Event()
_tg_controller = None  # устанавливается в main() из bot.py


def add_log(line: str):
    log_buffer.append(line)


def is_running() -> bool:
    return any(t.is_alive() for t in active_threads)


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
    if ctrl is None or not getattr(ctrl, "admin_id", 0):
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
        ctrl.bot.send_message(ctrl.admin_id, text, reply_markup=kb)
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
        InlineKeyboardButton("📊 Статус", callback_data="status"),
        InlineKeyboardButton("📋 Логи", callback_data="logs"),
        InlineKeyboardButton("👤 Аккаунты", callback_data="accounts_menu"),
        InlineKeyboardButton("🔒 Прокси", callback_data="proxies_menu"),
        InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu"),
        InlineKeyboardButton("🔍 Классификация", callback_data="classification_menu"),
    )
    return m


def kb_back(target: str = "main") -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("◀️ Назад", callback_data=f"menu_{target}"))
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
        InlineKeyboardButton("🍪 Обновить куки", callback_data=f"acc_cookies_{idx}"),
        InlineKeyboardButton("🆔 Изменить AdsPower ID", callback_data=f"acc_userid_{idx}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"acc_del_{idx}"),
        InlineKeyboardButton("◀️ Назад", callback_data="accounts_menu"),
    )
    return m


def kb_proxies(proxies: list) -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    for i, p in enumerate(proxies):
        parts = p.split(":")
        label = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else p
        m.add(InlineKeyboardButton(f"🔒 {i + 1}. {label}", callback_data=f"proxy_del_confirm_{i}"))
    m.row(
        InlineKeyboardButton("➕ Добавить прокси", callback_data="proxy_add"),
        InlineKeyboardButton("📋 Заменить все", callback_data="proxy_replace"),
    )
    m.add(InlineKeyboardButton("◀️ Назад", callback_data="menu_main"))
    return m


def kb_classification() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        InlineKeyboardButton("🔄 Переклассифицировать базу", callback_data="reclassify_all"),
        InlineKeyboardButton("📊 Статистика классификации", callback_data="classification_stats"),
        InlineKeyboardButton("📋 Разметка 50 объявлений", callback_data="create_ground_truth"),
        InlineKeyboardButton("📈 Оценка качества", callback_data="evaluate_quality"),
        InlineKeyboardButton("◀️ Назад", callback_data="menu_main"),
    )
    return m


def kb_settings() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        InlineKeyboardButton("🔗 Ссылка на объявление", callback_data="set_url"),
        InlineKeyboardButton("🤖 OpenAI API Key", callback_data="set_openai_key"),
        InlineKeyboardButton("🧠 OpenAI Model", callback_data="set_openai_model"),
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


# ══════════════════════════════════════════════════════════════════════════════
# Контроллер
# ══════════════════════════════════════════════════════════════════════════════


class TelegramController:
    BASE = Path(__file__).parent

    def __init__(self, token: str, admin_id: int = 0):
        self.token = token
        self.admin_id = int(admin_id) if admin_id else 0
        self.bot = telebot.TeleBot(token, parse_mode=None)
        self._run_callback = None
        # Состояние диалога: {chat_id: {"state": str, "data": dict}}
        self._state: dict = {}
        # L5: cfg-кэш с mtime-инвалидацией. _cfg() раньше читал config.json
        # на каждом callback-вызове (24+ мест → диск каждый раз). Кэш
        # перечитывает только если файл был изменён извне.
        self._cfg_cache: dict | None = None
        self._cfg_cache_mtime: float = 0.0
        self._setup()

    # ── Утилиты ──────────────────────────────────────────────────────────────

    def set_run_callback(self, fn):
        self._run_callback = fn

    def notify(self, text: str):
        if self.admin_id:
            try:
                self.bot.send_message(self.admin_id, text)
            except Exception:
                pass

    def _allowed(self, uid: int) -> bool:
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
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.BASE),
            prefix=".config-",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(cfg, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, path)
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
        path.write_text("\n".join(keywords) + "\n", encoding="utf-8")

    def _set_dialog(self, chat_id: int, state: str, data: dict = None):
        self._state[chat_id] = {"state": state, "data": data or {}}

    def _clear_dialog(self, chat_id: int):
        self._state.pop(chat_id, None)

    def _get_dialog(self, chat_id: int) -> dict:
        return self._state.get(chat_id, {})

    def _send(self, chat_id, text, markup=None, md=False):
        kwargs = {}
        if markup:
            kwargs["reply_markup"] = markup
        if md:
            kwargs["parse_mode"] = "Markdown"
        if len(text) > 4000:
            text = "...\n" + text[-3997:]
        self.bot.send_message(chat_id, text, **kwargs)

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
            f"Потоков: {sum(1 for t in active_threads if t.is_alive())}"
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
        """
        from accounts import load_all_accounts

        return load_all_accounts(self.BASE, self._cfg())

    def _show_accounts(self, chat_id, edit_msg=None):
        accs = self._accounts()
        text = f"Аккаунты ({len(accs)}):"
        if edit_msg:
            self._edit_or_send(
                edit_msg.chat.id, edit_msg.message_id, text, kb_accounts(accs)
            )
            return
        self._send(chat_id, text, kb_accounts(accs))

    def _show_proxies(self, chat_id, edit_msg=None):
        proxies = self._proxies()
        text = f"Прокси ({len(proxies)}):\n(нажми на прокси чтобы удалить)"
        if edit_msg:
            self._edit_or_send(
                edit_msg.chat.id, edit_msg.message_id, text, kb_proxies(proxies)
            )
            return
        self._send(chat_id, text, kb_proxies(proxies))

    def _show_settings(self, chat_id, edit_msg=None):
        cfg = self._cfg()
        text = (
            f"⚙️ Настройки\n\n"
            f"Потоков: {cfg.get('threads', 0) or 'без ограничений'}\n"
            f"URL: {cfg.get('target_url', '—')[:60]}...\n"
            f"Ключевых слов: {len(self._keywords())}\n"
            f"OpenAI Key: {'✅ задан' if cfg.get('openai_api_key', '') else '❌ не задан'}\n"
            f"OpenAI Model: {cfg.get('openai_model', 'gpt-3.5-turbo')}\n"
            f"AdsPower URL: {cfg.get('adspower_api_url', '—')}\n"
            f"AdsPower Key: {'✅ задан' if cfg.get('adspower_api_key', '') else '❌ не задан'}"
        )
        if edit_msg:
            self._edit_or_send(
                edit_msg.chat.id, edit_msg.message_id, text, kb_settings()
            )
            return
        self._send(chat_id, text, kb_settings())

    def _show_classification(self, chat_id, edit_msg=None):
        text = "🔍 Классификация объявлений"
        if edit_msg:
            self._edit_or_send(
                edit_msg.chat.id, edit_msg.message_id, text, kb_classification()
            )
            return
        self._send(chat_id, text, kb_classification())

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
            from database import DatabaseManager

            db = DatabaseManager()
            s = db.get_daily_summary(since)
            lines = [
                f"📊 Сводка {title}",
                "",
                f"Листингов распарсено: {s['listings_parsed']}",
                f"  ok: {s['listings_ok']}  "
                f"captcha: {s['listings_captcha']}  "
                f"error: {s['listings_error']}",
                "",
                "Классификация:",
                f"  собственники: {s['classified_owner']}",
                f"  агенты: {s['classified_agent']}",
                f"  uncertain: {s['classified_uncertain']}",
                "",
                f"Активных диалогов: {s['dialogs_active']}",
                f"Сообщений всего: {s['messages_total']}",
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
            from database import DatabaseManager

            cfg = self._cfg()
            accounts_list = load_accounts(self.BASE, cfg)
            db = DatabaseManager()
            today = time.strftime("%Y-%m-%d 00:00:00")
            lines = ["💰 Бюджет аккаунтов на сегодня", ""]
            if not accounts_list:
                lines.append("Нет активных аккаунтов.")
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
                        rows = db.get_metrics(
                            since=today,
                            account_name=name,
                            metric=metric,
                            group_by="metric",
                        )
                        used = int(rows[0]["value"]) if rows else 0
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
            from database import DatabaseManager

            db = DatabaseManager()
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

    def _cmd_health(self, message):
        """C1: health score аккаунта (или всех аккаунтов) за 7 дней.
        /health [name] — если name не указан, выводит для всех.
        """
        if not self._allowed(message.from_user.id):
            self.bot.reply_to(message, "Нет доступа.")
            return
        try:
            from account_state import account_state as _astate
            from account_state import compute_account_health
            from accounts import load_accounts
            from database import DatabaseManager

            parts = (message.text or "").split()
            cfg = self._cfg()
            db = DatabaseManager()

            if len(parts) > 1:
                target_accounts = [{"name": parts[1]}]
            else:
                target_accounts = load_accounts(self.BASE, cfg) or []

            if not target_accounts:
                self.bot.reply_to(message, "Нет аккаунтов.")
                return

            lines = ["🏥 Health score аккаунтов (7 дней)", ""]
            mode_icon = {"healthy": "✅", "warning": "⚠️", "degraded": "🔴", "critical": "💀"}
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
        self._set_dialog(cid, "acc_add_cookies", {"name": name})
        self.bot.reply_to(
            message,
            f"Аккаунт: {name}\n\nОтправь файл cookies.json "
            f"или вставь JSON-текст кук.\n\nОтправь /cancel для отмены.",
        )

    def _dialog_acc_cookies(self, message, data):
        """State: ожидание кук (файл или текст). Используется и для нового
        аккаунта (acc_add_cookies, idx=None), и для обновления существующего
        (acc_update_cookies, idx=число).
        """
        cid = message.chat.id
        idx = data.get("idx")  # None = новый, число = обновление
        name = data.get("name")

        # 1. Получаем cookies_json из файла или текста
        if message.content_type == "document":
            try:
                file_info = self.bot.get_file(message.document.file_id)
                downloaded = self.bot.download_file(file_info.file_path)
                cookies_json = json.loads(downloaded.decode("utf-8"))
            except Exception as e:
                self.bot.reply_to(message, f"Ошибка чтения файла: {e}")
                return
        else:
            # L10: `or ""` защищает от non-text сообщений (фото без подписи
            # → message.text=None → AttributeError при .strip()).
            text = (message.text or "").strip()
            try:
                cookies_json = json.loads(text)
            except json.JSONDecodeError:
                self.bot.reply_to(
                    message,
                    "Не удалось распознать JSON. "
                    "Отправь файл .json или валидный JSON-текст.",
                )
                return

        if not isinstance(cookies_json, list):
            self.bot.reply_to(message, "Куки должны быть массивом [].")
            return

        # 2. Определяем cookies_path и acc_name
        accs = self._accounts()
        if idx is not None:
            if idx >= len(accs):
                self.bot.reply_to(message, "Аккаунт не найден.")
                self._clear_dialog(cid)
                return
            cookies_path = self.BASE / accs[idx]["cookies_path"]
            acc_name = accs[idx]["name"]
        else:
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
            cookies_path = self.BASE / "accounts" / safe_name / "cookies.json"
            acc_name = name

        # 3. Сохраняем куки на диск
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies_json, f, ensure_ascii=False, indent=2)

        # 4. K1: для нового аккаунта — пишем в accounts.json.
        # Раньше TG-бот добавлял в cfg["accounts"], но bot.py читает
        # accounts.json приоритетно (G2) — изменения через TG не доходили.
        if idx is None:
            rel_path = cookies_path.relative_to(self.BASE).as_posix()
            try:
                from accounts import add_account

                add_account(
                    self.BASE,
                    {"name": acc_name, "cookies_path": rel_path, "enabled": True},
                    cfg=self._cfg(),
                )
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
        self.bot.reply_to(
            message, f"✅ Куки сохранены для '{acc_name}' ({len(cookies_json)} записей)."
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

    def _save_cfg_text_field(self, message, cfg_key: str, success_text: str, *, require_url: bool = False):
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

    def _dialog_set_sphere_key(self, message, data):
        self._save_cfg_text_field(message, "sphere_api_key", "✅ Sphere API-ключ обновлён.")

    def _dialog_set_openai_key(self, message, data):
        self._save_cfg_text_field(message, "openai_api_key", "✅ OpenAI API Key обновлён.")

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
        self.bot.reply_to(message, f"✅ OpenAI Model установлена: {model}")
        self._show_settings(cid)

    def _dialog_set_adspower_url(self, message, data):
        self._save_cfg_text_field(
            message, "adspower_api_url", "✅ AdsPower API URL обновлён.", require_url=True
        )

    def _dialog_set_adspower_key(self, message, data):
        self._save_cfg_text_field(
            message, "adspower_api_key", "✅ AdsPower API Key обновлён."
        )

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
        self.bot.reply_to(message, f"✅ AdsPower ID для '{acc_name}' установлен: {user_id}")
        self._show_accounts(cid)

    def _handle_dialog(self, message):
        """S2 Stage 2: dispatch-table вместо большого if/elif. Маппинг
        state → method, неизвестные state'ы (легаси) тихо игнорируются —
        пользователь увидит, что бот не реагирует, и может написать /cancel.
        """
        if not self._allowed(message.from_user.id):
            return
        dialog = self._get_dialog(message.chat.id)
        state = dialog.get("state")
        data = dialog.get("data", {})

        # acc_add_cookies и acc_update_cookies — один и тот же handler,
        # отличаются только через data["idx"]: None vs число.
        handlers = {
            "acc_add_name": self._dialog_acc_add_name,
            "acc_add_cookies": self._dialog_acc_cookies,
            "acc_update_cookies": self._dialog_acc_cookies,
            "proxy_add": self._dialog_proxy_add,
            "proxy_replace": self._dialog_proxy_replace,
            "set_keywords": self._dialog_set_keywords,
            "set_url": self._dialog_set_url,
            "set_sphere_key": self._dialog_set_sphere_key,
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
            if payload.endswith("_c"):
                request_id = payload[:-2]
                response = "continue"
            elif payload.endswith("_x"):
                request_id = payload[:-2]
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
                f"✅ Ответ принят: {response}\n"
                f"Аккаунт «{req.account_name}», kind={req.kind}",
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

    def _cb_classification_menu(self, call):
        self._show_classification(call.message.chat.id, call.message)

    # ── Запуск/стоп/статус/логи ────────────────────────────────────────────

    def _cb_run(self, call):
        cid = call.message.chat.id
        if is_running():
            self._send(cid, "Бот уже запущен.")
            return
        if not self._run_callback:
            self._send(cid, "Ошибка: run_callback не задан.")
            return
        stop_event.clear()
        active_threads.clear()
        self._send(cid, "Запускаю потоки...")
        threading.Thread(target=self._run_callback, daemon=True, name="tg-runner").start()
        self._show_main(cid, call.message)

    def _cb_stop(self, call):
        cid = call.message.chat.id
        if not is_running():
            self._send(cid, "Бот не запущен.")
            return
        add_log("🛑 Stop signal sent. Threads will exit at next checkpoint.")
        stop_event.set()
        self.bot.answer_callback_query(call.id, "🛑 Останавливаю...")
        self._send(
            cid,
            "Сигнал остановки отправлен. Жду завершения потоков (до 30s)...",
            kb_back(),
        )
        # C2: ждём фактического завершения потоков в отдельном потоке,
        # чтобы не блокировать handler TG (callback должен возвращаться быстро).
        threading.Thread(
            target=self._join_threads_and_report,
            args=(list(active_threads), cid),
            daemon=True,
            name="tg-stop-joiner",
        ).start()

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

    def _cb_status(self, call):
        if not active_threads:
            text = "Потоков нет."
        else:
            lines = [
                f"{'🟢' if t.is_alive() else '🔴'} {t.name}: "
                f"{'работает' if t.is_alive() else 'завершён'}"
                for t in active_threads
            ]
            text = "\n".join(lines)
        self._edit_or_send(
            call.message.chat.id, call.message.message_id, text, kb_back()
        )

    def _cb_logs(self, call):
        recent = list(log_buffer)[-30:]
        text = "\n".join(recent) if recent else "Лог пуст."
        self._edit_or_send(
            call.message.chat.id, call.message.message_id, text, kb_back()
        )

    # ── Аккаунты (K1: все CRUD идёт через accounts.json) ──────────────────

    def _cb_acc_detail(self, call):
        """Показать карточку аккаунта (callback_data: acc_detail_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        acc = accs[idx]
        cookie_ok = (self.BASE / acc.get("cookies_path", "")).exists()
        text = (
            f"👤 {acc['name']}\n"
            f"AdsPower ID: {acc.get('user_id') or acc.get('adspower_id') or '❌ НЕ ЗАДАН'}\n"
            f"Куки: {'✅ есть' if cookie_ok else '❌ нет'}\n"
            f"Путь: {acc.get('cookies_path', '—')}\n"
            f"Enabled: {'✅' if acc.get('enabled', True) else '💤 (disabled)'}"
        )
        self._edit_or_send(cid, call.message.message_id, text, kb_account_detail(idx))

    def _cb_acc_add(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "acc_add_name")
        self._send(cid, "Введи имя нового аккаунта:")

    def _cb_acc_cookies(self, call):
        """Запрос обновления cookies для существующего аккаунта (acc_cookies_<idx>)."""
        cid = call.message.chat.id
        idx = int(call.data.split("_")[-1])
        accs = self._accounts()
        if idx >= len(accs):
            self._send(cid, "Аккаунт не найден.")
            return
        self._set_dialog(cid, "acc_update_cookies", {"idx": idx})
        self._send(cid, f"Отправь новый cookies.json для '{accs[idx]['name']}':")

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
        self._show_accounts(cid, call.message)

    # ── Прокси ──────────────────────────────────────────────────────────────

    def _cb_proxy_add(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "proxy_add")
        self._send(
            cid, "Введи прокси в формате:\nip:port:user:pass\n\nИли: /cancel для отмены"
        )

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
            f"Текущий URL:\n{cfg.get('target_url', '—')}\n\n"
            f"Отправь новую ссылку на объявление:",
        )

    def _cb_set_sphere_key(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_sphere_key")
        self._send(cid, "Отправь API-ключ Sphere:")

    def _cb_set_openai_key(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_openai_key")
        self._send(cid, "Отправь OpenAI API Key:")

    def _cb_set_openai_model(self, call):
        cid = call.message.chat.id
        self._set_dialog(cid, "set_openai_model")
        cfg = self._cfg()
        cur = cfg.get("openai_model", "gpt-3.5-turbo")
        self._send(
            cid, f"Текущая модель: {cur}\n\nОтправь название новой модели (напр. gpt-4o):"
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
        current = (
            "\n".join(f"{i + 1}. {k}" for i, k in enumerate(kws)) if kws else "(пусто)"
        )
        self._send(
            cid,
            f"Текущие ключевые слова:\n{current}\n\n"
            f"Отправь новый список (по одному на строку):",
        )

    # ── Классификация ──────────────────────────────────────────────────────

    def _cb_reclassify_all(self, call):
        cid = call.message.chat.id
        try:
            from database import DatabaseManager
            from listing_classifier import ListingClassifier

            db_manager = DatabaseManager()
            # L4: один _cfg() вместо двух — не дёргаем диск дважды.
            cfg = self._cfg()
            llm_config = {
                "api_key": cfg.get("openai_api_key", ""),
                "model": cfg.get("openai_model", "gpt-3.5-turbo"),
            }
            classifier = ListingClassifier(db_manager, llm_config)
            results = classifier.classify_all_listings()
            text = (
                f"Переклассификация завершена:\n"
                f"Всего обработано: {results['total_processed']}\n"
                f"Собственники: {results['owners']}\n"
                f"Агенты: {results['agents']}\n"
                f"Неопределенные: {results['uncertain']}"
            )
            self._send(cid, text, kb_classification())
        except Exception as e:
            self._send(cid, f"Ошибка переклассификации: {str(e)}", kb_classification())

    def _cb_classification_stats(self, call):
        # K3: используем DatabaseManager.get_classification_stats() вместо
        # прямого sqlite3.connect (соблюдаем AGENTS.md "Never bypass
        # DatabaseManager"). Раньше: ручной conn без WAL/busy_timeout,
        # без write_lock, без надёжного close при exception.
        cid = call.message.chat.id
        try:
            from database import DatabaseManager

            stats = DatabaseManager().get_classification_stats()
            if not stats["total"]:
                self._send(cid, "📊 Нет классифицированных объявлений.", kb_classification())
            else:
                lines = ["📊 Статистика классификации:"]
                for label, count in sorted(stats["by_label"].items()):
                    lines.append(f"{label}: {count}")
                lines.append(f"Всего: {stats['total']}")
                self._send(cid, "\n".join(lines), kb_classification())
        except Exception as e:
            logger.exception("classification_stats failed")
            self._send(cid, f"Ошибка получения статистики: {e}", kb_classification())

    def _cb_create_ground_truth(self, call):
        # Stub: triggers ground-truth dataset creation flow.
        self._send(call.message.chat.id, "Создание разметки 50 объявлений...", kb_classification())

    def _cb_evaluate_quality(self, call):
        # Stub: triggers classification-quality evaluation script.
        self._send(call.message.chat.id, "Оценка качества классификации...", kb_classification())

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
            ("acc_cookies_", self._cb_acc_cookies),
            ("acc_userid_", self._cb_acc_userid),
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
            "classification_menu": self._cb_classification_menu,
            # Управление
            "run": self._cb_run,
            "stop": self._cb_stop,
            "status": self._cb_status,
            "logs": self._cb_logs,
            # Аккаунты
            "acc_add": self._cb_acc_add,
            # Прокси
            "proxy_add": self._cb_proxy_add,
            "proxy_replace": self._cb_proxy_replace,
            # Настройки
            "set_url": self._cb_set_url,
            "set_sphere_key": self._cb_set_sphere_key,
            "set_openai_key": self._cb_set_openai_key,
            "set_openai_model": self._cb_set_openai_model,
            "set_adspower_url": self._cb_set_adspower_url,
            "set_adspower_key": self._cb_set_adspower_key,
            "set_threads": self._cb_set_threads,
            "set_keywords": self._cb_set_keywords,
            # Классификация
            "reclassify_all": self._cb_reclassify_all,
            "classification_stats": self._cb_classification_stats,
            "create_ground_truth": self._cb_create_ground_truth,
            "evaluate_quality": self._cb_evaluate_quality,
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
