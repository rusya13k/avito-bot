"""
In-memory state per account.

Сейчас отвечает только за CAPTCHA-cooldown (A3): когда аккаунт попал на
капчу, мы помечаем его как "cooled_down" на N минут и не даём ему ничего
делать всё это время. State не персистится между перезапусками бота —
для cooldown'а в 15-30 минут это нормально.

Thread-safe: операции защищены lock'ом, т.к. бот многопоточный
(по потоку на аккаунт).

Использование:
    from account_state import account_state

    if account_state.is_cooled_down("acc1"):
        log("acc1", f"в cooldown, осталось {account_state.cooldown_remaining_seconds('acc1')}s")
        skip_account()

    # в commercial_parser, после детекта капчи:
    account_state.mark_captcha("acc1")
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Дефолтная длительность cooldown'а после капчи. Можно переопределить через
# config.json (ключ "captcha_cooldown_minutes") — bot.py вызывает
# configure_from_cfg(cfg) на старте.
DEFAULT_CAPTCHA_COOLDOWN_MINUTES = 30


def configure_from_cfg(cfg: dict) -> None:
    """
    Подхватывает настройки account_state из config.json:
        captcha_cooldown_minutes (int|float) — длительность cooldown'а после
            детекта капчи. Если ключа нет — оставляем дефолт (30).
    """
    global DEFAULT_CAPTCHA_COOLDOWN_MINUTES
    raw = cfg.get("captcha_cooldown_minutes")
    if raw is None:
        return
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "captcha_cooldown_minutes=%r в config.json не парсится — оставляю %d",
            raw,
            DEFAULT_CAPTCHA_COOLDOWN_MINUTES,
        )
        return
    if value <= 0:
        logger.warning(
            "captcha_cooldown_minutes=%r <= 0 — оставляю %d",
            raw,
            DEFAULT_CAPTCHA_COOLDOWN_MINUTES,
        )
        return
    DEFAULT_CAPTCHA_COOLDOWN_MINUTES = value
    logger.info("captcha_cooldown_minutes = %s min", value)


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
    # B1: pending user-resume запросы (один аккаунт может иметь несколько,
    # но обычно последний — актуальный).
    pending_requests: dict[str, UserResumeRequest] = field(default_factory=dict)
    # G2: per-account override длительности cooldown'а (минуты). None ⇒
    # используем глобальный DEFAULT_CAPTCHA_COOLDOWN_MINUTES.
    cooldown_minutes_override: float | None = None


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

        cooldown_minutes:
          - явное значение -> используем его;
          - None (по умолчанию) -> per-account override (set_account_cooldown_minutes),
            иначе глобальный DEFAULT_CAPTCHA_COOLDOWN_MINUTES.

        Возвращает unix-time момента окончания cooldown'а.
        """
        now = time.time()
        with self._lock:
            entry = self._get(account_name)
            effective_minutes = (
                cooldown_minutes
                if cooldown_minutes is not None
                else (
                    entry.cooldown_minutes_override
                    if entry.cooldown_minutes_override is not None
                    else DEFAULT_CAPTCHA_COOLDOWN_MINUTES
                )
            )
            new_until = now + effective_minutes * 60.0
            entry.cooldown_until = max(entry.cooldown_until, new_until)
            entry.captcha_hits += 1
            entry.last_captcha_at = now
            logger.warning(
                "[%s] CAPTCHA hit #%d -> cooldown until %s (~%d min)",
                account_name,
                entry.captcha_hits,
                time.strftime("%H:%M:%S", time.localtime(entry.cooldown_until)),
                effective_minutes,
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

    def last_captcha_at(self, account_name: str) -> float | None:
        with self._lock:
            entry = self._entries.get(account_name)
            if entry is None or entry.last_captcha_at == 0.0:
                return None
            return entry.last_captcha_at

    # ──────────────────────────────────────────────────────────────────────
    # B1: User-resume (SMS / login captcha)
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
