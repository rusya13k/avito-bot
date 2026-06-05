"""
H3: загрузка секретов из .env / переменных окружения.

Зачем:
- Раньше OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, ADSPOWER_API_KEY и логин/пароль
  аккаунтов лежали в config.json в открытом виде. Если файл попадает в git
  или шарится — это утечка.
- Теперь: переменные с высоким приоритетом читаются из .env (или из
  настоящего окружения процесса), config.json остаётся как
  fallback / source of structure (списки аккаунтов, флаги и т.п.).

Не используем python-dotenv, чтобы не плодить зависимостей.
Формат поддерживаемого .env — простой:
    KEY=value
    # комментарий
    KEY="value with spaces"
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Маппинг env-переменная → (json-ключ верхнего уровня).
# Только секреты + базовая инфра. Списки аккаунтов в env не льём.
ENV_TO_CFG = {
    "DEEPSEEK_API_KEY": "openai_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "OPENAI_API_BASE": "openai_api_base",
    "OPENAI_MODEL": "openai_model",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_ADMIN_ID": "telegram_admin_id",
    "ADSPOWER_API_URL": "adspower_api_url",
    "ADSPOWER_API_KEY": "adspower_api_key",
}


def load_dotenv_if_present(path: str | Path = ".env") -> int:
    """
    Минимальный .env-парсер. Возвращает количество подхваченных переменных
    (полезно для лога). Существующие переменные окружения НЕ перезаписываем
    — приоритет всегда у настоящего env.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    if count:
        logger.info(".env: подхвачено %d переменных", count)
    return count


def apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Поверх dict, прочитанного из config.json, накладывает значения из
    os.environ (по таблице ENV_TO_CFG). Возвращает тот же dict (in-place).

    telegram_admin_id парсится в int.
    """
    for env_key, cfg_key in ENV_TO_CFG.items():
        env_val = os.environ.get(env_key)
        if env_val is None or env_val == "":
            continue
        if cfg_key == "telegram_admin_id":
            try:
                cfg[cfg_key] = int(env_val)
            except ValueError:
                logger.warning("TELEGRAM_ADMIN_ID не int: %r — игнорирую", env_val)
                continue
        else:
            cfg[cfg_key] = env_val
        logger.debug("env override: %s -> %s (%d chars)", env_key, cfg_key, len(env_val))
    return cfg


def mask_secret(value: str | None, keep: int = 4) -> str:
    """
    Маскирует секрет для логов: 'sk-very-secret' -> 'sk-v…cret'.
    None/пустой -> '<unset>'.
    """
    if not value:
        return "<unset>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "…" + value[-keep:]
