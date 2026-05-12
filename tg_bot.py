"""
Telegram-бот для управления Avito-ботом.
Inline-кнопки, управление аккаунтами/прокси/настройками без редактирования файлов.
"""

import json
import logging
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
        with open(self.BASE / "config.json", encoding="utf-8") as f:
            return json.load(f)

    def _save_cfg(self, cfg: dict):
        with open(self.BASE / "config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

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

    # ── Экраны-меню (вызываются из команд и callback) ─────────────────────────

    def _show_main(self, chat_id, edit_msg=None):
        running = is_running()
        text = (
            f"Avito-бот\n"
            f"Статус: {'🟢 работает' if running else '🔴 остановлен'}\n"
            f"Потоков: {sum(1 for t in active_threads if t.is_alive())}"
        )
        if edit_msg:
            try:
                self.bot.edit_message_text(
                    text, edit_msg.chat.id, edit_msg.message_id, reply_markup=kb_main()
                )
                return
            except Exception:
                pass
        self._send(chat_id, text, kb_main())

    def _show_accounts(self, chat_id, edit_msg=None):
        cfg = self._cfg()
        accs = cfg.get("accounts", [])
        text = f"Аккаунты ({len(accs)}):"
        if edit_msg:
            try:
                self.bot.edit_message_text(
                    text, edit_msg.chat.id, edit_msg.message_id, reply_markup=kb_accounts(accs)
                )
                return
            except Exception:
                pass
        self._send(chat_id, text, kb_accounts(accs))

    def _show_proxies(self, chat_id, edit_msg=None):
        proxies = self._proxies()
        text = f"Прокси ({len(proxies)}):\n(нажми на прокси чтобы удалить)"
        if edit_msg:
            try:
                self.bot.edit_message_text(
                    text, edit_msg.chat.id, edit_msg.message_id, reply_markup=kb_proxies(proxies)
                )
                return
            except Exception:
                pass
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
            try:
                self.bot.edit_message_text(
                    text, edit_msg.chat.id, edit_msg.message_id, reply_markup=kb_settings()
                )
                return
            except Exception:
                pass
        self._send(chat_id, text, kb_settings())

    def _show_classification(self, chat_id, edit_msg=None):
        text = "🔍 Классификация объявлений"
        if edit_msg:
            try:
                self.bot.edit_message_text(
                    text, edit_msg.chat.id, edit_msg.message_id, reply_markup=kb_classification()
                )
                return
            except Exception:
                pass
        self._send(chat_id, text, kb_classification())

    # ══════════════════════════════════════════════════════════════════════════
    # Регистрация хендлеров
    # ══════════════════════════════════════════════════════════════════════════

    def _setup(self):
        bot = self.bot

        # ── /start ────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["start", "menu"])
        def cmd_start(message):
            if not self._allowed(message.from_user.id):
                bot.reply_to(message, "Нет доступа.")
                return
            self._clear_dialog(message.chat.id)
            self._show_main(message.chat.id)

        # ── /report ──────────────────────────────────────────────────────────
        # E3: краткая сводка за сутки. Аргумент 'all' — за всё время.
        @bot.message_handler(commands=["report"])
        def cmd_report(message):
            if not self._allowed(message.from_user.id):
                bot.reply_to(message, "Нет доступа.")
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
                bot.reply_to(message, "\n".join(lines))
            except Exception as exc:
                logger.exception("cmd_report failed")
                bot.reply_to(message, f"Ошибка отчёта: {exc}")

        # ── Текстовый ввод (диалоговые состояния) ────────────────────────────
        @bot.message_handler(
            content_types=["text", "document"], func=lambda m: m.chat.id in self._state
        )
        def handle_dialog(message):
            if not self._allowed(message.from_user.id):
                return
            dialog = self._get_dialog(message.chat.id)
            state = dialog.get("state")
            data = dialog.get("data", {})
            cid = message.chat.id

            # ── Ожидание имени нового аккаунта ────────────────────────────────
            if state == "acc_add_name":
                name = message.text.strip()
                if not name:
                    bot.reply_to(message, "Имя не может быть пустым.")
                    return
                # Проверяем что такого нет
                cfg = self._cfg()
                if any(a["name"] == name for a in cfg.get("accounts", [])):
                    bot.reply_to(message, f"Аккаунт '{name}' уже существует.")
                    return
                self._set_dialog(cid, "acc_add_cookies", {"name": name})
                bot.reply_to(
                    message,
                    f"Аккаунт: {name}\n\nОтправь файл cookies.json "
                    f"или вставь JSON-текст кук.\n\nОтправь /cancel для отмены.",
                )

            # ── Ожидание кук (файл или текст) для нового/обновляемого аккаунта
            elif state in ("acc_add_cookies", "acc_update_cookies"):
                idx = data.get("idx")  # None = новый, число = обновление
                name = data.get("name")

                cookies_json = None

                if message.content_type == "document":
                    # Файл cookies.json
                    try:
                        file_info = bot.get_file(message.document.file_id)
                        downloaded = bot.download_file(file_info.file_path)
                        cookies_json = json.loads(downloaded.decode("utf-8"))
                    except Exception as e:
                        bot.reply_to(message, f"Ошибка чтения файла: {e}")
                        return
                else:
                    # Текст JSON
                    try:
                        cookies_json = json.loads(message.text.strip())
                    except Exception:
                        bot.reply_to(
                            message,
                            "Не удалось распознать JSON. "
                            "Отправь файл .json или валидный JSON-текст.",
                        )
                        return

                if not isinstance(cookies_json, list):
                    bot.reply_to(message, "Куки должны быть массивом [].")
                    return

                # Определяем путь
                if idx is not None:
                    cfg = self._cfg()
                    accs = cfg.get("accounts", [])
                    if idx >= len(accs):
                        bot.reply_to(message, "Аккаунт не найден.")
                        self._clear_dialog(cid)
                        return
                    cookies_path = self.BASE / accs[idx]["cookies_path"]
                    acc_name = accs[idx]["name"]
                else:
                    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
                    cookies_path = self.BASE / "accounts" / safe_name / "cookies.json"
                    acc_name = name

                # Сохраняем куки
                cookies_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cookies_path, "w", encoding="utf-8") as f:
                    json.dump(cookies_json, f, ensure_ascii=False, indent=2)

                # Добавляем в config если новый
                if idx is None:
                    cfg = self._cfg()
                    rel_path = cookies_path.relative_to(self.BASE).as_posix()
                    cfg.setdefault("accounts", []).append(
                        {
                            "name": acc_name,
                            "cookies_path": rel_path,
                        }
                    )
                    self._save_cfg(cfg)

                self._clear_dialog(cid)
                bot.reply_to(
                    message, f"✅ Куки сохранены для '{acc_name}' ({len(cookies_json)} записей)."
                )
                self._show_accounts(cid)

            # ── Ожидание прокси (одна строка) ─────────────────────────────────
            elif state == "proxy_add":
                line = message.text.strip()
                if not line:
                    bot.reply_to(message, "Пустая строка.")
                    return
                proxies = self._proxies()
                proxies.append(line)
                self._save_proxies(proxies)
                self._clear_dialog(cid)
                bot.reply_to(message, f"✅ Прокси добавлен: {line}")
                self._show_proxies(cid)

            # ── Ожидание замены всех прокси ───────────────────────────────────
            elif state == "proxy_replace":
                lines = [line.strip() for line in message.text.splitlines() if line.strip()]
                if not lines:
                    bot.reply_to(message, "Список пустой.")
                    return
                self._save_proxies(lines)
                self._clear_dialog(cid)
                bot.reply_to(message, f"✅ Сохранено {len(lines)} прокси.")
                self._show_proxies(cid)

            # ── Ожидание ключевых слов ────────────────────────────────────────
            elif state == "set_keywords":
                lines = [line.strip() for line in message.text.splitlines() if line.strip()]
                if not lines:
                    bot.reply_to(message, "Список пустой.")
                    return
                self._save_keywords(lines)
                self._clear_dialog(cid)
                bot.reply_to(message, f"✅ Сохранено {len(lines)} ключевых слов.")
                self._show_settings(cid)

            # ── Ожидание URL ──────────────────────────────────────────────────
            elif state == "set_url":
                url = message.text.strip()
                if not url.startswith("http"):
                    bot.reply_to(message, "Не похоже на URL.")
                    return
                cfg = self._cfg()
                cfg["target_url"] = url
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(message, "✅ URL обновлён.")
                self._show_settings(cid)

            # ── Ожидание Sphere API-ключа ──────────────────────────────────────
            elif state == "set_sphere_key":
                key = message.text.strip()
                if not key:
                    bot.reply_to(message, "Ключ пустой.")
                    return
                cfg = self._cfg()
                cfg["sphere_api_key"] = key
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(message, "✅ Sphere API-ключ обновлён.")
                self._show_settings(cid)

            # ── Ожидание OpenAI API-ключа ──────────────────────────────────────
            elif state == "set_openai_key":
                key = message.text.strip()
                if not key:
                    bot.reply_to(message, "Ключ пустой.")
                    return
                cfg = self._cfg()
                cfg["openai_api_key"] = key
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(message, "✅ OpenAI API Key обновлён.")
                self._show_settings(cid)

            # ── Ожидание OpenAI модели ──────────────────────────────────────
            elif state == "set_openai_model":
                model = message.text.strip()
                if not model:
                    bot.reply_to(message, "Модель не может быть пустой.")
                    return
                cfg = self._cfg()
                cfg["openai_model"] = model
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(message, f"✅ OpenAI Model установлена: {model}")
                self._show_settings(cid)

            # ── Ожидание AdsPower API URL ─────────────────────────────────────
            elif state == "set_adspower_url":
                url = message.text.strip()
                if not url.startswith("http"):
                    bot.reply_to(message, "Не похоже на URL.")
                    return
                cfg = self._cfg()
                cfg["adspower_api_url"] = url
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(message, "✅ AdsPower API URL обновлён.")
                self._show_settings(cid)

            # ── Ожидание AdsPower API-ключа ────────────────────────────────────
            elif state == "set_adspower_key":
                key = message.text.strip()
                if not key:
                    bot.reply_to(message, "Ключ пустой.")
                    return
                cfg = self._cfg()
                cfg["adspower_api_key"] = key
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(message, "✅ AdsPower API Key обновлён.")
                self._show_settings(cid)

            # ── Ожидание кол-ва потоков ───────────────────────────────────────
            elif state == "set_threads":
                if not message.text.strip().isdigit():
                    bot.reply_to(message, "Введи число (0 = без ограничений).")
                    return
                n = int(message.text.strip())
                if n > 50:
                    bot.reply_to(message, "Максимум 50.")
                    return
                cfg = self._cfg()
                cfg["threads"] = n
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(message, f"✅ Потоков: {n or 'без ограничений'}")
                self._show_settings(cid)

            # ── Ожидание AdsPower User ID для аккаунта ────────────────────────
            elif state == "acc_set_userid":
                idx = data.get("idx")
                user_id = message.text.strip()
                if not user_id:
                    bot.reply_to(message, "ID не может быть пустым.")
                    return
                cfg = self._cfg()
                accs = cfg.get("accounts", [])
                if idx >= len(accs):
                    bot.reply_to(message, "Аккаунт не найден.")
                    self._clear_dialog(cid)
                    return
                accs[idx]["user_id"] = user_id
                self._save_cfg(cfg)
                self._clear_dialog(cid)
                bot.reply_to(
                    message, f"✅ AdsPower ID для '{accs[idx]['name']}' установлен: {user_id}"
                )
                self._show_accounts(cid)

        # ── /cancel ───────────────────────────────────────────────────────────
        @bot.message_handler(commands=["cancel"])
        def cmd_cancel(message):
            self._clear_dialog(message.chat.id)
            bot.reply_to(message, "Отменено.", reply_markup=ReplyKeyboardRemove())
            self._show_main(message.chat.id)

        # ── Callback-кнопки ───────────────────────────────────────────────────
        @bot.callback_query_handler(func=lambda c: True)
        def on_callback(call):
            if not self._allowed(call.from_user.id):
                bot.answer_callback_query(call.id, "Нет доступа.")
                return

            # Отвечаем Telegram СРАЗУ — иначе кнопка "зависает" со спиннером.
            # Для тостов (короткое всплывающее сообщение) используем show_alert.
            bot.answer_callback_query(call.id)

            cid = call.message.chat.id
            d = call.data

            # ── B1: user-resume callback (SMS / login captcha) ────────────────
            # Формат: b1_res_<request_id>_<c|x>
            if d.startswith("b1_res_"):
                try:
                    payload = d[len("b1_res_") :]
                    if payload.endswith("_c"):
                        request_id = payload[:-2]
                        response = "continue"
                    elif payload.endswith("_x"):
                        request_id = payload[:-2]
                        response = "cancel"
                    else:
                        bot.answer_callback_query(call.id, "Bad payload")
                        return
                except Exception:
                    bot.answer_callback_query(call.id, "Bad payload")
                    return

                from account_state import account_state as _astate

                req = _astate.find_request(request_id)
                if req is None:
                    self._send(cid, f"Запрос {request_id} не найден или уже закрыт.")
                    return

                ok = _astate.notify_user_resumed(req.account_name, request_id, response)
                if ok:
                    add_log(f"[{req.account_name}] admin -> {response} (id={request_id})")
                    try:
                        bot.edit_message_text(
                            f"✅ Ответ принят: {response}\n"
                            f"Аккаунт «{req.account_name}», kind={req.kind}",
                            cid,
                            call.message.message_id,
                        )
                    except Exception:
                        self._send(cid, f"Ответ принят: {response}")
                else:
                    self._send(cid, f"Не удалось закрыть запрос {request_id}.")
                return

            # ── Навигация ─────────────────────────────────────────────────────
            if d == "menu_main":
                self._clear_dialog(cid)
                self._show_main(cid, call.message)
            elif d == "accounts_menu":
                self._show_accounts(cid, call.message)
            elif d == "proxies_menu":
                self._show_proxies(cid, call.message)
            elif d == "settings_menu":
                self._show_settings(cid, call.message)

            # ── Запуск/стоп ───────────────────────────────────────────────────
            elif d == "run":
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

            elif d == "stop":
                if not is_running():
                    self._send(cid, "Бот не запущен.")
                    return
                add_log("🛑 Stop signal sent. Threads will exit at next checkpoint.")
                stop_event.set()
                bot.answer_callback_query(call.id, "🛑 Останавливаю...")
                self._send(
                    cid,
                    "Сигнал остановки отправлен. Жду завершения потоков (до 30s)...",
                    kb_back(),
                )

                # C2: ждём фактического завершения потоков, а не сразу
                # отчитываемся "сигнал отправлен". join вызываем в отдельном
                # потоке, чтобы не блокировать handler TG (callback_query
                # обработчик должен возвращаться быстро).
                def _join_threads_and_report(threads, chat_id):
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

                threading.Thread(
                    target=_join_threads_and_report,
                    args=(list(active_threads), cid),
                    daemon=True,
                    name="tg-stop-joiner",
                ).start()

            # ── Статус ────────────────────────────────────────────────────────
            elif d == "status":
                if not active_threads:
                    text = "Потоков нет."
                else:
                    lines = [
                        f"{'🟢' if t.is_alive() else '🔴'} {t.name}: "
                        f"{'работает' if t.is_alive() else 'завершён'}"
                        for t in active_threads
                    ]
                    text = "\n".join(lines)
                try:
                    bot.edit_message_text(
                        text, cid, call.message.message_id, reply_markup=kb_back()
                    )
                except Exception:
                    self._send(cid, text, kb_back())

            # ── Логи ──────────────────────────────────────────────────────────
            elif d == "logs":
                recent = list(log_buffer)[-30:]
                text = "\n".join(recent) if recent else "Лог пуст."
                try:
                    bot.edit_message_text(
                        text, cid, call.message.message_id, reply_markup=kb_back()
                    )
                except Exception:
                    self._send(cid, text, kb_back())

            # ── Аккаунты: детали ──────────────────────────────────────────────
            elif d.startswith("acc_detail_"):
                idx = int(d.split("_")[-1])
                cfg = self._cfg()
                accs = cfg.get("accounts", [])
                if idx >= len(accs):
                    self._send(cid, "Аккаунт не найден.")
                    return
                acc = accs[idx]
                cookie_ok = (self.BASE / acc.get("cookies_path", "")).exists()
                text = (
                    f"👤 {acc['name']}\n"
                    f"AdsPower ID: {acc.get('user_id', '❌ НЕ ЗАДАН')}\n"
                    f"Куки: {'✅ есть' if cookie_ok else '❌ нет'}\n"
                    f"Путь: {acc.get('cookies_path', '—')}"
                )
                try:
                    bot.edit_message_text(
                        text, cid, call.message.message_id, reply_markup=kb_account_detail(idx)
                    )
                except Exception:
                    self._send(cid, text, kb_account_detail(idx))

            elif d == "acc_add":
                self._set_dialog(cid, "acc_add_name")
                self._send(cid, "Введи имя нового аккаунта:")

            elif d.startswith("acc_cookies_"):
                idx = int(d.split("_")[-1])
                cfg = self._cfg()
                accs = cfg.get("accounts", [])
                if idx >= len(accs):
                    self._send(cid, "Аккаунт не найден.")
                    return
                self._set_dialog(cid, "acc_update_cookies", {"idx": idx})
                self._send(cid, f"Отправь новый cookies.json для '{accs[idx]['name']}':")

            elif d.startswith("acc_userid_"):
                idx = int(d.split("_")[-1])
                cfg = self._cfg()
                accs = cfg.get("accounts", [])
                if idx >= len(accs):
                    self._send(cid, "Аккаунт не найден.")
                    return
                self._set_dialog(cid, "acc_set_userid", {"idx": idx})
                self._send(cid, f"Введите AdsPower User ID для аккаунта '{accs[idx]['name']}':")

            elif d.startswith("acc_del_"):
                idx = int(d.split("_")[-1])
                cfg = self._cfg()
                accs = cfg.get("accounts", [])
                if idx >= len(accs):
                    self._send(cid, "Аккаунт не найден.")
                    return
                try:
                    bot.edit_message_text(
                        f"Удалить аккаунт '{accs[idx]['name']}'?",
                        cid,
                        call.message.message_id,
                        reply_markup=kb_confirm(f"acc_del_ok_{idx}", "accounts_menu"),
                    )
                except Exception:
                    self._send(
                        cid,
                        f"Удалить '{accs[idx]['name']}'?",
                        kb_confirm(f"acc_del_ok_{idx}", "accounts_menu"),
                    )

            elif d.startswith("acc_del_ok_"):
                idx = int(d.split("_")[-1])
                cfg = self._cfg()
                accs = cfg.get("accounts", [])
                if idx >= len(accs):
                    self._send(cid, "Аккаунт не найден.")
                    return
                # Имя удаляемого аккаунта используется только для лога;
                # _show_accounts перерисует список и будет видно, что нет.
                logger.info("Removing account index=%s name=%s", idx, accs[idx].get("name"))
                cfg["accounts"].pop(idx)
                self._save_cfg(cfg)
                self._show_accounts(cid, call.message)

            # ── Прокси ────────────────────────────────────────────────────────
            elif d == "proxy_add":
                self._set_dialog(cid, "proxy_add")
                self._send(
                    cid, "Введи прокси в формате:\nip:port:user:pass\n\nИли: /cancel для отмены"
                )

            elif d == "proxy_replace":
                self._set_dialog(cid, "proxy_replace")
                self._send(
                    cid,
                    "Отправь список прокси (по одному на строку):\n"
                    "ip:port:user:pass\n\n"
                    "Они ЗАМЕНЯТ текущий список.\n/cancel — отмена",
                )

            elif d.startswith("proxy_del_confirm_"):
                idx = int(d.split("_")[-1])
                proxies = self._proxies()
                if idx >= len(proxies):
                    self._send(cid, "Прокси не найден.")
                    return
                try:
                    bot.edit_message_text(
                        f"Удалить прокси?\n{proxies[idx]}",
                        cid,
                        call.message.message_id,
                        reply_markup=kb_confirm(f"proxy_del_ok_{idx}", "proxies_menu"),
                    )
                except Exception:
                    self._send(
                        cid,
                        f"Удалить?\n{proxies[idx]}",
                        kb_confirm(f"proxy_del_ok_{idx}", "proxies_menu"),
                    )

            elif d.startswith("proxy_del_ok_"):
                idx = int(d.split("_")[-1])
                proxies = self._proxies()
                if idx >= len(proxies):
                    self._send(cid, "Прокси не найден.")
                    return
                proxies.pop(idx)
                self._save_proxies(proxies)
                self._show_proxies(cid, call.message)

            # ── Настройки ─────────────────────────────────────────────────────
            elif d == "set_url":
                self._set_dialog(cid, "set_url")
                cfg = self._cfg()
                self._send(
                    cid,
                    f"Текущий URL:\n{cfg.get('target_url', '—')}\n\n"
                    f"Отправь новую ссылку на объявление:",
                )

            elif d == "set_sphere_key":
                self._set_dialog(cid, "set_sphere_key")
                self._send(cid, "Отправь API-ключ Sphere:")

            elif d == "set_openai_key":
                self._set_dialog(cid, "set_openai_key")
                self._send(cid, "Отправь OpenAI API Key:")

            elif d == "set_openai_model":
                self._set_dialog(cid, "set_openai_model")
                cfg = self._cfg()
                cur = cfg.get("openai_model", "gpt-3.5-turbo")
                self._send(
                    cid, f"Текущая модель: {cur}\n\nОтправь название новой модели (напр. gpt-4o):"
                )

            elif d == "set_adspower_url":
                self._set_dialog(cid, "set_adspower_url")
                cfg = self._cfg()
                cur = cfg.get("adspower_api_url", "—")
                self._send(cid, f"Текущий URL: {cur}\n\nОтправь новый AdsPower API URL:")

            elif d == "set_adspower_key":
                self._set_dialog(cid, "set_adspower_key")
                self._send(cid, "Отправь AdsPower API Key:")

            elif d == "set_threads":
                self._set_dialog(cid, "set_threads")
                cfg = self._cfg()
                cur = cfg.get("threads", 0) or "без ограничений"
                self._send(
                    cid,
                    f"Текущее кол-во потоков: {cur}\n\n"
                    f"Введи новое значение (0 = без ограничений, макс. 50):",
                )

            elif d == "set_keywords":
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

            elif d == "classification_menu":
                self._show_classification(cid, call.message)

            elif d == "reclassify_all":
                # Import the classifier
                try:
                    from database import DatabaseManager
                    from listing_classifier import ListingClassifier

                    db_manager = DatabaseManager()
                    llm_config = {
                        "api_key": self._cfg().get("openai_api_key", ""),
                        "model": self._cfg().get("openai_model", "gpt-3.5-turbo"),
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

            elif d == "classification_stats":
                try:
                    import sqlite3

                    from database import DatabaseManager

                    db_manager = DatabaseManager()
                    # Get classification statistics
                    conn = sqlite3.connect(db_manager.db_path)
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT classification, COUNT(*) as count FROM listings WHERE classification IS NOT NULL GROUP BY classification"
                    )
                    results = cursor.fetchall()
                    stats = {row[0]: row[1] for row in results}
                    total = sum(count for _, count in results)
                    text = "📊 Статистика классификации:\n"
                    for classification, count in stats.items():
                        text += f"{classification}: {count}\n"
                    text += f"Всего: {total}"
                    self._send(cid, text, kb_classification())
                    conn.close()
                except Exception as e:
                    self._send(cid, f"Ошибка получения статистики: {str(e)}", kb_classification())

            elif d == "create_ground_truth":
                # This would trigger the creation of ground truth data
                self._send(cid, "Создание разметки 50 объявлений...", kb_classification())

            elif d == "evaluate_quality":
                # This would run the evaluation script
                self._send(cid, "Оценка качества классификации...", kb_classification())

    # ── Polling ───────────────────────────────────────────────────────────────

    def start_polling(self):
        self.bot.infinity_polling(timeout=20, long_polling_timeout=10, logger_level=None)
