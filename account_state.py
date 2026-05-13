"""
In-memory state per account.

Отвечает за:
  - CAPTCHA-cooldown (A3/старый): когда аккаунт попал на капчу, помечаем
    как "cooled_down" на N минут.
  - Дневные бюджеты (A2): guard'ы на listings/messages/phone per day,
    основанные на E2-метриках из БД (для listings/messages) и in-memory
    счётчике (для phone_clicks, без доп. DB-запроса внутри парсера).
  - Сессионные phone-счётчики (A3): ограничение кликов "Показать телефон"
    на уровне сессии (30% soft-limit и перенос >5 кликов в следующую сессию).
  - Warmup-режим (B1): первые N дней аккаунт работает в щадящем режиме —
    не кликает телефоны, не отправляет сообщения, парсит меньше.
  - Long-cooldown при серии капч (B4): если за 24ч >= 3 капчи →
    автоматический cooldown 4-8 ч; >= 5 капч → cooldown до следующего дня.

State не персистится между перезапусками бота — для cooldown'а в 15-30 минут
и phone-счётчиков это нормально.

Thread-safe: операции защищены lock'ом, т.к. бот многопоточный
(по потоку на аккаунт).

Использование:
    from account_state import account_state

    if account_state.is_cooled_down("acc1"):
        log("acc1", f"в cooldown, осталось {account_state.cooldown_remaining_seconds('acc1')}s")
        skip_account()

    # A2: проверка дневного бюджета (listings / messages)
    if not account_state.check_daily_budget("acc1", "listings", db_manager):
        skip_listings()

    # A3: проверка лимита кликов "Показать телефон"
    if account_state.should_skip_phone("acc1"):
        skip_phone_click()

    # B1: warmup-режим (устанавливается из bot.run_thread по created_at)
    if account_state.is_in_warmup("acc1"):
        skip_messages()

    # в commercial_parser, после детекта капчи:
    account_state.mark_captcha("acc1")
"""

from __future__ import annotations

import datetime
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Дефолтная длительность cooldown'а после капчи. Можно переопределить через
# config.json (ключ "captcha_cooldown_minutes") — bot.py вызывает
# configure_from_cfg(cfg) на старте.
DEFAULT_CAPTCHA_COOLDOWN_MINUTES = 30

# ── A2: Дневные бюджеты ───────────────────────────────────────────────────
# Глобальные дефолты. Переопределяются через config.json (ключи
# daily_budget_listings / daily_budget_messages / daily_budget_phone)
# или per-account в accounts.json (те же ключи).
DEFAULT_DAILY_BUDGET: dict[str, int] = {
    "listings": 80,  # листингов в день
    "messages": 30,  # сообщений в день
    "phone": 25,  # кликов "Показать телефон" в день
}

# Метрики в БД, соответствующие действиям (для get_metrics-запроса).
_BUDGET_METRIC_MAP: dict[str, str] = {
    "listings": "listings_parsed",
    "messages": "messages_sent",
    "phone": "phone_clicks",  # новая метрика (A3)
}


def configure_from_cfg(cfg: dict) -> None:
    """
    Подхватывает настройки account_state из config.json:
        captcha_cooldown_minutes (int|float) — длительность cooldown'а после
            детекта капчи. Если ключа нет — оставляем дефолт (30).
        daily_budget_listings / daily_budget_messages / daily_budget_phone —
            глобальные дефолты дневных бюджетов (A2).
    """
    global DEFAULT_CAPTCHA_COOLDOWN_MINUTES
    raw = cfg.get("captcha_cooldown_minutes")
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "captcha_cooldown_minutes=%r в config.json не парсится — оставляю %d",
                raw,
                DEFAULT_CAPTCHA_COOLDOWN_MINUTES,
            )
        else:
            if value <= 0:
                logger.warning(
                    "captcha_cooldown_minutes=%r <= 0 — оставляю %d",
                    raw,
                    DEFAULT_CAPTCHA_COOLDOWN_MINUTES,
                )
            else:
                DEFAULT_CAPTCHA_COOLDOWN_MINUTES = value
                logger.info("captcha_cooldown_minutes = %s min", value)

    # A2: глобальные дефолты дневных бюджетов из config.json
    for action in ("listings", "messages", "phone"):
        key = f"daily_budget_{action}"
        raw_budget = cfg.get(key)
        if raw_budget is not None:
            try:
                DEFAULT_DAILY_BUDGET[action] = int(raw_budget)
                logger.info("daily_budget[%s] = %d", action, DEFAULT_DAILY_BUDGET[action])
            except (TypeError, ValueError):
                logger.warning(
                    "%s=%r в config.json не парсится — оставляю %d",
                    key,
                    raw_budget,
                    DEFAULT_DAILY_BUDGET[action],
                )


@dataclass
class UserResumeRequest:
    """
    B1: Запрос «требуется ручное действие администратора»
    (например, ввести SMS-код или решить капчу на login).

    Поток бота создаёт запрос → блокируется на event'е → админ нажимает
    в TG кнопку → set event → поток просыпается и продолжает.
    """

    request_id: str
    account_name: str
    kind: str  # "login_sms" | "login_captcha" | "manual_resume" | ...
    prompt: str  # человекочитаемое описание для админа
    created_at: float
    event: threading.Event = field(default_factory=threading.Event)
    response: str | None = None  # "continue" | "cancel" | None (timeout)
    answered_at: float = 0.0

    @property
    def answered(self) -> bool:
        return self.event.is_set()


@dataclass
class _Entry:
    cooldown_until: float = 0.0  # unix-time; 0 = не в cooldown
    captcha_hits: int = 0  # счётчик капчей за процесс
    last_captcha_at: float = 0.0  # unix-time последнего инцидента
    # pending user-resume запросы (один аккаунт может иметь несколько,
    # но обычно последний — актуальный).
    pending_requests: dict[str, UserResumeRequest] = field(default_factory=dict)
    # G2: per-account override длительности cooldown'а (минуты). None ⇒
    # используем глобальный DEFAULT_CAPTCHA_COOLDOWN_MINUTES.
    cooldown_minutes_override: float | None = None

    # ── A2: per-account дневные бюджеты ──────────────────────────────────
    # Если задан override — используется вместо DEFAULT_DAILY_BUDGET.
    daily_budget_overrides: dict[str, int] = field(default_factory=dict)

    # ── A3: phone-clicks счётчики ─────────────────────────────────────────
    # In-memory дневной счётчик (автосброс при смене даты).
    phone_clicks_date: str = ""  # "YYYY-MM-DD" — дата последнего сброса
    phone_clicks_today: int = 0  # кликов "Показать телефон" сегодня
    # Сессионные счётчики (сбрасываются в start_new_session, вызываемом из A4-loop).
    session_phone_clicks: int = 0  # кликов в текущей сессии
    prev_session_phone_clicks: int = 0  # кликов в предыдущей сессии

    # ── B1: warmup-режим ──────────────────────────────────────────────────
    # unix-time окончания warmup-периода (0 = не в warmup).
    warmup_until: float = 0.0

    # ── B4: timestamps капч за последние 24ч (для long-cooldown) ─────────
    captcha_timestamps: list = field(default_factory=list)  # list[float]

    # ── C2: de-dup бюджетных алертов — чтобы не слать дважды за день ──────
    # maps "action_pct" ("listings_80") → "YYYY-MM-DD"
    budget_alert_sent: dict = field(default_factory=dict)

    # ── F5b: dialog_id'ы, которые бот решил никогда не отвечать ──────────
    # Bросок 5% при первом просмотре нового диалога (см. AvitoMessenger).
    # State in-memory, сбрасывается при рестарте — это допустимо: после
    # рестарта максимум один раз ответим «сразу», что не страшно.
    ignored_dialogs: set = field(default_factory=set)

    # ── F7: random «dead days» ────────────────────────────────────────────
    # Per-account override базовой вероятности dead-day. None ⇒ глобальный
    # дефолт 0.05 (плюс ×3 boost в выходные). Задаётся через accounts.json
    # ключ "dead_day_rate" (см. bot.run_thread).
    dead_day_rate: float | None = None
    # Кэш решения "сегодня — выходной?" — формат {"date": "YYYY-MM-DD",
    # "is_dead": bool}. Бросок монетки делается ОДИН раз в день при первом
    # вызове is_dead_day; все последующие вызовы возвращают то же решение.
    dead_day_decision: dict = field(default_factory=dict)


class AccountState:
    """
    Хранит in-memory state по аккаунтам. Все методы потокобезопасные.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}

    def _get(self, account_name: str) -> _Entry:
        # Должен вызываться под lock.
        entry = self._entries.get(account_name)
        if entry is None:
            entry = _Entry()
            self._entries[account_name] = entry
        return entry

    # ──────────────────────────────────────────────────────────────────────
    # Capture / query
    # ──────────────────────────────────────────────────────────────────────

    def set_account_cooldown_minutes(
        self,
        account_name: str,
        minutes: float | None,
    ) -> None:
        """
        G2: установить per-account override длительности captcha-cooldown'а.
        `minutes=None` сбрасывает override на глобальный default.

        Безопасно вызывать на старте потока бота из значения
        `accounts.json` поля `captcha_cooldown_minutes`.
        """
        if minutes is not None:
            try:
                minutes = float(minutes)
            except (TypeError, ValueError):
                logger.warning(
                    "[%s] G2: captcha_cooldown_minutes=%r не парсится — игнорирую",
                    account_name,
                    minutes,
                )
                return
            if minutes <= 0:
                logger.warning(
                    "[%s] G2: captcha_cooldown_minutes=%s <= 0 — игнорирую",
                    account_name,
                    minutes,
                )
                return
        with self._lock:
            self._get(account_name).cooldown_minutes_override = minutes

    def mark_captcha(
        self,
        account_name: str,
        cooldown_minutes: float | None = None,
    ) -> float:
        """
        Помечает, что аккаунт попал на капчу. Сдвигает cooldown_until
        вперёд (если предыдущий был дольше — оставляем дольший).

        B4: дополнительно отслеживает количество капч за последние 24ч:
          - >= 3 → long-cooldown 4-8 ч (override переданного cooldown_minutes).
          - >= 5 → cooldown до следующего календарного дня + бюджеты вдвое.

        cooldown_minutes:
          - явное значение -> используем его (если не перебивается B4);
          - None (по умолчанию) -> per-account override (set_account_cooldown_minutes),
            иначе глобальный DEFAULT_CAPTCHA_COOLDOWN_MINUTES.

        Возвращает unix-time момента окончания cooldown'а.
        """
        now = time.time()
        with self._lock:
            entry = self._get(account_name)

            # Базовый cooldown (из аргумента / per-account override / global default)
            effective_minutes = (
                cooldown_minutes
                if cooldown_minutes is not None
                else (
                    entry.cooldown_minutes_override
                    if entry.cooldown_minutes_override is not None
                    else DEFAULT_CAPTCHA_COOLDOWN_MINUTES
                )
            )

            # B4: обновляем список timestamps и считаем капчи за 24ч
            entry.captcha_timestamps.append(now)
            cutoff = now - 86400
            entry.captcha_timestamps = [ts for ts in entry.captcha_timestamps if ts > cutoff]
            hits_24h = len(entry.captcha_timestamps)

            if hits_24h >= 5:
                # Очень плохо — cooldown до следующего календарного дня
                tomorrow = datetime.datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + datetime.timedelta(days=1)
                long_until = tomorrow.timestamp()
                entry.cooldown_until = max(entry.cooldown_until, long_until)
                # Бюджеты вдвое — чтобы завтра не нагружать так же
                for action in ("listings", "messages", "phone"):
                    cur = entry.daily_budget_overrides.get(
                        action, DEFAULT_DAILY_BUDGET.get(action, 80)
                    )
                    entry.daily_budget_overrides[action] = max(1, cur // 2)
                logger.warning(
                    "[%s] B4: %d капч за 24ч — cooldown до %s, бюджеты вдвое",
                    account_name,
                    hits_24h,
                    tomorrow.strftime("%Y-%m-%d %H:%M"),
                )
            elif hits_24h >= 3:
                # Долгий cooldown 4-8 ч вместо обычного
                long_minutes = random.uniform(240, 480)
                long_until = now + long_minutes * 60
                entry.cooldown_until = max(entry.cooldown_until, long_until)
                logger.warning(
                    "[%s] B4: %d капчи за 24ч — длинный cooldown %.0f мин",
                    account_name,
                    hits_24h,
                    long_minutes,
                )
            else:
                new_until = now + effective_minutes * 60.0
                entry.cooldown_until = max(entry.cooldown_until, new_until)

            entry.captcha_hits += 1
            entry.last_captcha_at = now
            logger.warning(
                "[%s] CAPTCHA hit #%d (24h: %d) -> cooldown until %s",
                account_name,
                entry.captcha_hits,
                hits_24h,
                time.strftime("%H:%M:%S", time.localtime(entry.cooldown_until)),
            )
            return entry.cooldown_until

    def clear_cooldown(self, account_name: str) -> None:
        """Снять cooldown (например, после ручного решения капчи)."""
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is not None:
                entry.cooldown_until = 0.0

    def is_cooled_down(self, account_name: str) -> bool:
        """True, если аккаунт сейчас в cooldown."""
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None:
                return False
            return time.time() < entry.cooldown_until

    def cooldown_remaining_seconds(self, account_name: str) -> int:
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None:
                return 0
            remaining = entry.cooldown_until - time.time()
            return max(0, int(remaining))

    def captcha_hits(self, account_name: str) -> int:
        with self._lock:
            entry = self._entries.get(account_name)
            return entry.captcha_hits if entry else 0

    def captcha_hits_24h(self, account_name: str) -> int:
        """B4: количество капч за последние 24 часа."""
        cutoff = time.time() - 86400
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None:
                return 0
            return sum(1 for ts in entry.captcha_timestamps if ts > cutoff)

    def last_captcha_at(self, account_name: str) -> float | None:
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None or entry.last_captcha_at == 0.0:
                return None
            return entry.last_captcha_at

    # ──────────────────────────────────────────────────────────────────────
    # B1: Warmup-режим для новых аккаунтов
    # ──────────────────────────────────────────────────────────────────────

    def set_warmup_until(self, account_name: str, ts: float) -> None:
        """
        B1: установить момент окончания warmup-периода (unix-time).
        Вызывается из bot.run_thread при старте потока аккаунта,
        вычисляется как created_at + warmup_days * 86400.
        ts=0 сбрасывает warmup (немедленный нормальный режим).
        """
        with self._lock:
            self._get(account_name).warmup_until = float(ts)

    def is_in_warmup(self, account_name: str) -> bool:
        """
        B1: True, если аккаунт сейчас в warmup-режиме (created_at + warmup_days).
        В warmup: нет кликов телефона, нет LLM-сообщений, меньше листингов.
        """
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None:
                return False
            return time.time() < entry.warmup_until

    # ──────────────────────────────────────────────────────────────────────
    # F7: random «dead days»
    # ──────────────────────────────────────────────────────────────────────

    def set_account_dead_day_rate(self, account_name: str, rate: float | None) -> None:
        """
        F7: per-account override базовой вероятности dead-day. None ⇒
        используется глобальный дефолт 0.05.
        """
        with self._lock:
            self._get(account_name).dead_day_rate = (
                None if rate is None else float(rate)
            )

    def is_dead_day(self, account_name: str) -> bool:
        """
        F7: True, если сегодня объявлен «выходным» для аккаунта.

        Реальный пользователь не работает каждый день: иногда отпуск,
        иногда забыл зайти, иногда выходной. Бот, работающий 365 дней
        без пропуска, — палевный паттерн.

        Решение принимается один раз в день (по local-date) при первом
        обращении и кэшируется до смены даты. Так все последующие вызовы
        в течение суток вернут то же решение, а run_thread сможет уверенно
        пропустить день без риска оживить аккаунт через несколько часов.

        Веса:
          - base = entry.dead_day_rate (per-account) или 0.05 (default).
          - В выходные (Sat/Sun по локальному времени) base × 3 — у
            риелторов суббота/воскресенье часто полностью пустые.
        """
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            entry = self._get(account_name)
            cached = entry.dead_day_decision
            if cached and cached.get("date") == today:
                return bool(cached.get("is_dead", False))

            base = entry.dead_day_rate if entry.dead_day_rate is not None else 0.05
            weekday = time.localtime().tm_wday  # 0=Mon … 5=Sat, 6=Sun
            rate = base * 3 if weekday in (5, 6) else base
            is_dead = random.random() < rate
            entry.dead_day_decision = {"date": today, "is_dead": is_dead}
            return is_dead

    def force_dead_day(self, account_name: str) -> None:
        """
        F7: TG /skipday — принудительно ставит сегодняшний день как dead
        для аккаунта. Полезно когда админ видит, что аккаунт нагружен/нагрелся,
        и хочет дать ему день отдыха без перезапуска.
        """
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            self._get(account_name).dead_day_decision = {"date": today, "is_dead": True}

    # ──────────────────────────────────────────────────────────────────────
    # F5b: «навсегда игнорированные» диалоги (5% от новых)
    # ──────────────────────────────────────────────────────────────────────

    def mark_dialog_ignored(self, account_name: str, dialog_id: int) -> None:
        """
        F5b: пометить диалог как «никогда не отвечаем». Решение принимается
        AvitoMessenger при первом просмотре нового диалога (5% chance).
        """
        with self._lock:
            self._get(account_name).ignored_dialogs.add(int(dialog_id))

    def is_dialog_ignored(self, account_name: str, dialog_id: int) -> bool:
        """F5b: True если данный диалог в этом процессе уже помечен как ignored."""
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None:
                return False
            return int(dialog_id) in entry.ignored_dialogs

    # ──────────────────────────────────────────────────────────────────────
    # User-resume (SMS / login captcha)
    # ──────────────────────────────────────────────────────────────────────

    def create_user_resume_request(
        self,
        account_name: str,
        kind: str,
        prompt: str,
    ) -> UserResumeRequest:
        """
        Создаёт новый pending request от потока бота. Поток далее должен
        вызвать `wait_user_resume(...)` с возвращённым request_id.

        Вне модуля типичный flow:
            req = account_state.create_user_resume_request(acc, "login_sms", "...")
            # уведомить TG-админа о req
            response = account_state.wait_user_resume(acc, req.request_id, timeout=600)
            if response == "continue":
                ...
        """
        request_id = uuid.uuid4().hex[:12]
        req = UserResumeRequest(
            request_id=request_id,
            account_name=account_name,
            kind=kind,
            prompt=prompt,
            created_at=time.time(),
        )
        with self._lock:
            entry = self._get(account_name)
            entry.pending_requests[request_id] = req
        logger.warning(
            "[%s] user-resume requested: kind=%s id=%s prompt=%r",
            account_name,
            kind,
            request_id,
            prompt,
        )
        return req

    def wait_user_resume(
        self,
        account_name: str,
        request_id: str,
        timeout: float = 600.0,
    ) -> str | None:
        """
        Блокирует текущий поток до тех пор, пока админ не нажмёт кнопку в TG
        ("continue" / "cancel") или не выйдет timeout (тогда вернёт None).

        Возвращает response (str) или None при таймауте.
        После завершения запрос удаляется из pending_requests.
        """
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None or request_id not in entry.pending_requests:
                logger.warning(
                    "[%s] wait_user_resume: unknown request_id=%s",
                    account_name,
                    request_id,
                )
                return None
            req = entry.pending_requests[request_id]

        # Блокируем без удержания lock'а.
        signaled = req.event.wait(timeout=timeout)

        with self._lock:
            entry = self._entries.get(account_name)
            if entry is not None:
                entry.pending_requests.pop(request_id, None)

        if not signaled:
            logger.warning(
                "[%s] wait_user_resume: TIMEOUT после %.0fs (request_id=%s)",
                account_name,
                timeout,
                request_id,
            )
            return None

        return req.response

    def notify_user_resumed(
        self,
        account_name: str,
        request_id: str,
        response: str = "continue",
    ) -> bool:
        """
        Вызывается из TG-обработчика, когда админ нажимает кнопку.

        Returns True, если запрос найден и был успешно «закрыт» (event set).
        False — если такого запроса нет (например, expired/таймаут раньше).
        """
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None:
                return False
            req = entry.pending_requests.get(request_id)
            if req is None:
                return False
            req.response = response
            req.answered_at = time.time()
        # set вне lock'а: безопасно и не мешает потокам, ждущим на event.
        req.event.set()
        logger.info(
            "[%s] user-resume answered: id=%s response=%s",
            account_name,
            request_id,
            response,
        )
        return True

    def list_pending_requests(self) -> list[UserResumeRequest]:
        """Все непрочитанные запросы (для диагностики / TG /status)."""
        with self._lock:
            out: list[UserResumeRequest] = []
            for entry in self._entries.values():
                for req in entry.pending_requests.values():
                    out.append(req)
            return out

    def find_request(self, request_id: str) -> UserResumeRequest | None:
        """
        Ищет pending request по request_id во всех аккаунтах.
        Используется TG callback'ом, у которого нет account_name в payload
        (callback_data ограничен 64 байтами в TG).
        """
        with self._lock:
            for entry in self._entries.values():
                req = entry.pending_requests.get(request_id)
                if req is not None:
                    return req
            return None

    # ──────────────────────────────────────────────────────────────────────
    # A2: Daily budget
    # ──────────────────────────────────────────────────────────────────────

    def set_daily_budget_limits(
        self,
        account_name: str,
        limits: dict[str, int],
    ) -> None:
        """
        A2: установить per-account override дневных лимитов.

        limits — dict с ключами "listings", "messages", "phone".
        Значение None или отсутствующий ключ → используем глобальный дефолт.

        Вызывается из bot.run_thread на старте потока.
        """
        with self._lock:
            entry = self._get(account_name)
            for action, limit in limits.items():
                if limit is None:
                    entry.daily_budget_overrides.pop(action, None)
                else:
                    try:
                        entry.daily_budget_overrides[action] = int(limit)
                    except (TypeError, ValueError):
                        logger.warning(
                            "[%s] A2: daily_budget[%s]=%r не парсится — игнорирую",
                            account_name,
                            action,
                            limit,
                        )

    def _get_limit(self, account_name: str, action: str) -> int:
        """Возвращает эффективный лимит (per-account или global default)."""
        entry = self._entries.get(account_name)
        if entry and action in entry.daily_budget_overrides:
            return entry.daily_budget_overrides[action]
        return DEFAULT_DAILY_BUDGET.get(action, 9999)

    def get_effective_limit(self, account_name: str, action: str) -> int:
        """C2: публичная обёртка над _get_limit — для /budget и health-score."""
        with self._lock:
            return self._get_limit(account_name, action)

    def check_budget_alert(
        self,
        account_name: str,
        action: str,
        used: int,
    ) -> str | None:
        """
        C2: вернуть "80" или "100" если порог впервые пересечён сегодня, иначе None.
        Де-дуплицирует: один аккаунт × одно действие × один порог → один алерт в день.

        Вызывается перед check_daily_budget в avito_client, когда значение used
        уже известно (получено из БД или in-memory).
        """
        limit = self._get_limit(account_name, action)
        if limit <= 0:
            return None
        pct = used * 100 // limit
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            entry = self._get(account_name)
            if pct >= 100:
                key = f"{action}_100"
                if entry.budget_alert_sent.get(key) != today:
                    entry.budget_alert_sent[key] = today
                    return "100"
            elif pct >= 80:
                key = f"{action}_80"
                if entry.budget_alert_sent.get(key) != today:
                    entry.budget_alert_sent[key] = today
                    return "80"
        return None

    def _get_daily_total_from_db(
        self,
        account_name: str,
        action: str,
        db_manager: Any,
    ) -> int:
        """
        Запрашивает сумму метрики за сегодня из БД.
        Используется для actions "listings" и "messages" (не "phone" — см. ниже).
        """
        metric = _BUDGET_METRIC_MAP.get(action)
        if not metric or db_manager is None:
            return 0
        today = time.strftime("%Y-%m-%d 00:00:00", time.localtime())
        try:
            rows = db_manager.get_metrics(
                since=today,
                account_name=account_name,
                metric=metric,
                group_by="metric",
            )
            return int(rows[0]["value"]) if rows else 0
        except Exception:
            logger.debug(
                "[%s] A2: не удалось получить daily total из БД для %s",
                account_name,
                action,
            )
            return 0

    def check_daily_budget(
        self,
        account_name: str,
        action: str,
        db_manager: Any = None,
    ) -> bool:
        """
        A2: True — если действие ещё в рамках дневного бюджета.
        False — лимит достигнут (нужно остановить это действие до завтра).

        action: "listings" | "messages" | "phone"

        Для "phone" использует in-memory счётчик (нет DB-запроса внутри парсера).
        Для остальных — запрашивает сумму из E2-метрик в БД.
        """
        limit = self._get_limit(account_name, action)
        if action == "phone":
            with self._lock:
                entry = self._get(account_name)
                today = time.strftime("%Y-%m-%d")
                if entry.phone_clicks_date != today:
                    entry.phone_clicks_date = today
                    entry.phone_clicks_today = 0
                return entry.phone_clicks_today < limit
        else:
            total = self._get_daily_total_from_db(account_name, action, db_manager)
            return total < limit

    def remaining_budget(
        self,
        account_name: str,
        action: str,
        db_manager: Any = None,
    ) -> int:
        """
        A2: количество оставшихся единиц бюджета сегодня (может быть 0).
        """
        limit = self._get_limit(account_name, action)
        if action == "phone":
            with self._lock:
                entry = self._get(account_name)
                today = time.strftime("%Y-%m-%d")
                if entry.phone_clicks_date != today:
                    entry.phone_clicks_date = today
                    entry.phone_clicks_today = 0
                return max(0, limit - entry.phone_clicks_today)
        else:
            total = self._get_daily_total_from_db(account_name, action, db_manager)
            return max(0, limit - total)

    # ──────────────────────────────────────────────────────────────────────
    # A3: Phone-click rate limiting
    # ──────────────────────────────────────────────────────────────────────

    def should_skip_phone(self, account_name: str) -> bool:
        """
        A3: True если "Показать телефон" нужно пропустить по лимитам.

        Проверяет:
        1. B1: warmup-режим активен → пропускаем всегда.
        2. Дневной in-memory счётчик >= лимита (hard limit).
        3. Предыдущая сессия имела >5 кликов → пропускаем всю текущую сессию.
        """
        limit = self._get_limit(account_name, "phone")
        with self._lock:
            entry = self._get(account_name)
            # B1: в warmup не кликаем телефон вообще
            if time.time() < entry.warmup_until:
                return True
            today = time.strftime("%Y-%m-%d")
            if entry.phone_clicks_date != today:
                entry.phone_clicks_date = today
                entry.phone_clicks_today = 0
            if entry.phone_clicks_today >= limit:
                return True
            if entry.prev_session_phone_clicks > 5:
                return True
            return False

    def record_phone_click(self, account_name: str) -> None:
        """
        A3: регистрирует клик "Показать телефон".
        Обновляет in-memory дневной и сессионный счётчики.
        (Метрика phone_clicks в БД инкрементируется отдельно в save_listing_to_db.)
        """
        with self._lock:
            entry = self._get(account_name)
            today = time.strftime("%Y-%m-%d")
            if entry.phone_clicks_date != today:
                entry.phone_clicks_date = today
                entry.phone_clicks_today = 0
            entry.phone_clicks_today += 1
            entry.session_phone_clicks += 1

    def start_new_session(self, account_name: str) -> None:
        """
        A4: вызывается в начале каждого нового цикла (из A4-loop в run_thread).
        Ротирует сессионные счётчики: prev ← current, current ← 0.
        """
        with self._lock:
            entry = self._get(account_name)
            entry.prev_session_phone_clicks = entry.session_phone_clicks
            entry.session_phone_clicks = 0

    def phone_clicks_today(self, account_name: str) -> int:
        """Текущий дневной счётчик кликов (для тестов и /budget в TG)."""
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None:
                return 0
            today = time.strftime("%Y-%m-%d")
            if entry.phone_clicks_date != today:
                return 0
            return entry.phone_clicks_today

    # ──────────────────────────────────────────────────────────────────────
    # C1: Health-based restrictions
    # ──────────────────────────────────────────────────────────────────────

    def apply_health_restrictions(self, account_name: str, health: dict) -> None:
        """
        C1: применить ограничения на основе health score.

        degraded/critical:
          - бюджеты × 0.5 (но не ниже 1)
          - телефон заблокирован на _HEALTH_DEGRADED_PHONE_BLOCK_DAYS дней
        healthy/warning: нет действий.
        """
        mode = health.get("mode", "healthy")
        if mode not in ("degraded", "critical"):
            return
        logger.warning(
            "[%s] C1: режим %s (captcha_rate=%.3f, капч=%d, листингов=%d за 7д) — "
            "снижаем бюджет и блокируем телефон",
            account_name,
            mode,
            health["score"],
            health["captchas_7d"],
            health["listings_7d"],
        )
        with self._lock:
            entry = self._get(account_name)
            for action in ("listings", "messages", "phone"):
                cur_limit = self._get_limit(account_name, action)
                entry.daily_budget_overrides[action] = max(
                    1, int(cur_limit * _HEALTH_DEGRADED_BUDGET_FACTOR)
                )
            phone_block_until = time.time() + _HEALTH_DEGRADED_PHONE_BLOCK_DAYS * 86400
            if entry.warmup_until < phone_block_until:
                entry.warmup_until = phone_block_until

    # ──────────────────────────────────────────────────────────────────────
    # For tests / debugging
    # ──────────────────────────────────────────────────────────────────────

    def reset_all(self) -> None:
        with self._lock:
            # Не «вешаем» уже ждущие потоки навсегда — set каждому event.
            for entry in self._entries.values():
                for req in entry.pending_requests.values():
                    req.response = "cancel"
                    req.event.set()
            self._entries.clear()


# Глобальный синглтон — используется во всех модулях.
account_state = AccountState()


# ── C1: per-account health score ──────────────────────────────────────────

# Скользящее окно — 7 дней.
_HEALTH_WINDOW_DAYS = 7

# Пороги captcha_rate (капч / листинг) для определения режима.
_HEALTH_DEGRADED_THRESHOLD = 0.05  # >5% → degraded
_HEALTH_HEALTHY_THRESHOLD = 0.01  # <1% → healthy (возможно повышение бюджета)

# При degraded — автоматически снижаем бюджет до доли от текущего.
_HEALTH_DEGRADED_BUDGET_FACTOR = 0.5

# При degraded — блокируем телефон на N дней (как warmup).
_HEALTH_DEGRADED_PHONE_BLOCK_DAYS = 3


def compute_account_health(account_name: str, db_manager: Any) -> dict:
    """
    C1: вычислить health score аккаунта за последние 7 дней.

    Возвращает dict:
        score       float  — captcha_rate (captchas / listings); 0.0 если нет данных
        mode        str    — "healthy" | "degraded" | "critical"
        captchas_7d int    — капч за 7 дней
        listings_7d int    — листингов за 7 дней
        since       str    — начало окна (YYYY-MM-DD)
    """
    since = time.strftime(
        "%Y-%m-%d 00:00:00",
        time.localtime(time.time() - _HEALTH_WINDOW_DAYS * 86400),
    )
    result = {
        "score": 0.0,
        "mode": "healthy",
        "captchas_7d": 0,
        "listings_7d": 0,
        "since": since[:10],
    }
    if db_manager is None:
        return result
    try:
        rows = db_manager.get_metrics(
            since=since,
            account_name=account_name,
            group_by="metric",
        )
        metric_map = {r["metric"]: int(r["value"]) for r in rows}
        listings = metric_map.get("listings_parsed", 0)
        captchas = metric_map.get("captcha_hits", 0)
        result["listings_7d"] = listings
        result["captchas_7d"] = captchas
        if listings > 0:
            score = captchas / listings
            result["score"] = round(score, 4)
            if score >= _HEALTH_DEGRADED_THRESHOLD:
                result["mode"] = "critical" if captchas >= 5 else "degraded"
            elif score < _HEALTH_HEALTHY_THRESHOLD:
                result["mode"] = "healthy"
            else:
                result["mode"] = "warning"
    except Exception:
        logger.debug("[%s] C1: не удалось вычислить health score", account_name)
    return result


def apply_health_restrictions(account_name: str, health: dict) -> None:
    """
    C1: тонкая обёртка над account_state.apply_health_restrictions —
    для обратной совместимости и удобного вызова из bot.run_thread.
    """
    account_state.apply_health_restrictions(account_name, health)
