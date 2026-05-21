# HANDOFF — Avito-bot project

Шорткат-контекст для продолжения работы в новом диалоге.

## Проект

Многопоточный бот парсинга коммерческой недвижимости на Avito.

- **CWD**: `C:\dev\new bot`
- **Stack**: Python 3.12 + Selenium + Telegram-bot + SQLite + LLM (OpenAI)
- **AdsPower**: должен быть запущен (антидетект-браузер per account)
- **Главные модули**: `bot.py` (1919, после S1: `run_thread` 70 строк + 7 helpers), `tg_bot.py` (1487, после S2 full: `_setup` 30 строк + 25+ `_cmd_*`/`_dialog_*`/`_cb_*` методов + dispatch tables), `database.py`, `account_state.py`, `commercial_parser.py` (602, после S3: extract_listing_data 35 + save_listing_to_db 40 строк + 14 helpers), `avito_messenger.py`, `avito_client.py` (G1-фасад над selenium-flow), `accounts.py` (K1-CRUD), `llm_sanitizer.py` (K2)

## Состояние (на момент хэндоффа)

- **git log** (последний → старше):
  - `f0d835d` — **H1 OUTBOUND**: бот пишет собственникам первым. +outbound_messenger.py +16 personas +DB dedup. **Это главная новая фича**.
  - `eca3d2a` — S2 full: _setup() декомпозиция (932 → 30 строк), +25+ методов, +11 тестов
  - `c829cc4` — S2 partial: _edit_or_send helper, 11 try-edit-except-send → 1 (+4 теста)
  - `2ef06d7` — S3 декомпозиция commercial_parser (213+154 → ~35+~40 строк, +14 helpers + 21 тест)
  - `b056d47` — S1 декомпозиция run_thread (363 → 70 строк, +7 helpers + 11 тестов)
  - `a4317eb` — F6 probabilistic active hours (вероятностное окно вместо бинарного, x1.3 lifetime)
  - `ab2e7a5` — F10+F11+F12 batch (scroll-rng, dialogs-rng, browse budget guard, ~x1.3 совокупно)
  - `ae2f0ea` — F7 random «dead days» (5% дней пропускаем целиком, ×3 в выходные)
  - `ced5000` — F9 realistic dwell times (lognormal + interest score)
  - `4bc3713` — F5 messenger reply delays (lognormal, 5% ignore новых)
  - `90fa3f3` — F8 idle cycles (4 типа cycle, per-account override, warmup-mode)
  - `00c8255` — Quick-wins L9-L12 (cookies cleanup, narrow exceptions, AdsPower.update_proxy)
  - `13eda14` — Quick-wins L1-L8 (logging, exception narrowing, cfg-cache)
  - `61e49a6` — Tier1-3 + F1 + K1-K3 (большой коммит, ~2787 строк)
  - `63eacd6` — secrets → .env
  - `f3375ae` — initial snapshot
- **working tree**: чисто
- **ruff**: All checks passed!
- **pytest**: **420 passed in ~7s**
- **smoke-import**: OK (`bot, tg_bot, database, commercial_parser, avito_messenger, avito_client, llm_classifier, listing_classifier, heuristic_scorer, logging_setup, env_config, accounts, llm_sanitizer`)

## Verify commands (см. AGENTS.md)

```bash
./.venv/Scripts/python -m ruff check .
./.venv/Scripts/python -m pytest tests/ -v
./.venv/Scripts/python -c "import bot, tg_bot, database, commercial_parser, avito_messenger, avito_client, llm_classifier, listing_classifier, heuristic_scorer, logging_setup, env_config, accounts, llm_sanitizer; print('OK')"
```

## Что СДЕЛАНО (за последние сессии)

### H1 — OUTBOUND (proactive: бот пишет собственникам первым) — DONE (f0d835d)

**Главная новая фича.** До этого бот был чисто reactive (отвечал на
входящие). Теперь он сам идёт по уже-распарсенным листингам класса
'owner' и пишет ИХ собственникам первое сообщение.

**Архитектура:**
- `outbound_messenger.py` — новый модуль, OutboundMessenger.run_one_cycle
- БД: `outbound_contacts` таблица, UNIQUE(profile_id) — глобальный dedup
  (никогда не пишем одному собственнику дважды, даже разными аккаунтами)
- LLM prompts в `prompts/outbound_first_message.{system,user}.txt`
- 16 personas (small_business_office, retail_starter, ecommerce_warehouse,
  cafe_owner, и т.д.) × 3 формальности × 2 длины × 3 подхода = 288
  стилевых комбо. Каждый аккаунт — стабильно своя персона (hash от имени)
- A2 budget: action="outbound", default 10/день, в warmup отключено,
  при degraded health × 0.5
- F8 cycle: новый kind "outbound_only" с весом 0.30 в default

**Per-account конфиг (accounts.json):**
```json
"persona": "small_business_office",
"daily_budget_outbound": 10,
"outbound_max_per_cycle": 2,
"outbound_listing_min_age_hours": 1.0,
"outbound_between_messages_min_sec": 90.0,
"outbound_between_messages_max_sec": 240.0
```

**Защиты от fingerprinting:**
- temperature=0.95 в LLM → высокая вариативность текста
- llm_sanitizer вырезает phone/email/url/messenger handles
- min_listing_age_hours=1 → не пишем по только что распарсенным
- Type-and-send посимвольно (30-90ms задержки) — анти-fingerprinting
  через скорость ввода
- Pre/post-click captcha checks
- В warmup outbound_only выпадает с весом 0

**Capacity (для 200 контактов/день):**
- 1 аккаунт: 8-12 outbound/день в стабильном режиме
- 200 / 9 ≈ **22-25 аккаунтов** в production + ~5 резерв (warmup +
  периодические баны) = **пул ~30 аккаунтов**
- См. секцию Capacity ниже для подробного расчёта

**Тесты:** 33 теста в `tests/test_outbound_db.py` + `test_outbound_messenger.py`.

### Tier 1 (защита аккаунтов) — done
A1 per-account proxy · A2 daily budgets · A3 phone-click soft limit · A4 session pauses 30-90 min

### Tier 2 (для долгожителей) — done
B1 warmup · B2 active-hours · B3 thread-rng start · B4 long-cooldown при множественных капчах

### Tier 3 (полировка) — done
C1 health score · C2 budget alerts (TG, de-dup) · C3 captcha-log с traceability

### Tier 4 fingerprinting — DONE (все F1-F12 закрыты)
- **F1** ✓ favorite_rate=0.08, call_rate=0.05 (вместо 70%/55%) + учёт в A3 phone-clicks
- **F2** ✓ Variable batch sizes — `_weighted_listing_count()`, weights `[0,0.10,0.25,0.30,0.20,0.10,0.04,0.01]`, max_listings_per_search/max_categories_per_browse в config
- **F3** ✓ объединено с F1
- **F4** ✓ Yandex query pool: 70% thematic / 25% general / 5% skip
- **F5** ✓ Messenger reply delays: бот больше не отвечает мгновенно. На каждом
  цикле бросаем `random.lognormvariate(mu, sigma)` (default 2.5/1.0, mean
  ~30 мин), clamp'им в `[min_reply_age_min, max_reply_age_min]` (default
  15..600 мин), и сравниваем с возрастом первого появления сообщения в
  БД (`db.get_first_in_message_age_seconds(dialog_id, text)`, через
  `MIN(timestamp)` чтобы устоять к дубликатам add_message). Если возраст
  меньше target — `return` из `_handle_current_chat`, на след.цикле
  бросок другой — статистически распределение задержек ~ lognormal.
  F5b: 5% chance «никогда не отвечать» для НОВЫХ диалогов (`out_count==0`),
  состояние в `account_state.ignored_dialogs` (in-memory, ресет на рестарт).
  Параметры: `messenger_min_reply_age_min/max_reply_age_min/reply_delay_mu/
  reply_delay_sigma/ignore_new_dialog_chance` в config.json или per-account.
  AvitoClient принимает `messenger_config: dict | None`, прокидывает в
  AvitoMessenger через `**dict`.
- **F6** ✓ Probabilistic active hours: B2 раньше был бинарным флагом
  (в окне → 100%, вне → 0%). Теперь — `_ACTIVITY_BY_HOUR` (часовое
  распределение, 0:00=0.02, 10:00=0.95, 22:00=0.45, ...) + функция
  `_active_probability(account, cfg, hour=None)`. На каждом цикле
  бросаем монетку: `random.random() > prob` → пропуск цикла (sleep
  30-90 мин). Совместимость с B2: если `active_hours_start/end` заданы,
  ВНЕ окна prob=0 принудительно. Per-account override через
  `account.activity_pattern` (dict {hour:prob}).
- **F7** ✓ Random «dead days»: 5% дней (×3 в выходные = 15%) бот вообще
  не работает. Решение принимается раз в день (кэш в
  `account_state.dead_day_decision`). Per-account `dead_day_rate`.
- **F8** ✓ Idle cycles: 4 типа цикла (full/messenger_only/browse_only/profile_check) с
  default-распределением 0.55/0.20/0.15/0.10. Per-account override через
  `cycle_distribution` в accounts.json, warmup-mode (B1) использует жёсткое
  `_CYCLE_KINDS_WARMUP` (full=0.20, messenger_only=0.0, browse_only=0.40,
  profile_check=0.40) — в warmup мы и так не отправляем сообщения.
  `_do_profile_check(driver, account_name)` — короткий цикл visit `/profile`
  + опц. `/profile/favorites` (30%).
- **F9** ✓ Realistic dwell times: lognormal-распределение время «задержки»
  на listing-странице с поправкой на interest score (объявления, которые
  выглядят интереснее → дольше смотрим).
- **F10** ✓ scroll_gallery с 60% probability + variable iters
  (`random.randint(1,12)` вместо фикс. 20). Раньше каждый view → ровно 20
  пролистываний — палевно.
- **F11** ✓ Variable number of dialogs to process: weighted choice
  `_pick_dialog_count(available)` с весами `[0, 0.30, 0.25, 0.20, 0.10,
  0.08, 0.05, 0.02]` — пик на 1-3 диалогах, cap на 7. Раньше всегда
  `range(min(5, len(dialogs)))` → ровно 5.
- **F12** ✓ Budget guard для browse_commercial_categories:
  `AvitoClient.browse_commercial_categories` теперь ДО входа в browse
  проверяет A2-бюджет на "listings" (если db_manager есть). Иначе
  browse листает 3×3=9 листингов мимо A2-счётчика → реальное превышение
  лимита ~10-15%.

### Критические фиксы K1/K2/K3 — done
- **K1** accounts.json как источник истины (TG-CRUD больше не пишет в config["accounts"]); атомарная запись + threading.Lock; миграция legacy; фикс бага удаления (acc_del_* ловил acc_del_ok_*)
- **K2** `llm_sanitizer.sanitize_llm_reply()` блокирует 7 классов: empty/too_short/too_long/phone/messenger_url/tg_handle/email. Интегрирован в `avito_messenger.py:359-385`
- **K3** `tg_bot.py` больше не делает `sqlite3.connect()` мимо DatabaseManager → `db.get_classification_stats()`

### Quick-wins L1-L8 — done
- L1 print() → `_bot_logger` в bot.py (CRITICAL → TGAlertHandler автоматически)
- L2 bare except → конкретные в не-Selenium коде (`update_profile_proxy`, `get_random_proxy`, main `input()`)
- L3 `api_key: str = None` → `str | None`
- L4 дубликат `self._cfg()` → одна локальная переменная
- L5 mtime-кэш для `TelegramController._cfg()` (deepcopy, инвалидация на изменение mtime)
- L6 `run_thread` top-level → `logger.exception` с traceback
- L7 `driver.quit()` → `except WebDriverException`
- L8 `_save_cfg()` атомарная запись (tempfile + os.replace)

### Quick-wins L9-L12 — done
- **L9** `accounts.remove_account` теперь чистит связанный cookies-файл
  (`acc["cookies_path"]`) при удалении аккаунта. Делается ВНЕ лока: основная
  запись в accounts.json уже зафиксирована, ошибка очистки куки — только в
  лог.
- **L10** `tg_bot.py` cookies-upload (текстовый JSON) — `except Exception` →
  `except json.JSONDecodeError`. Добавлен guard `(message.text or "").strip()`,
  чтобы non-text сообщения (фото без подписи) не падали с AttributeError, а
  получали валидный error-message.
- **L11** Добавлен метод `AdsPowerAPI.update_proxy(user_id, proxy_str) -> bool`,
  который инкапсулирует POST на `/api/v1/user/update`. Top-level
  `update_profile_proxy()` теперь — однострочный делегат (back-compat для
  `tests/test_proxy.py`, которые мокают этот символ).
- **L12** `AdsPowerAPI.stop_profile()` `except Exception: pass` →
  `except (requests.RequestException, ValueError): pass`. Stop — fire-and-forget,
  но прячем теперь только сетевые/JSON ошибки, реальные баги пробрасываем.

## Что ОСТАЛОСЬ (по приоритетам)

### Tier 4 fingerprinting — ✅ всё закрыто (F1-F12)

См. секцию выше. Если придумать хочется новых F-задач — смотри `zadachi.txt`,
там идеи могли остаться (например, более сложные user-paths, A/B-варианты
LLM-промптов под разные «персоны»).

### Крупные refactor S-уровня (риск регрессий)
- ~~**S1** декомпозиция `bot.run_thread`~~ ✓ done (b056d47): 363 → 70 строк
  + 7 helpers (`_apply_per_account_overrides`, `_apply_warmup_if_new`,
  `_check_health_and_log`, `_connect_with_retry`, `_build_avito_client`,
  `_sleep_until_tomorrow`, `_run_main_loop`) + 11 тестов в
  `tests/test_run_thread_helpers.py`.
- ~~**S3** декомпозиция `commercial_parser.py`~~ ✓ done (2ef06d7):
  - `extract_listing_data`: 213 → 35 строк + 11 helpers
    (`_extract_title/price/description/location/area/category/seller_info/
    publication_date/photos`, `_try_show_phone`, `_enrich_phones_from_description`)
  - `save_listing_to_db`: 154 → 40 строк + 3 helpers
    (`_normalize_for_db`, `_save_phones_for_listing`,
    `_record_listing_outcome_metrics`)
  - +21 тест в `tests/test_commercial_parser_helpers.py` (на pure helpers).
- ~~**S2** декомпозиция `tg_bot.py`~~ ✓ done (c829cc4 + eca3d2a):
  - Stage 0 (c829cc4): `_edit_or_send(chat_id, message_id, text, kb=None)`
    helper. 11 копипастов try-edit-except-send → 1-line вызовы. +4 теста.
  - Stage 1-3 (eca3d2a): `_setup()` 932 → 30 строк (×30 короче).
    - 8 message-команд → `_cmd_*` методы класса
    - `handle_dialog` ~280 строк → `_handle_dialog` диспетчер +
      14 `_dialog_*` методов (dispatch by state через dict)
    - `on_callback` ~370 строк → `_on_callback` диспетчер +
      24+ `_cb_*` методов (B1-special / prefix-table / exact-map)
    - +11 тестов в `tests/test_tg_dispatchers.py` (диспетчеры,
      prefix-резолюция, _allowed-блокировка)
  - Добавление новой TG-команды теперь = `_cmd_X` метод + одна
    строка в `_setup`. Аналогично для callback и dialog state.

### Опциональное
- **E1** Phase 2 G1: миграция реализаций в AvitoClient (сейчас тонкая обёртка с lazy-импортами)
- **D1** SQLite task queue (только если 10+ аккаунтов)

## Конвенции проекта (см. AGENTS.md)

- **Логирование**: `logging.getLogger(__name__)`, для per-account — `get_account_logger(name, account_id)`. Никогда не `print()` в business logic. CRITICAL автоматически идёт в TGAlertHandler (E4).
- **БД**: всегда через `DatabaseManager`. Многошаговые писательские операции — внутри `db.transaction()` с `cursor=cur`.
- **Секреты**: через `.env` (priority over config.json через `env_config.apply_env_overrides`).
- **LLM-промпты**: в `prompts/`, через `_load_prompt(name)` + `.format(...)`.
- **Selenium**: bare `except:` разрешён только в Selenium-flow (E722 в ruff ignore).
- **Тесты**: pytest, фикстура `db` в `tests/conftest.py`, mock LLM. `LLMClassifier.classify_listing` / `generate_response` никогда не вызывают реальный OpenAI.
- **Style**: line-length 100, double quotes, ruff format. Комментарии на русском OK.

## Куда смотреть для продолжения

- `zadachi.txt` — полный roadmap с детальными ТЗ (Tier 4 / F1-F12 теперь все
  закрыты, но там же лежат идеи Tier 4+ и S-задачи)
- `AGENTS.md` — project conventions
- `tests/test_tg_cfg.py` — пример тестов через monkeypatch для TelegramController (полезно для S2)

Что осталось из крупного — только опциональное:

1. **E1** (Phase 2 G1) — миграция Selenium-реализаций ВНУТРЬ AvitoClient,
   сейчас он — тонкая обёртка с lazy-импортами bot/commercial_parser/
   avito_messenger. После S1/S3 функции уже разрезаны на helpers — можно
   переносить группами. Риск низкий, ценность чисто архитектурная (нет
   business-эффекта). Делать когда нужны крупные правки Selenium-логики.
2. **D1** (SQLite task queue) — только если масштабирование до 10+
   аккаунтов. Текущая архитектура (поток на аккаунт) справляется на 1-5.

Вся «тяжёлая» работа закрыта:
- Tier 1-3 (защита аккаунтов): A1-A4 / B1-B4 / C1-C3 ✓
- Tier 4 (behavioral fingerprinting): F1-F12 ✓
- Quick-wins: L1-L12 ✓
- Crit fixes: K1-K3 ✓
- Большие refactor: S1 (run_thread), S2 (tg_bot._setup), S3 (commercial_parser) ✓
- 387 unit-тестов покрывают все extracted helpers + диспетчеры.
