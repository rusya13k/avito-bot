"""
Preflight check: проверяет что всё нужное для запуска бота настроено
и доступно, БЕЗ открытия браузера и реальных действий.

Запуск: ./.venv/Scripts/python preflight_check.py

Выводит таблицу [статус] [компонент] [деталь] и итоговый READY/NOT READY.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

OK = "[ OK ]"
WARN = "[WARN]"
FAIL = "[FAIL]"


class Result:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []
        self.fatal: list[str] = []  # блокеры запуска
        self.warnings: list[str] = []  # не блокеры, но желательно поправить

    def ok(self, name, detail=""):
        self.rows.append((OK, name, detail))

    def warn(self, name, detail):
        self.rows.append((WARN, name, detail))
        self.warnings.append(f"{name}: {detail}")

    def fail(self, name, detail):
        self.rows.append((FAIL, name, detail))
        self.fatal.append(f"{name}: {detail}")

    def print(self):
        print()
        print(f"{'=' * 78}")
        print(f"{'PREFLIGHT CHECK':^78}")
        print(f"{'=' * 78}")
        for status, name, detail in self.rows:
            print(f"  {status}  {name:<40}  {detail}")
        print(f"{'=' * 78}")
        if self.fatal:
            print(f"  {FAIL}  БЛОКЕРЫ ЗАПУСКА: {len(self.fatal)}")
            for b in self.fatal:
                print(f"     - {b}")
        if self.warnings:
            print(f"  {WARN}  ПРЕДУПРЕЖДЕНИЯ: {len(self.warnings)}")
            for w in self.warnings:
                print(f"     - {w}")
        print(f"{'=' * 78}")
        if self.fatal:
            print("  ИТОГ: НЕ ГОТОВ К ЗАПУСКУ. Исправь блокеры выше.")
            return False
        if self.warnings:
            print("  ИТОГ: ГОТОВ К ЗАПУСКУ, но есть предупреждения (см. выше).")
            return True
        print("  ИТОГ: ВСЁ ГОТОВО ДЛЯ ЗАПУСКА.")
        return True


def check_env_file(r: Result):
    env_file = ROOT / ".env"
    if not env_file.exists():
        r.fail(".env file", "отсутствует — секреты не подгрузятся")
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
        r.ok(".env file", "загружен")
    except ImportError:
        r.warn(".env file", "python-dotenv не установлен")


def check_openai(r: Result):
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    # Поддержка DEEPSEEK_API_KEY как алиаса (env_config.py мапит его на OPENAI_API_KEY).
    if not key:
        key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    base = (os.getenv("OPENAI_API_BASE") or "https://api.coda.ink/v1").strip()
    model = (os.getenv("OPENAI_MODEL") or "gpt-5.5").strip()

    if not key:
        r.fail(
            "LLM API key",
            "ни OPENAI_API_KEY, ни DEEPSEEK_API_KEY не заданы — "
            "outbound и LLM-replies НЕ будут работать",
        )
        return

    # Распознаём провайдера по prefix-у
    if key.startswith("r8_"):
        provider = "Replicate"
        if "replicate" not in base.lower():
            r.fail(
                "OpenAI key vs base",
                f"ключ Replicate (r8_), но api_base={base} — несовместимо",
            )
            return
    elif key.startswith("sk-"):
        provider = "OpenAI"
    elif key.startswith("xai-"):
        provider = "xAI"
    else:
        provider = "unknown"

    # Реальный ping к API
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=base, timeout=10)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "say OK"}],
            max_tokens=5,
        )
        out = (resp.choices[0].message.content or "").strip()
        r.ok("LLM API", f"{provider}, model={model}, ping ok: {out[:30]!r}")
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "Incorrect" in msg or "Invalid" in msg:
            r.fail(
                "LLM API",
                f"401 Auth error — ключ невалиден или истёк (provider={provider}, base={base})",
            )
        elif "404" in msg:
            r.fail(
                "LLM API",
                f"404 — model={model!r} не существует у provider={provider}",
            )
        else:
            r.fail("LLM API", f"{type(exc).__name__}: {msg[:120]}")


def check_telegram(r: Result):
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    admin = (os.getenv("TELEGRAM_ADMIN_ID") or "").strip()

    if not token:
        r.warn("Telegram bot token", "отсутствует — TG-контроль не будет работать")
        return
    if not admin or admin == "0":
        r.warn("Telegram admin_id", "не задан — никто не сможет управлять ботом")
        return

    try:
        import requests

        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        data = resp.json()
        if data.get("ok"):
            bot_info = data.get("result", {})
            r.ok(
                "Telegram bot",
                f"@{bot_info.get('username', '?')}, admin_id={admin}",
            )
        else:
            r.fail(
                "Telegram bot token",
                f"невалидный — {data.get('description', '?')}",
            )
    except Exception as exc:
        r.warn("Telegram bot", f"проверка не удалась: {type(exc).__name__}: {str(exc)[:80]}")


def check_proxies(r: Result):
    p = ROOT / "proxies.txt"
    accounts_path = ROOT / "accounts.json"
    per_account_proxies = 0
    if accounts_path.exists():
        try:
            accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
            if isinstance(accounts, list):
                per_account_proxies = sum(
                    1 for a in accounts if a.get("enabled", True) and a.get("proxy")
                )
        except (json.JSONDecodeError, OSError):
            pass

    if not p.exists():
        if per_account_proxies > 0:
            r.ok(
                "proxies.txt",
                f"отсутствует, но {per_account_proxies} аккаунтов имеют индивидуальный proxy",
            )
        else:
            r.fail(
                "proxies",
                "ни proxies.txt, ни per-account proxy не настроены",
            )
        return

    lines = [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines and per_account_proxies == 0:
        r.fail(
            "proxies",
            "proxies.txt пуст и нет per-account proxy",
        )
        return
    # Базовая sanity: должен быть формат ip:port[:user:pass]
    bad = [ln for ln in lines if ln.count(":") not in (1, 3)]
    detail = f"{len(lines)} прокси в файле"
    if per_account_proxies:
        detail += f" + {per_account_proxies} per-account"
    if bad:
        r.warn(
            "proxies",
            f"{detail}, но {len(bad)} строк в неверном формате: {bad[0][:30]}...",
        )
    else:
        r.ok("proxies", detail)


def check_accounts(r: Result):
    p = ROOT / "accounts.json"
    if not p.exists():
        r.fail("accounts.json", "отсутствует")
        return
    try:
        accounts = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        r.fail("accounts.json", f"невалидный JSON: {exc}")
        return

    if not isinstance(accounts, list) or not accounts:
        r.fail("accounts.json", "должен быть непустым массивом аккаунтов")
        return

    enabled = [a for a in accounts if a.get("enabled", True)]
    disabled = len(accounts) - len(enabled)

    issues = []
    warnings = []
    for acc in enabled:
        name = acc.get("name", "?")
        # AdsPower user_id больше не используется — проверяем chrome_profile_dir
        if not acc.get("chrome_profile_dir"):
            issues.append(f"'{name}': нет chrome_profile_dir")
        if not acc.get("user_agent"):
            warnings.append(f"'{name}': нет user_agent (будет использован дефолтный)")
        if not acc.get("phone"):
            warnings.append(f"'{name}': нет phone (B1-login не сработает)")
        cookies = acc.get("cookies_path")
        if cookies and not (ROOT / cookies).exists():
            warnings.append(f"'{name}': cookies файл не найден ({cookies})")

    detail_parts = [f"{len(enabled)} active, {disabled} disabled"]
    if issues:
        detail_parts.append("блокеры:")
        for iss in issues[:3]:
            detail_parts.append(f"      • {iss}")
        if len(issues) > 3:
            detail_parts.append(f"      • ...и ещё {len(issues) - 3}")
        r.fail("accounts.json", "\n    ".join(detail_parts))
    elif warnings:
        detail_parts.append("замечания:")
        for w in warnings[:3]:
            detail_parts.append(f"      • {w}")
        if len(warnings) > 3:
            detail_parts.append(f"      • ...и ещё {len(warnings) - 3}")
        r.warn("accounts.json", "\n    ".join(detail_parts))
    else:
        r.ok("accounts.json", " | ".join(detail_parts))


def check_smoke_imports(r: Result):
    try:
        import bot  # noqa: F401
        import outbound_messenger  # noqa: F401

        r.ok("smoke imports", "bot, outbound_messenger импортируются")
    except Exception as exc:
        r.fail("smoke imports", f"{type(exc).__name__}: {str(exc)[:120]}")


def check_db_init(r: Result):
    """Проверим что БД создаётся и outbound_contacts таблица есть."""
    try:
        from database import DatabaseManager

        db = DatabaseManager()
        # H1: проверим что метод доступен
        contacted = db.was_owner_contacted("preflight_test_dummy")
        if contacted is False:
            r.ok("database H1 schema", "outbound_contacts существует, методы работают")
        else:
            r.warn(
                "database H1 schema",
                "странное состояние БД — preflight_test_dummy уже в outbound_contacts",
            )
    except Exception as exc:
        r.fail("database init", f"{type(exc).__name__}: {str(exc)[:120]}")


def main():
    # Windows console часто стоит на cp1251 — кириллица в нашем выводе
    # тогда падает с UnicodeEncodeError. Переключаем stdout/stderr на utf-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    r = Result()

    print("Запуск preflight-чека...")
    sys.stdout.flush()

    check_env_file(r)
    check_openai(r)
    check_telegram(r)
    check_proxies(r)
    check_accounts(r)
    check_smoke_imports(r)
    check_db_init(r)

    ready = r.print()
    sys.exit(0 if ready else 1)


if __name__ == "__main__":
    main()
