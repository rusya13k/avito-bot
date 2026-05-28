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
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

ACCOUNTS_JSON_FILENAME = "accounts.json"

# K1: сериализуем запись в accounts.json.
# pyTelegramBotAPI обрабатывает callback'и в ThreadPool, так что два TG-handler'а
# могут одновременно дёрнуть add_account/remove_account. Без локa один из них
# затрёт изменения другого (классический lost-update).
_WRITE_LOCK = threading.Lock()


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="Уникальное имя аккаунта")
    enabled: bool = True
    adspower_id: str | None = None
    user_id: str | None = None
    phone: str | None = None
    password: str | None = None
    cookies_path: str | None = None
    persona: str | None = None
    captcha_cooldown_minutes: int | None = None

    @field_validator("name")
    def name_must_not_be_empty(cls, v):
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("name cannot be empty or just whitespace")
        return v_stripped


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
    Теперь использует Pydantic для строгой валидации.
    """
    if not isinstance(item, dict):
        logger.warning(
            "G2: %s: account #%d не dict (%s) — пропускаю",
            source,
            index,
            type(item).__name__,
        )
        return None

    try:
        validated = AccountConfig(**item)
    except ValidationError as exc:
        logger.warning(
            "G2: %s: account #%d содержит ошибки валидации и будет пропущен:\n%s",
            source,
            index,
            exc,
        )
        return None

    # enabled: по умолчанию True. Если явно False — вернём None (отфильтровать).
    if not validated.enabled:
        logger.info(
            "G2: %s: аккаунт %r отключён (enabled=false) — пропускаю",
            source,
            validated.name,
        )
        return None

    out = validated.model_dump()

    # Alias: новый формат — adspower_id, старый — user_id. Поддерживаем оба
    # и гарантируем наличие обоих для совместимости.
    if out.get("adspower_id") and not out.get("user_id"):
        out["user_id"] = out["adspower_id"]
    elif out.get("user_id") and not out.get("adspower_id"):
        out["adspower_id"] = out["user_id"]

    return out


def get_account_overrides(account: dict[str, Any], key: str, default: Any = None) -> Any:
    """
    G2: достаёт per-account override для известного параметра. Если поля нет —
    возвращает default. Используется в bot.py, например, для подбора своего
    captcha_cooldown_minutes на конкретный аккаунт.
    """
    return account.get(key, default)


# ──────────────────────────────────────────────────────────────────────
# K1: CRUD операции для TG-бота (запись в accounts.json)
# ──────────────────────────────────────────────────────────────────────


def load_all_accounts(
    repo_dir: Path | str,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    K1: как load_accounts, но БЕЗ фильтра disabled-аккаунтов.

    TG-бот рендерит полный список (✅ enabled / 💤 disabled), чтобы можно было
    редактировать аккаунты, временно отключённые через `enabled=false`.
    bot.py при старте использует обычный `load_accounts`, который такие
    аккаунты сам отфильтрует.

    Дубли по имени по-прежнему отбрасываются (первое вхождение остаётся).
    """
    repo_dir = Path(repo_dir)
    accounts_path = repo_dir / ACCOUNTS_JSON_FILENAME

    if accounts_path.exists():
        raw = _load_json_list(accounts_path)
        return _normalize_only(raw, source=str(accounts_path))

    if cfg and isinstance(cfg.get("accounts"), list):
        return _normalize_only(cfg["accounts"], source="config.json")

    return []


def _normalize_only(raw: list[Any], source: str) -> list[dict[str, Any]]:
    """
    Как _normalize_and_filter, но НЕ выбрасывает disabled.
    Сохраняет фактическое значение `enabled` (True/False), не насильно True.
    Теперь использует Pydantic.
    """
    out: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            logger.warning(
                "K1: %s: account #%d не dict (%s) — пропускаю", source, i, type(item).__name__
            )
            continue

        try:
            validated = AccountConfig(**item)
        except ValidationError as exc:
            logger.warning(
                "K1: %s: account #%d содержит ошибки валидации и будет пропущен:\n%s",
                source,
                i,
                exc,
            )
            continue

        name = validated.name
        if name in seen_names:
            logger.warning("K1: %s: дубль имени %r — оставляю первое вхождение", source, name)
            continue
        seen_names.add(name)

        normalized = validated.model_dump()
        if normalized.get("adspower_id") and not normalized.get("user_id"):
            normalized["user_id"] = normalized["adspower_id"]
        elif normalized.get("user_id") and not normalized.get("adspower_id"):
            normalized["adspower_id"] = normalized["user_id"]
        out.append(normalized)
    return out


def save_accounts(repo_dir: Path | str, accounts: list[dict[str, Any]]) -> Path:
    """
    K1: атомарно записывает список аккаунтов в `accounts.json`.

    Атомарность — через tempfile в той же директории + os.replace
    (стандартная практика, чтобы не получить пустой/недоразрешённый JSON
    при сбое процесса посередине записи).

    Returns: путь к accounts.json (для логов).
    """
    repo_dir = Path(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)
    target = repo_dir / ACCOUNTS_JSON_FILENAME

    # Сначала валидация: каждый элемент — dict с непустым name.
    for i, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            raise TypeError(f"save_accounts: item #{i} не dict ({type(acc).__name__})")
        if not isinstance(acc.get("name"), str) or not acc["name"].strip():
            raise ValueError(f"save_accounts: item #{i} без валидного 'name'")

    payload = json.dumps(accounts, ensure_ascii=False, indent=2)
    with _WRITE_LOCK:
        # tempfile в той же директории, чтобы os.replace был атомарным
        # (cross-device replace не атомарен на некоторых FS).
        fd, tmp_path = tempfile.mkstemp(prefix=".accounts_", suffix=".json.tmp", dir=str(repo_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, target)
        except Exception:
            # Подчищаем temp при крахе записи (на os.replace его уже нет).
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return target


def add_account(
    repo_dir: Path | str,
    account: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    K1: добавить аккаунт в accounts.json.

    Raises:
        ValueError, если имя пустое или такой `name` уже есть.
    """
    repo_dir = Path(repo_dir)
    name = (account or {}).get("name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("add_account: 'name' обязателен и не может быть пустым")

    with _WRITE_LOCK:
        # Внутри лока: re-read, mutate, write — чтобы не потерять параллельные
        # изменения. _WRITE_LOCK реентрантным НЕ является, поэтому save_accounts
        # тоже не должен брать его — он берёт свой; но мы пишем БЕЗ помощи
        # save_accounts, чтобы избежать двойного захвата.
        all_accs = load_all_accounts(repo_dir, cfg)
        if any(a["name"] == name for a in all_accs):
            raise ValueError(f"add_account: аккаунт {name!r} уже существует")
        all_accs.append(dict(account))
        _atomic_write(repo_dir, all_accs)
    return all_accs


def update_account(
    repo_dir: Path | str,
    name: str,
    updates: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    K1: применить partial update к аккаунту по имени.

    Returns:
        Обновлённый dict аккаунта или None, если такого `name` нет.
    """
    repo_dir = Path(repo_dir)
    with _WRITE_LOCK:
        all_accs = load_all_accounts(repo_dir, cfg)
        for acc in all_accs:
            if acc["name"] == name:
                acc.update(updates)
                _atomic_write(repo_dir, all_accs)
                return acc
    return None


def remove_account(
    repo_dir: Path | str,
    name: str,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """
    K1: удалить аккаунт по имени.

    L9: при удалении заодно чистим связанный cookies-файл
    (`acc["cookies_path"]`, если задан и существует). Иначе при создании
    одноимённого аккаунта в будущем мы бы автоматически подсосали старые
    куки — путаница при дебаге и потенциальная утечка авторизации.

    Файл удаляем ВНЕ лока на accounts.json: основная запись (удаление
    из списка) уже зафиксирована, ошибка очистки куки ничего критичного
    не ломает (только лог).

    Returns:
        True, если был удалён; False, если такого имени не было.
    """
    repo_dir = Path(repo_dir)
    with _WRITE_LOCK:
        all_accs = load_all_accounts(repo_dir, cfg)
        target = next((a for a in all_accs if a["name"] == name), None)
        if target is None:
            return False
        new_accs = [a for a in all_accs if a["name"] != name]
        _atomic_write(repo_dir, new_accs)

    # L9: cleanup cookies-файла за пределами лока.
    cookies_rel = target.get("cookies_path")
    if isinstance(cookies_rel, str) and cookies_rel.strip():
        cookies_abs = repo_dir / cookies_rel
        try:
            if cookies_abs.is_file():
                cookies_abs.unlink()
                logger.info("L9: удалён cookies-файл %s", cookies_abs)
        except OSError as exc:
            logger.warning("L9: не удалось удалить cookies-файл %s: %s", cookies_abs, exc)

    return True


def _atomic_write(repo_dir: Path, accounts: list[dict[str, Any]]) -> Path:
    """
    Внутренний хелпер: атомарная запись БЕЗ повторного взятия _WRITE_LOCK
    (вызывается из add/update/remove, которые уже его держат).

    Логически дублирует save_accounts — но save_accounts берёт лок сам,
    что приводило бы к deadlock при вызове из add/update/remove.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    target = repo_dir / ACCOUNTS_JSON_FILENAME
    payload = json.dumps(accounts, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(prefix=".accounts_", suffix=".json.tmp", dir=str(repo_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target
