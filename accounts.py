"""
G2: загрузка списка аккаунтов из отдельного файла `accounts.json`.

Раньше список аккаунтов лежал прямо в `config.json` под ключом `"accounts"`.
Это смешивало секреты (телефоны/пароли) с глобальными настройками
(LLM-ключи, AdsPower URL, и т.п.) — неудобно и небезопасно. Теперь:

- `accounts.json` (в корне репозитория) — приоритетный источник.
- Если `accounts.json` отсутствует, читаем `cfg["accounts"]` для обратной
  совместимости и логируем deprecation-warning.
- `accounts.json` находится в `.gitignore` — секреты не попадают в git.

Формат `accounts.json`:

    [
      {
        "name": "мой тест",                       // required, уникальный
        "adspower_id": "k1c2utgb",                // (alias: user_id)
        "phone": "+79991234567",                  // для login (B1)
        "password": "...",                        // для login
        "cookies_path": "accounts/мой_тест/cookies.json",
        "enabled": true,                          // false = скипнуть
        "captcha_cooldown_minutes": 60            // override глобального
      }
    ]

Минимальное обязательное поле — `name`. Если `enabled=false`, аккаунт не
запускается. Остальные поля — best-effort: бот разберётся с тем, что есть.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ACCOUNTS_JSON_FILENAME = "accounts.json"


def load_accounts(
    repo_dir: Path | str,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Возвращает список dict'ов с аккаунтами.

    Приоритет источников:
      1. `<repo_dir>/accounts.json`, если файл существует.
      2. Иначе — `cfg["accounts"]` (legacy, с deprecation warning).
      3. Иначе — пустой список.

    Все аккаунты нормализуются (см. `_normalize_account`) и аккаунты с
    `enabled=False` отфильтровываются.
    """
    repo_dir = Path(repo_dir)
    accounts_path = repo_dir / ACCOUNTS_JSON_FILENAME

    if accounts_path.exists():
        raw = _load_json_list(accounts_path)
        return _normalize_and_filter(raw, source=str(accounts_path))

    if cfg and isinstance(cfg.get("accounts"), list):
        logger.warning(
            "G2: список аккаунтов читается из config.json (устаревший формат). "
            'Перенесите блок "accounts" в %s — это безопаснее (секреты не '
            "попадут в git) и удобнее для per-account настроек.",
            accounts_path,
        )
        return _normalize_and_filter(cfg["accounts"], source="config.json")

    logger.warning(
        'G2: ни %s, ни config.json["accounts"] не содержат списка аккаунтов',
        accounts_path,
    )
    return []


def _load_json_list(path: Path) -> list[Any]:
    """Читает JSON-файл; возвращает пустой список при любых проблемах
    (с логом ERROR — чтобы инцидент был виден, но бот мог стартовать)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("G2: не могу прочитать %s: %s", path, exc)
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("G2: %s — невалидный JSON: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.error(
            "G2: %s должен содержать JSON-список аккаунтов, получено %s",
            path,
            type(data).__name__,
        )
        return []
    return data


def _normalize_and_filter(
    raw: list[Any],
    source: str,
) -> list[dict[str, Any]]:
    """
    Нормализует и фильтрует список аккаунтов:
      - non-dict / без `name` — пропускаем с warning.
      - `enabled=False` — пропускаем с info.
      - alias `adspower_id` → `user_id` (для совместимости со старым
        кодом, который читает `acc.get("user_id")`).
      - проставляем `enabled=True` по умолчанию (полезно для UI/логов).
    """
    out: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw):
        normalized = _normalize_account(item, index=i, source=source)
        if normalized is None:
            continue
        name = normalized["name"]
        if name in seen_names:
            logger.warning(
                "G2: %s: дубль имени аккаунта %r — оставляю первое вхождение",
                source,
                name,
            )
            continue
        seen_names.add(name)
        out.append(normalized)
    return out


def _normalize_account(
    item: Any,
    *,
    index: int,
    source: str,
) -> dict[str, Any] | None:
    """
    Возвращает нормализованный dict или None, если запись надо отбросить.
    """
    if not isinstance(item, dict):
        logger.warning(
            "G2: %s: account #%d не dict (%s) — пропускаю",
            source,
            index,
            type(item).__name__,
        )
        return None

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning(
            "G2: %s: account #%d без 'name' — пропускаю",
            source,
            index,
        )
        return None

    # Делаем копию — не мутируем исходный конфиг (важно если cfg повторно читается).
    out = dict(item)

    # Alias: новый формат — adspower_id, старый — user_id. Поддерживаем оба.
    if out.get("adspower_id") and not out.get("user_id"):
        out["user_id"] = out["adspower_id"]
    elif out.get("user_id") and not out.get("adspower_id"):
        out["adspower_id"] = out["user_id"]

    # enabled: по умолчанию True. Если явно False — вернём None (отфильтровать).
    enabled = out.get("enabled", True)
    if enabled is False:
        logger.info(
            "G2: %s: аккаунт %r отключён (enabled=false) — пропускаю",
            source,
            name,
        )
        return None
    out["enabled"] = True

    return out


def get_account_overrides(account: dict[str, Any], key: str, default: Any = None) -> Any:
    """
    G2: достаёт per-account override для известного параметра. Если поля нет —
    возвращает default. Используется в bot.py, например, для подбора своего
    captcha_cooldown_minutes на конкретный аккаунт.
    """
    return account.get(key, default)
