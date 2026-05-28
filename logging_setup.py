"""
E1: единая точка настройки логирования + LoggerAdapter с account_id.
E4: опциональный TG-хендлер для алертов на ERROR/CRITICAL.

Раньше:
- bot.log() писал через print() в один поток с локом.
- Параллельно классификаторы и account_state писали через logging.getLogger.
- Не было ни общего формата, ни общего уровня, ни account_id в каждом
  сообщении.

Теперь:
- setup_logging() вызывается один раз на старте приложения. Всё пишется
  в stderr (human или JSON формат — управляется LOG_FORMAT env-var).
- get_account_logger(name, account_id) возвращает LoggerAdapter, который
  автоматически добавляет account_id в каждый record.
- install_tg_alert_handler(send_func) подключает TG-хендлер: любой ERROR
  или CRITICAL автоматически уходит в TG.
- install_tg_buffer_handler(buffer_func) — для совместимости со старой
  лентой логов в TG-боте (раньше pgshell-style buffer заполнялся через
  add_log из bot.log()).
"""

import json
import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable

# ──────────────────────────────────────────────────────────────────────
# Formatters
# ──────────────────────────────────────────────────────────────────────


class HumanFormatter(logging.Formatter):
    """[HH:MM:SS] L [account] logger.name: message"""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        lvl = record.levelname[0]  # I / W / E / C / D
        account = getattr(record, "account_id", None) or "-"
        msg = record.getMessage()
        line = f"[{ts}] {lvl} [{account}] {record.name}: {msg}"
        if record.exc_info:
            line += "\n" + "".join(traceback.format_exception(*record.exc_info))
        return line


class JsonFormatter(logging.Formatter):
    """Один JSON-объект на строку — удобно для prod / парсинга."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("account_id", "trace_id"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────
# LoggerAdapter
# ──────────────────────────────────────────────────────────────────────


class AccountAdapter(logging.LoggerAdapter):
    """Прицепляет account_id к каждой записи (через extra)."""

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("account_id", self.extra.get("account_id"))
        return msg, kwargs


# ──────────────────────────────────────────────────────────────────────
# Optional handlers
# ──────────────────────────────────────────────────────────────────────


class TGBufferHandler(logging.Handler):
    """
    Дублирует human-friendly строку в кольцевой буфер TG-бота
    (раньше это делал bot.log() через _tg.add_log).
    """

    def __init__(self, push_func: Callable[[str, str | None], None]):
        super().__init__(level=logging.INFO)
        self._push = push_func
        self.setFormatter(HumanFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            account_id = getattr(record, "account_id", None)
            self._push(self.format(record), account_id)
        except Exception:
            self.handleError(record)


class TGAlertHandler(logging.Handler):
    """
    E4: ERROR/CRITICAL — мгновенно в TG. send_func принимает text.
    Внутри ловит свои собственные исключения, чтобы не уйти в рекурсию
    на собственный logger.
    """

    def __init__(self, send_func: Callable[[str], None]):
        super().__init__(level=logging.ERROR)
        self._send = send_func
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.level:
            return
        try:
            account = getattr(record, "account_id", None) or "-"
            text = f"[{record.levelname}] {record.name} [{account}]\n{record.getMessage()}"
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))
                # последние ~12 строк трейса достаточно
                tb_short = "\n".join(tb.splitlines()[-12:])
                text += "\n<pre>\n" + tb_short + "\n</pre>"
            # лимит TG сообщения 4096; режем с запасом под HTML
            if len(text) > 3800:
                text = text[:3800] + "\n…(truncated)"
            with self._lock:
                self._send(text)
        except Exception:
            # не уходим в рекурсию: handleError печатает в stderr
            self.handleError(record)


# ──────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────


_INIT_FLAG = "_devin_logging_initialized"
_TG_BUF_HANDLER: TGBufferHandler | None = None
_TG_ALERT_HANDLER: TGAlertHandler | None = None


def setup_logging(level: str | None = None, json_format: bool | None = None) -> None:
    """
    Идемпотентная настройка root logger. Безопасно вызывать несколько раз.

    Параметры можно задать через env:
        LOG_LEVEL=INFO|DEBUG|WARNING|ERROR
        LOG_FORMAT=human|json
    """
    root = logging.getLogger()
    if getattr(root, _INIT_FLAG, False):
        return

    lvl = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    fmt = (
        ("json" if json_format else "human")
        if json_format is not None
        else (os.getenv("LOG_FORMAT") or "human").lower()
    )

    root.setLevel(getattr(logging, lvl, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonFormatter() if fmt == "json" else HumanFormatter())
    root.addHandler(stream)

    # Подавим шум сторонних библиотек.
    for noisy in ("urllib3", "selenium", "httpcore", "httpx", "openai", "TeleBot", "WDM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    setattr(root, _INIT_FLAG, True)


def get_account_logger(name: str, account_id: str | None) -> AccountAdapter:
    """LoggerAdapter с account_id. Используется в потоках бота/мессенджера."""
    return AccountAdapter(
        logging.getLogger(name),
        {"account_id": account_id},
    )


def install_tg_buffer_handler(push_func: Callable[[str, str | None], None]) -> None:
    """
    Подключить кольцевой TG-буфер логов (раньше это делал bot.log() напрямую).
    Идемпотентно: повторный вызов перенастраивает push-функцию.
    """
    global _TG_BUF_HANDLER
    root = logging.getLogger()
    if _TG_BUF_HANDLER is None:
        _TG_BUF_HANDLER = TGBufferHandler(push_func)
        root.addHandler(_TG_BUF_HANDLER)
    else:
        _TG_BUF_HANDLER._push = push_func


def install_tg_alert_handler(send_func: Callable[[str], None]) -> None:
    """
    E4: подключить алерт-хендлер. send_func — функция, отправляющая
    текст администратору TG-бота.
    """
    global _TG_ALERT_HANDLER
    root = logging.getLogger()
    if _TG_ALERT_HANDLER is None:
        _TG_ALERT_HANDLER = TGAlertHandler(send_func)
        root.addHandler(_TG_ALERT_HANDLER)
    else:
        _TG_ALERT_HANDLER._send = send_func
