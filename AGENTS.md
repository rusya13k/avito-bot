# AGENTS.md

Project conventions and verification commands for AI-coding agents (and humans).

## Verification commands

Run these before opening a PR. They should all pass.

```bash
# Lint (ruff: pycodestyle, pyflakes, isort, pyupgrade, ERA)
python -m ruff check .

# Auto-format (ruff format — black-compatible)
python -m ruff format .

# Unit tests
python -m pytest tests/ -v

# Smoke-import (catches broken module-level code)
python -c "import bot, tg_bot, database, commercial_parser, avito_messenger, avito_client, llm_classifier, listing_classifier, heuristic_scorer, logging_setup, env_config, accounts; print('OK')"
```

A successful baseline: `187 tests pass, ruff returns "All checks passed!"`.

## Project conventions

### Logging

- Use `logging.getLogger(__name__)` at module top. Do NOT print to stdout/stderr
  in business logic.
- For per-account logs, prefer `logging_setup.get_account_logger(name, account_id)` —
  it adds `account_id` to the LogRecord, which is then surfaced by both the
  human formatter (`[acc1]`) and the JSON formatter (`"account_id": "acc1"`).
- Critical errors: `logger.exception(...)` so the trace gets pushed to the
  TG-alert handler (E4).

### Database

- All multi-step writes that must be atomic should go inside
  `db.transaction()` and pass `cursor=cur` to each method
  (`upsert_listing`, `upsert_phone`, `upsert_dialog`, `add_message`,
  `mark_listing_parse_status`, ...).
- Single ops can call methods directly (each opens its own short transaction).
- Never bypass `DatabaseManager` to talk to SQLite directly.

### Configuration

- Secrets (API keys, tokens) — through `.env` (or real environment).
  ENV overrides `config.json` (see `env_config.py:apply_env_overrides`).
- Anything user-tunable that isn't a secret — `config.json` or
  `classification_config.py` (heuristic weights live there).

### LLM prompts

- Templates are plain text in `prompts/` and loaded via
  `llm_classifier._load_prompt(name)`.
- Use `.format(...)` placeholders (never f-strings or string concatenation
  outside `_load_prompt`).
- Input/output are logged at DEBUG level — enable with `LOG_LEVEL=DEBUG`
  to capture for prompt tuning.

### Selenium

- Always use `WebDriverWait` for elements that may take time to render
  (post-click, post-navigation). Direct `find_element` is OK only inside
  already-loaded containers.
- Bare `except:` is allowed in selenium-flow because `WebDriverException`
  has many unpredictable subclasses; `E722` is in the global ignore list.

### Tests

- `pytest`. Fixtures in `tests/conftest.py`.
- Use the `db` fixture for an isolated temp SQLite per test.
- Mock `LLMClassifier.classify_listing` / `generate_response` — never call
  real OpenAI in tests.

### Style

- `line-length = 100`, double quotes (ruff format).
- Type hints encouraged but not enforced (`mypy --strict` only for the
  modules listed in F3 — not yet configured).
- New comments in Russian are OK (matches existing codebase).

## Sprint status

Tracked in `zadachi.txt`. As of latest:
- **All sprints done**: Sprints 1-5, E2 (metrics + search_filters), G1, G2,
  Tier1 (A1-A4: proxy, budget, phone-limit, session-pauses),
  Tier2 (B1-B4: warmup, active-hours, thread-rng, long-cooldown),
  Tier3 (C1-C3: health-score, budget-alerts, captcha-log).
- **TG commands**: `/report`, `/budget`, `/lastcaptcha`, `/health`, `/warmup`.
- **Not recommended now**: G3/D1 (task queue — only for 10+ accounts),
  E1 (G1 Phase 2 — defer until major selector changes).

### E2 — Metrics

Per account / per hour счётчики живут в таблице `metrics` (см.
`database_schema.md`). API:

- `db.incr_metric(account_name, metric, by=1, ts=None, cursor=None)` —
  атомарный UPSERT в часовой бакет. Поддерживает `cursor=...` для участия
  в общей транзакции (см. C4) — например, в `commercial_parser.save_listing_to_db`
  парсинг + метрика коммитятся вместе.
- `db.get_metric_value(account_name, metric, ts=None)` — точечное чтение.
- `db.get_metrics(since, until, account_name, metric, group_by=...)` —
  с агрегацией по `hour` / `day` / `metric`.
- `db.get_daily_summary(since)` подмешивает в итог `llm_errors`,
  `captcha_hits`, `dialogs_handled`, `messages_sent` из metrics.

Где инкрементится:
- `commercial_parser.save_listing_to_db` — `listings_parsed` + `listings_<status>`
  (+ `captcha_hits` если status='captcha'); ошибка → `listings_error`.
- `llm_classifier.LLMClassifier` — `llm_errors` (account_name=''; шарится
  между потоками) на каждом fallback'е.
- `avito_messenger.AvitoMessenger` — `dialogs_handled` после каждого
  обработанного диалога; `messages_sent` после успешной отправки (в той же
  транзакции, что и `add_message`/`upsert_dialog`).

Команда `/report` в TG-боте автоматически выводит новые счётчики.

### G1 — AvitoClient (Selenium-фасад)

Весь Selenium-флоу собран за единым фасадом `avito_client.AvitoClient`.
До G1 логика была размазана по `bot.py` (login + browse), `commercial_parser.py`
(parsing) и `avito_messenger.py` (messenger). Каждое изменение селекторов /
поведения Avito правилось в трёх местах.

Использование (см. `bot.run_thread`):
```python
client = AvitoClient(
    driver, wait, account_name,
    log_func=log, db_manager=db, llm_classifier=llm,
)
client.warmup_yandex()
client.login(cookies_path=..., phone=..., password=...)  # composite 3-уровневый
client.browse_commercial_categories()
processed, new, errors = client.find_and_view_commercial_listings()
client.process_messages()
```

Архитектурные правила:
- **Новый код** — пишется ПРОТИВ AvitoClient, не против исходных модулей.
  Это даёт стабильный публичный API.
- **Реализации** — пока что в исходных модулях. AvitoClient — тонкая
  обёртка с lazy-импортами (избегаем циклических загрузок). Когда придёт
  время рефакторить реализацию (например, при больших правках селекторов),
  правишь её внутри метода — внешние вызывающие не страдают.
- **Что НЕ входит в AvitoClient**: AdsPower (start/stop профиля), прокси,
  жизненный цикл потока, db connection. Это уровень выше — `bot.run_thread`.

Тесты — `tests/test_avito_client.py` (mock-driver, без реального Selenium):
проверяем делегирование + 3-уровневый login flow.

### G2 — Per-account config

Список аккаунтов теперь живёт в отдельном `accounts.json` (в корне репо;
в `.gitignore`). Загрузка — через `accounts.load_accounts(repo_dir, cfg)`:

- Приоритет 1 — `accounts.json` (рекомендуемый формат).
- Приоритет 2 — `cfg["accounts"]` из `config.json` (legacy + deprecation
  warning). Поддерживается, чтобы старые установки не ломались.
- Disabled-аккаунты (`enabled: false`) автоматически фильтруются.
- Алиасы `adspower_id ↔ user_id` нормализуются в обе стороны.

Per-account override: `captcha_cooldown_minutes` теперь можно задать
индивидуально на аккаунт. Применяется при старте потока через
`account_state.set_account_cooldown_minutes(name, minutes)`. Глобальный
default из `config.json` остаётся валидным для аккаунтов без override.

Для добавления нового per-account параметра:
1. Описать поле в README/`accounts.example.json`.
2. Достать через `account.get("my_param")` в нужном месте кода.
3. Если параметр глобально хранится в state — добавить setter (как
   `set_account_cooldown_minutes`) и вызвать его из `bot.run_thread`.

## Common gotchas

- AdsPower must be running before bot.py — connection refused otherwise.
- `OPENAI_API_KEY` starting with `r8_` is Replicate, not OpenAI; the bot
  will fall back to heuristic-only classification.
- `cookies.json` per-account is in `accounts/<name>/` and is in `.gitignore`.
- `*.db` files (SQLite) are in `.gitignore` — don't commit local data.
