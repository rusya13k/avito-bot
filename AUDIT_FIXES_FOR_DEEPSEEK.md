# Аудит Avito-бота и подробный план исправлений

> Документ-инструкция для исполнителя (DeepSeek). Здесь собраны **корневые причины** текущих проблем (по логам + коду) и **пошаговые правки** с указанием файлов, строк, фрагментов «до/после» и способов проверки.
>
> Дата аудита: 2026-06-12. Анализировались: `logs/bot.log`, `bot_stderr.log`, `bot_run.log`, конфиги и исходники.

---

## 0. Контекст окружения (читать обязательно)

- **Разработка** ведётся на **Windows** (`C:\dev\new bot`).
- **Продакшен** работает на **Linux** (`/opt/avito-bot/`, пользователь `avito`, развёртывание через `deploy.sh`). На это указывают пути в `tmp_*.py` (`/opt/avito-bot/bin/chromedriver`, `/tmp/...`) и файл `logs/login_failed_server.png`.
- Браузер запускается через **AdsPower** (REST API на `127.0.0.1:50325`), профиль `k1c2utgb`, headful (`headless=0`, Xvfb `:99` на Linux). AdsPower сам поставляет свой Chromium **147.0.7727.56** и свой `chromedriver`.
- Один активный аккаунт: `пупупу` (см. `accounts.json`).
- LLM: на сервере через `.env` подключён Anthropic-совместимый эндпоинт `https://vibecode.moe/v1/messages` (в `config.json` стоит другой — `https://api.coda.ink/v1`, GPT-5.5; реальное значение переопределяется из `.env`).

### ⚠️ КРИТИЧНО №0 — рассинхрон prod ↔ repo (проверить ПЕРВЫМ ДЕЛОМ)

В **текущем коде** `bot.py:2183-2186` функция `_apply_account_proxy` для аккаунта с `adspower_id` **сразу** возвращает `"__no_proxy__"` и НЕ спрашивает админа про прокси:

```python
# bot.py:2183-2186 (текущая версия в репо)
if account.get("adspower_id"):
    log(account_name, "A1: AdsPower профиль — прокси управляется AdsPower")
    return "__no_proxy__"
```

Но в **логах сервера** аккаунт `пупупу` (у которого `adspower_id="k1c2utgb"`) бесконечно крутится в цикле «Нет доступного прокси → запрос админу в TG → таймаут 600s → рестарт». Это поведение **старой** версии кода, где early-return ещё не было.

**Вывод:** на сервере задеплоена устаревшая версия `bot.py`. Значительная часть проблем из логов могла быть уже частично исправлена локально, но **не выкачена на прод**.

**Действие (шаг 0):**
1. На сервере: `cd /opt/avito-bot && git log --oneline -5` — сравнить с локальным `git log --oneline -5` (последний коммит должен быть `9006c57 fix: connect_to_sphere ...`).
2. Убедиться, что локальные несохранённые правки (`git status` показывает `M bot.py`, `M captcha_detect.py`) **закоммичены и задеплоены**, иначе любые фиксы ниже бессмысленны.
3. Зафиксировать чёткий процесс деплоя: правка → коммит → `git pull` на сервере → рестарт сервиса. Все правки ниже проверять **на той версии, что реально крутится на сервере**.

---

## 1. Сводка корневых причин (по приоритету)

| # | Приоритет | Проблема | Корень | Где |
|---|-----------|----------|--------|-----|
| P0-A | 🔴 Блокер | Браузер не стартует: `session not created: ChromeDriver supports 148, browser is 147` | После падения AdsPower-драйвера код доходит до `ChromeDriverManager()` без версии → качает latest (148) ≠ Chromium AdsPower (147) | `bot.py:200-212`, `connect_to_sphere` |
| P0-B | 🔴 Блокер | Бесконечный no-proxy цикл → crash-loop stop | Старая версия на сервере + мёртвый локальный SOCKS `127.0.0.1:10800` + таймаут ожидания админа 600s, после которого поток умирает и рестартует с тем же вопросом | `bot.py:2183-2236`, `proxy_health.py` |
| P0-C | 🔴 Блокер | LLM не генерит сообщения: `402 Payment Required` (vibecode.moe) | Закончился баланс/неоплачен ключ LLM-эндпоинта `https://vibecode.moe/v1/messages` | `.env`, `llm_classifier.py:131-192`, `outbound_messenger.py:106-153` |
| P1-D | 🟠 Высокий | `run_thread crashed` по `ReadTimeoutError (read timeout=120)` | Браузер зависает (мёртвый прокси/DNS), Selenium-команда висит 120s и кидает исключение, весь поток падает | `bot.py` (selenium client timeout), `outbound_messenger.py:377` |
| P1-E | 🟠 Высокий | `TG-controller недоступен — login interactive невозможен` | `_tg_controller` не инициализирован/`admin_id` не виден в момент запроса; login требующий SMS падает «в молоко» | `bot.py:1885-1908`, `tg_bot.py:132-177` |
| P1-F | 🟠 Высокий | Логин на сервере падает (`login_failed_server.png`); селекторы формы под вопросом | Avito мог изменить DOM формы логина; селекторы `login-form/...` могли устареть | `bot.py:1936-2105`, `avito_client.py:210-304` |
| P1-G | 🟠 Высокий | Спам `IP BLOCK DETECTED (checkbox-captcha)` в Yandex-warmup | IP сервера в бане у Яндекса (работа **без прокси** из-за P0-B) | `bot.py:1317-1436`, `warmup.py` |
| P2-H | 🟡 Гигиена | Мёртвый код в `connect_to_sphere` (строки 214-218, `NameError` риск); stealth не применяется | Все ветки выше делают `return`/`raise` → код недостижим, ссылается на неопределённую `driver` | `bot.py:214-218` |
| P2-I | 🟡 Гигиена | `pkill` валится на Windows; `config.json` warning про `accounts`; кэш probe усугубляет цикл; мусорные `tmp_*.py` | Кроссплатформенность и шум в логах | `bot.py:150-155, 3120-3123`, `account_state.py` |

---

## 2. Детальные исправления

> Для каждого пункта: **что чинить**, **почему**, **как** (с фрагментами), **проверка**. Перед правкой `bot.py` сделай бэкап или работай в ветке.

---

### P0-A. ChromeDriver 148 vs Chromium 147 — браузер не стартует

**Файл:** `bot.py`, функция `connect_to_sphere` (строки ~138-218).

**Корень:** Порядок попыток подключения:
1. AdsPower `webdriver_path` через `Service(..., port=0)` — правильная версия (147).
2. AdsPower `webdriver_path` через subprocess.
3. Системный `chromedriver` из PATH (`webdriver.Chrome(options=options)`) — тут срабатывает **Selenium Manager**, который при отсутствии локального драйвера качает latest (148) и/или висит 120s (`Standard driver install failed: ... Read timed out`).
4. `ChromeDriverManager().install()` **без версии** → тоже latest (148) → `session not created: only supports 148, browser is 147`.

**Что сделать:**

1. **Зафиксировать происхождение `webdriver_path`.** AdsPower отдаёт путь к **совместимому** chromedriver в ответе API (`adspower_launcher._parse_response` → ключ `webdriver`). Если попытки 1/1b падают — нужно понять почему (битый путь? нет файла? таймаут?), а не молча падать на latest. Добавь явный лог фактического `webdriver_path` и версии.

2. **Убрать/обезопасить fallback на latest.** Попытка 4 (`ChromeDriverManager()` без версии) — главный источник mismatch. Заменить на строгий вариант: качать **только** под версию браузера, иначе фейлить явной ошибкой.

```python
# bot.py, попытка 3-4 — БЫЛО:
# Попытка 3: ChromeDriverManager с версией браузера
try:
    if browser_version:
        return webdriver.Chrome(service=Service(ChromeDriverManager(driver_version=browser_version).install()), options=options)
except Exception:
    pass

# Попытка 4: ChromeDriverManager без версии
try:
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
except Exception as exc:
    _bot_logger.error("All chromedriver attempts failed: %s", exc)
    raise
```

```python
# СТАЛО:
# Попытка 3: ChromeDriverManager строго под версию браузера (major)
try:
    if browser_version:
        # Берём major-версию (147), чтобы webdriver-manager подобрал совместимый драйвер
        major = browser_version.split(".")[0]
        _bot_logger.info("connect_to_sphere: ChromeDriverManager для версии %s", major)
        return webdriver.Chrome(
            service=Service(ChromeDriverManager(driver_version=major).install()),
            options=options,
        )
except Exception as exc:
    _bot_logger.error("connect_to_sphere: ChromeDriverManager(%s) failed: %s", browser_version, exc)

# НЕ качаем latest вслепую — это и есть источник mismatch 148 vs 147.
_bot_logger.error(
    "connect_to_sphere: все совместимые драйверы провалились "
    "(browser=%s). Проверь webdriver_path от AdsPower и DISPLAY=:99.",
    browser_version,
)
raise RuntimeError(f"No compatible chromedriver for Chrome {browser_version}")
```

3. **Запинить chromedriver на сервере.** Самый надёжный способ — не полагаться на Selenium Manager/webdriver-manager вообще, а использовать **только** chromedriver от AdsPower. На сервере проверить, что AdsPower реально возвращает `webdriver` в API-ответе:
   ```bash
   curl -s "http://127.0.0.1:50325/api/v1/browser/active?user_id=k1c2utgb" | python3 -m json.tool
   ```
   В ответе `data.webdriver` должен быть путём к существующему файлу нужной версии (147). Если его нет/битый — обновить AdsPower или вручную положить chromedriver 147 и прописать путь.

4. **Если AdsPower обновил Chromium до 148** — тогда наоборот: нужен chromedriver 148. То есть **версии должны совпадать**; зафиксируй, какая реально стоит:
   ```bash
   curl -s http://127.0.0.1:<debug_port>/json/version   # "Browser": "Chrome/147..." или 148
   /opt/avito-bot/bin/chromedriver --version
   ```

**Проверка:** профиль стартует, в логах `connect_to_sphere: AdsPower chromedriver OK`, нет `session not created`. Прогнать `tmp_test_login.py` (он подключается тем же путём) — должен подключиться без ошибки версии.

---

### P0-B. Бесконечный no-proxy цикл + мёртвый локальный SOCKS-прокси

**Файлы:** `bot.py:2159-2236` (`_apply_account_proxy`, `_ask_admin_no_proxy`), `proxy_health.py`, `account_state.py` (`wait_user_resume`, probe-кэш), `accounts.json`.

**Корень (три слоя):**

1. **Рассинхрон:** на сервере старый `bot.py` без early-return по `adspower_id` (см. раздел 0). У `пупупу` есть `adspower_id`, значит прокси **вообще не нужен боту** — им управляет AdsPower. После выката текущей версии (`bot.py:2183-2186`) цикл no-proxy для AdsPower-аккаунтов исчезнет сам.

2. **Мёртвый прокси:** в `accounts.json` указан `"proxy": "socks5://127.0.0.1:10800"` — это локальный SOCKS-туннель (видимо, клиент VPN/прокси-цепочки), который **на сервере не поднят** → `_tcp_connect_proxy` падает за 2s → probe фейлит. Но т.к. у аккаунта AdsPower, это поле для него вообще игнорируется (после фикса п.1).

3. **Порочный таймаут:** даже для не-AdsPower аккаунтов, при отсутствии прокси `_ask_admin_no_proxy` ждёт ответа админа **600 секунд**, после таймаута возвращает `None`, поток `run_thread` завершается (`bot.py`, проверка `if proxy_result is None: return`), супервизор рестартует поток — и **снова тот же вопрос**. За час 3 рестарта → `NOT restarting (possible crash loop)`.

**Что сделать:**

1. **Выкатить текущую версию** (early-return по `adspower_id`) — это закрывает 90% проблемы для текущего аккаунта. ✅ (раздел 0)

2. **Починить сам порочный цикл** (на случай не-AdsPower аккаунтов). Сейчас при таймауте ожидания админа поток просто умирает и рестартует. Нужно: при таймауте no-proxy **не убивать поток в бесконечном рестарте**, а ставить аккаунт в «паузу до ручного вмешательства» — без рестарт-спама.

   В `_ask_admin_no_proxy` различать `timeout` и `cancel`. Сейчас `_wait_user_resume_for_login` возвращает `"timeout"`, но `_ask_admin_no_proxy` сваливает всё в `None`:

```python
# bot.py:2232-2236 — БЫЛО:
if response == "continue":
    log(account_name, "A1: Админ разрешил продолжить БЕЗ прокси.")
    return "__no_proxy__"
log(account_name, "A1: Аккаунт остановлен админом (нет прокси).")
return None
```

```python
# СТАЛО:
if response == "continue":
    log(account_name, "A1: Админ разрешил продолжить БЕЗ прокси.")
    return "__no_proxy__"
if response == "timeout":
    # Не рестартим поток вхолостую каждые 10 минут — помечаем аккаунт
    # как «ждёт ручного решения по прокси» и НЕ перезапускаем.
    log(account_name, "A1: Таймаут ответа админа по прокси — аккаунт в паузе (без авто-рестарта).")
    account_state.mark_account_paused(account_name, reason="no_proxy_timeout")  # см. п.3
    return None
log(account_name, "A1: Аккаунт остановлен админом (нет прокси).")
return None
```

3. **Добавить флаг «пауза без рестарта»** и научить супервизор его уважать. В супервизоре (`bot.py`, цикл перезапуска потоков, ~3220-3260) перед рестартом проверять `account_state.is_account_paused(acc_name)` и, если да — **не рестартить** (логнуть один раз и оставить аккаунт остановленным до `/resume`). Если в `account_state` нет такого механизма — добавить простой потокобезопасный set приостановленных аккаунтов с методами `mark_account_paused/clear_account_paused/is_account_paused`.

4. **Сократить таймаут ожидания и не повторять вопрос бесконечно.** Снизить `timeout_seconds` в `_ask_admin_no_proxy` с 600 до, например, 300, и слать вопрос админу **один раз** (а не на каждом рестарте). После выката п.2-3 повтор уйдёт сам.

5. **Не кэшировать неуспешный probe так же долго, как успешный.** В `account_state` probe-кэш имеет TTL 300s для любого результата (`_probe_cache_ttl_sec = 300.0`). Из-за этого после первого фейла прокси «мёртв» ещё 5 минут, и рестарт сразу получает FAIL из кэша. Сделать TTL для FAIL короче (например 30-60s) либо кэшировать только `ok=True`:

```python
# account_state.py, set_cached_probe — добавить разделение TTL:
def set_cached_probe(self, proxy_str: str, result: Any) -> None:
    ttl = self._probe_cache_ttl_sec if getattr(result, "ok", False) else 30.0
    with self._probe_cache_lock:
        self._probe_cache[proxy_str] = (result, time.time(), ttl)
# и в get_cached_probe читать индивидуальный ttl из кортежа
```

6. **Прибраться в `accounts.json`:** для AdsPower-аккаунта поле `proxy: "socks5://127.0.0.1:10800"` вводит в заблуждение. Либо убрать его (прокси настраивается в самом профиле AdsPower), либо поднять локальный SOCKS-туннель на сервере, если он реально нужен. Решение зависит от того, через что аккаунт должен ходить — **уточнить у владельца** (это влияет на анти-бан).

**Проверка:** запустить бота; AdsPower-аккаунт стартует без единого `no_proxy_warning` в логах; никаких `thread died — restarting`. Для теста не-AdsPower сценария: временно убрать `adspower_id`, убедиться, что при отсутствии прокси аккаунт встаёт в паузу **один раз**, без рестарт-спама.

---

### P0-C. LLM `402 Payment Required` — не генерируются сообщения

**Файлы:** `.env`, `config.json`, `llm_classifier.py:131-192`, `outbound_messenger.py:106-153`.

**Корень:** На сервере `.env` переопределяет `OPENAI_API_BASE`/ключ на Anthropic-совместимый `https://vibecode.moe/v1/messages`, и у этого ключа **закончился баланс** (HTTP 402). Из-за этого:
- `outbound`: `generate_first_message` фейлит 2 раза → `return None` → контакт молча пропускается (`H1: не получилось сгенерировать сообщение ... скип`). Outbound-цикл «работает», но **не отправляет ничего**.
- Входящие ответы (`Ошибка генерации сообщения: 402 ...`) — тоже не генерируются.

**Что сделать:**

1. **Главное — пополнить баланс / заменить ключ LLM.** Это не код, а операционная задача:
   - Проверить на сервере, какой реально эндпоинт и ключ активны:
     ```bash
     cd /opt/avito-bot && grep -E "DEEPSEEK_API_KEY|OPENAI_API_KEY|OPENAI_API_BASE|OPENAI_MODEL" .env
     ```
   - Проверить баланс у провайдера (`vibecode.moe` или `api.coda.ink`). Пополнить **или** прописать рабочий ключ другого провайдера.
   - Быстрый тест ключа вручную (Anthropic-совместимый):
     ```bash
     curl -sS https://vibecode.moe/v1/messages \
       -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \
       -H "content-type: application/json" \
       -d '{"model":"claude-haiku-4-5","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'
     ```
     Ответ `402` = баланс/оплата; `200` = ключ рабочий.

2. **Согласовать `config.json` и `.env`.** Сейчас `config.json` говорит `api.coda.ink/v1 + gpt-5.5`, а реально (через `.env`) используется `vibecode.moe + claude-*`. Это путает диагностику. Привести к одному источнику истины: либо чистить `config.json` (оставить пустые ключи, всё из `.env`), либо наоборот. Зафиксировать в README, какой провайдер боевой.

3. **Сделать 402 «громкой» ошибкой, а не тихим скипом.** Сейчас при 402 outbound молча пропускает все контакты — со стороны выглядит как «бот работает, но ничего не шлёт». Нужно: при HTTP 402/401 (платёж/авторизация) — **один раз** уведомить админа в TG и пометить LLM как недоступный, чтобы не жечь циклы впустую.

```python
# outbound_messenger.py, _generate_first_message — в блоке except:
except requests.HTTPError as exc:  # отдельно ловим HTTP-коды
    status = getattr(exc.response, "status_code", None)
    if status in (401, 402, 403):
        logger.error("outbound: LLM недоступен (HTTP %s) — пополни баланс/проверь ключ", status)
        llm_classifier._notify_llm_down(status)  # один раз шлём админу в TG + ставим флаг
        return None  # без ретраев — деньги/ключ ретраи не вылечат
    ...
except Exception as exc:
    ...
```
   Реализовать в `llm_classifier` лёгкий «circuit breaker»: при 402/401 ставить флаг `_llm_disabled_until = now + N минут` и в начале `_call_llm` быстро возвращать ошибку, не делая HTTP-запрос (экономия времени циклов). Сбрасывать флаг по таймеру или по `/resume`.

4. **Проверить, не блокирует ли отсутствие LLM весь цикл.** По коду — нет (best-effort, контакт скипается). Но убедиться, что при недоступном LLM аккаунт всё равно делает «человеческую» активность (browse/favorites), а не простаивает подозрительно.

**Проверка:** после пополнения — в логах outbound `H1: отправлено сообщение ...` вместо `скип`; нет `402` в `bot.log`. Тест curl возвращает `200`.

---

### P1-D. `run_thread crashed` по Selenium `ReadTimeoutError (read timeout=120)`

**Файлы:** `bot.py` (создание драйвера / selenium client config), `outbound_messenger.py:377` (`self.driver.get(listing_url)`), `bot.py` обёртка `safe_get`.

**Корень:** Когда браузер зависает (мёртвый прокси → DNS не резолвится, либо AdsPower-профиль завис), любая Selenium-команда (`driver.get`) висит до дефолтного клиентского таймаута **120s** и кидает `urllib3 ReadTimeoutError`. В `outbound_messenger._contact_one` это исключение **не ловится** → пробивает до `run_thread`, который падает целиком (`run_thread crashed`), и супервизор рестартует поток.

**Что сделать:**

1. **Обернуть рискованные навигации в outbound в try/except**, чтобы одна зависшая страница скипала контакт, а не роняла поток:

```python
# outbound_messenger.py:377 — БЫЛО:
self.driver.get(listing_url)
```
```python
# СТАЛО:
try:
    self.driver.get(listing_url)
except WebDriverException as exc:   # ловит timeout/crash драйвера
    log_func(self.account_name, f"H1: не удалось открыть листинг ({str(exc)[:60]}), скип.")
    return False
```
   (импортировать `from selenium.common.exceptions import WebDriverException`.)

2. **Снизить клиентский таймаут Selenium** с 120s до разумных ~30-40s, чтобы зависший вызов не съедал по 2 минуты. В Selenium 4 это задаётся через `ClientConfig`/`command_executor`. Минимальный безопасный приём — задать таймаут на готовом драйвере:

```python
# сразу после создания драйвера в connect_to_sphere (перед return):
try:
    driver.command_executor.set_timeout(40)   # сек, вместо дефолтных 120
except Exception:
    pass
```
   (Проверить точный API под установленную версию Selenium — `requirements.txt` фиксирует `selenium==4.41.0`. Если `set_timeout` недоступен, использовать `driver.command_executor._client_config.timeout = 40` либо передать `ClientConfig(timeout=40)` в конструктор.)

3. **Различать «зависание браузера» и «обрыв сессии».** При `ReadTimeoutError`/`session deleted`/`disconnected` — корректно завершить профиль AdsPower (`adspower.stop_browser`) и пересоздать драйвер, а не просто рестартить весь поток с нуля.

**Проверка:** искусственно «убить» страницу (несуществующий домен) — контакт скипается с логом, поток жив. В логах нет `run_thread crashed` по таймауту.

---

### P1-E. `TG-controller недоступен — login interactive невозможен`

**Файлы:** `bot.py:1885-1908` (`_wait_user_resume_for_login`), `tg_bot.py:48, 132-177` (`send_user_action_request`, `_tg_controller`), `bot.py` `main()` (инициализация TG).

**Корень:** `send_user_action_request` возвращает `False` (→ лог «TG-controller недоступен»), если в момент запроса `_tg_controller is None` либо у него не задан `admin_id/admin_ids`. Это происходит, когда:
- бот стартовал, но `/start` в TG ещё не нажат / `_tg_controller` не присвоен в `tg_bot`;
- или поток аккаунта запустился **раньше**, чем инициализировался TG.

Из логов видно противоречие: иногда TG работает (`TG bot polling started`, ответы админа приходят), иногда `недоступен`. То есть это **гонка инициализации** или порядок запуска.

**Что сделать:**

1. **Гарантировать инициализацию TG до старта потоков аккаунтов.** В `main()` убедиться, что `_tg_controller` (с `admin_ids`) присвоен в `tg_bot` **до** запуска `run_thread`. Если потоки стартуют сразу — добавить ожидание готовности TG (короткий барьер) или передавать контроллер в потоки явно.

2. **Не молчать при недоступном TG в критичных местах.** Сейчас при `delivered == False` функция возвращает `"cancel"` и login тихо проваливается. Добавить явный лог-ERROR с причиной (`_tg_controller is None?` / `admin_ids пуст?`), чтобы было видно корень:

```python
# bot.py:1896-1900 — добавить диагностику:
delivered = _tg.send_user_action_request(account_name, req.request_id, prompt)
if not delivered:
    _bot_logger.error(
        "[%s] TG-controller недоступен (controller=%s, admin_ids=%s) — "
        "interactive login невозможен",
        account_name, _tg.controller_state(), _tg.admin_ids_state(),
    )
    log(account_name, "  TG-controller недоступен — login interactive невозможен")
    return "cancel"
```
   (добавить в `tg_bot` хелперы `controller_state()`/`admin_ids_state()` для диагностики, либо просто логнуть `_tg_controller is None` и наличие admin_ids.)

3. **Проверить токен и admin_ids на сервере.** В `config.json` `telegram_bot_token: ""` (пусто) — значит токен берётся из `.env` (`TELEGRAM_BOT_TOKEN`/аналог). Проверить:
   ```bash
   grep -iE "TELEGRAM|ADMIN" /opt/avito-bot/.env /opt/avito-bot/config.json
   ```
   `telegram_admin_ids` в `config.json` заданы (`1951766747`, `411399016`) — убедиться, что админ реально нажал `/start` (бот пишет в чат только тем, кто инициировал диалог).

**Проверка:** при запуске бота админ получает в TG приветствие/готовность; при искусственном no-proxy/SMS-сценарии кнопки приходят. В логах нет `TG-controller недоступен`, если TG настроен.

---

### P1-F. Логин падает на сервере; устаревшие селекторы формы

**Файлы:** `bot.py:1936-2105` (`perform_login`), `avito_client.py:210-304` (`login`), `bot.py:1833-1882` (`is_session_authenticated`), `captcha_detect.py`. Диагностика — `logs/login_failed_server.png`, `tmp_*login*.py`.

**Корень:** Сейчас обычно срабатывает «native AdsPower session is live → skipping login» (т.е. профиль уже залогинен куками AdsPower). Но когда сессия слетает, идёт ручной логин `perform_login`, который ищет элементы по `data-marker`:
- телефон: `input[data-marker='login-form/login/input']` (bot.py:1961)
- submit: `//button[@data-marker='login-form/submit']` (bot.py:1980, 2043)
- пароль: `input[data-marker='login-form/password/input']` (bot.py:2020)

Avito периодически меняет вёрстку → селекторы устаревают → 45s polling истекает → `login_failed_server.png`. Именно это разработчик отлаживал во `tmp_*login*.py`.

**Что сделать:**

1. **Проверить актуальность селекторов вживую.** Запустить на сервере `tmp_test_login.py` (он логирует, какие `login-form/*` маркеры реально присутствуют и видимы). По выводу:
   - если маркеры есть → проблема не в селекторах, а в капче/SMS/защите профиля;
   - если маркеров нет → Avito сменил вёрстку, обновить селекторы под текущий DOM (взять новые `data-marker` из `tmp_check_login.py`, который дампит все `data-marker` страницы).

2. **Сделать селекторы устойчивее** — искать по нескольким вариантам (новый + старый), а не по одному:

```python
# Пример устойчивого поиска поля телефона (заменить жёсткий селектор):
PHONE_SELECTORS = [
    "input[data-marker='login-form/login/input']",
    "input[name='login']",
    "input[type='tel']",
    "input[autocomplete='username']",
]
phone_input = _find_first(driver, wait, PHONE_SELECTORS)  # хелпер: пробует по очереди
```
   Аналогично для submit и password. Хелпер `_find_first` пробует каждый селектор с коротким ожиданием и возвращает первый видимый.

3. **Опираться на «native session» как основной путь.** Поскольку профили AdsPower уже залогинены, ручной `perform_login` — аварийный. Стоит укрепить `is_session_authenticated` (она уже устойчива: проверяет редирект на /login + маркеры) и логировать причину слёта сессии, чтобы реже доходить до ручного логина.

4. **На время отладки** оставить сохранение скриншота и **дамп `page_source`** при фейле логина (сейчас сохраняется только PNG) — это ускорит диагностику смены вёрстки:
```python
# рядом с сохранением login_failed_*.png:
Path("logs", f"login_failed_{account_name}.html").write_text(driver.page_source, encoding="utf-8")
```

**Проверка:** `tmp_test_login.py` находит поля; при слёте сессии ручной логин доходит до успеха ИЛИ корректно эскалирует SMS-запрос в TG (а не молча падает по таймауту).

---

### P1-G. Спам IP-блока Яндекса в warmup

**Файлы:** `bot.py:1317-1436` (`yandex_warmup`), `bot.py:434-470` (`check_block`), `warmup.py:310-394` (`big_warmup`, параметр `with_yandex_search`).

**Корень:** Warmup ходит на `yandex.ru/search` для «прогрева» фингерпринта, но **IP сервера в бане у Яндекса** (усугублено работой без прокси из-за P0-B) → каждый запрос ловит `checkbox-captcha` → `Warmup FAILED`. Это не роняет работу (best-effort), но засоряет логи и тратит время/палит автоматизацию.

**Что сделать:**

1. **Первично — починить прокси (P0-B).** С нормальным резидентным прокси Яндекс перестанет банить. Это корень.

2. **Сделать Yandex-warmup опциональным через конфиг.** Сейчас `big_warmup(..., with_yandex_search=True)` зашит. Добавить чтение из конфига и **выключить, пока IP в бане**:

```python
# там, где вызывается big_warmup (bot.py):
with_yandex = bool(cfg.get("warmup_yandex_enabled", True))
big_warmup(driver, account_name, with_yandex_search=with_yandex, yandex_queries=cfg.get("warmup_yandex_queries", 1))
```
   В `config.json` добавить `"warmup_yandex_enabled": false` до восстановления прокси.

3. **Circuit breaker для warmup:** если N запусков подряд ловят капчу — отключить Yandex-warmup для аккаунта на X часов (хранить в `account_state`), чтобы не долбить забаненный IP и не светиться.

4. **Заменить «прогрев» на менее палевный источник** (необязательно): вместо Яндекс-поиска — заход на нейтральные сайты (как уже делает `big_warmup` для сайтов). Yandex-search даёт мало пользы при забаненном IP.

**Проверка:** при `warmup_yandex_enabled=false` нет строк `IP BLOCK DETECTED`/`Warmup FAILED` от Яндекса; при включённом и живом прокси — `Warmup completed (N queries successful, 0 captcha)`.

---

### P2-H. Мёртвый код и риск `NameError` в `connect_to_sphere`

**Файл:** `bot.py:214-218`.

**Корень:** Все ветки попыток 1-4 заканчиваются `return` или `raise`, поэтому строки 214-218 **недостижимы**. Вдобавок они ссылаются на `driver`, которой в этой области нет → если бы выполнились, был бы `NameError`. Заодно это значит, что **stealth-инъекция в этой функции не происходит вообще** (хотя для AdsPower это, возможно, и не нужно — проверить, где stealth реально применяется).

**Что сделать:** удалить недостижимый блок:

```python
# bot.py:214-218 — УДАЛИТЬ ЦЕЛИКОМ:
    # Stealth (AdsPower не нужен, но на всякий случай не убираем)
    if not _apply_stealth(driver):
        _bot_logger.warning("T7/T8: stealth-инъекция не применилась (CDP недоступен?)")

    return driver
```
   Если stealth для AdsPower всё же желателен — применять его **к успешно созданному `driver` перед каждым `return`** (или вынести в обёртку, которая зовёт `connect_to_sphere`, получает `driver` и затем применяет stealth). Уточнить необходимость: AdsPower обычно сам маскирует фингерпринт.

**Проверка:** `ruff check bot.py` без ошибок; `python -c "import bot"` импортируется; функция возвращает драйвер как раньше.

---

### P2-I. Кроссплатформенность, шум в логах, мусор

**Файлы:** `bot.py:150-155` (`pkill`), `bot.py:3120-3123` (warning про accounts), временные `tmp_*.py`.

1. **`pkill` валится на Windows.** Сейчас обёрнуто в `try/except: pass`, но на Windows это просто всегда no-op + лишний процесс-спавн. Сделать платформо-зависимо:
```python
import platform
if platform.system() != "Windows":
    try:
        subprocess.run(["pkill", "-f", "chromedriver.*chrome-logs"], capture_output=True, timeout=5)
    except Exception:
        pass
```
   На Windows для очистки stale-драйверов использовать `taskkill /F /IM chromedriver.exe` при необходимости (обычно не нужно — это прод-путь Linux).

2. **Warning «в config.json отсутствует ключ accounts».** Это ложная тревога: аккаунты берутся из `accounts.json` (новый формат), а `bot.py:3120-3123` всё равно ругается. Понизить до `debug` или убрать, т.к. рядом `accounts.py` уже логирует корректный путь источника:
```python
# bot.py:3120-3123 — заменить warning на debug (или убрать), раз есть accounts.json
if "accounts" not in cfg or not isinstance(cfg.get("accounts"), list):
    _bot_logger.debug("config.json без блока 'accounts' — используется accounts.json (ожидаемо)")
```

3. **Удалить временные отладочные скрипты** после починки логина (или вынести в `tools/` и добавить в `.gitignore`): `tmp_after_login_fail.py`, `tmp_check_login.py`, `tmp_test_login.py`, `tmp_test_login_after_submit.py`, `tmp_test_login_full.py`. Они содержат хардкод путей `/opt/avito-bot/bin/chromedriver` и `user_id=k1c2utgb` — не должны попасть в репозиторий как есть.

4. **Логи в кодировке cp1251 на Windows** (`bot_stderr.log` показывает кракозябры). Убедиться, что логгер пишет в UTF-8 (`logging_setup.py`) — на Linux-проде это не проблема, но для локальной отладки стоит выставить `encoding="utf-8"` у файловых хендлеров.

**Проверка:** `ruff check .` чисто; `git status` без `tmp_*`; логи читаемы.

---

## 3. Рекомендуемый порядок выполнения

1. **Шаг 0 — синхронизация prod↔repo** (раздел 0). Без этого остальное не имеет смысла.
2. **P0-A** (chromedriver версия) — иначе браузер не стартует.
3. **P0-B** (прокси/no-proxy цикл) — поднять прокси-туннель ИЛИ убрать `proxy` у AdsPower-аккаунта + выкатить early-return + починить crash-loop.
4. **P0-C** (LLM 402) — пополнить баланс/заменить ключ.
5. **P1-D** (selenium timeout/краши), **P1-E** (TG-controller), **P1-F** (логин/селекторы), **P1-G** (yandex warmup).
6. **P2-H, P2-I** — гигиена.

После каждого P0/P1: прогнать `pytest -q` (в проекте ~187 тестов) и `ruff check .`, затем запуск бота и наблюдение логов 1-2 цикла.

---

## 4. Чек-лист проверки «бот здоров»

- [ ] `git log` на сервере == локальный (нет рассинхрона).
- [ ] AdsPower-профиль стартует: в логах `connect_to_sphere: AdsPower chromedriver OK`, нет `session not created`.
- [ ] Нет `no_proxy_warning` для AdsPower-аккаунтов; нет `thread died — restarting`.
- [ ] LLM: тест curl → `200`; в логах outbound `отправлено сообщение`, нет `402`.
- [ ] Нет `run_thread crashed` по `ReadTimeoutError`.
- [ ] TG: админ получает уведомления; нет `TG-controller недоступен` при настроенном TG.
- [ ] Логин: `native session is live` стабильно; при слёте — корректный ручной логин/SMS-эскалация.
- [ ] Нет спама `IP BLOCK DETECTED` (Yandex warmup выключен или прокси живой).
- [ ] `pytest -q` зелёный; `ruff check .` чисто.

---

## 5. Открытые вопросы к владельцу (нужно решить до правок)

1. **Прокси:** локальный `socks5://127.0.0.1:10800` — это туннель, который должен быть поднят на сервере, или артефакт? Через какой прокси аккаунты должны ходить (резидентный/мобильный)? Для AdsPower-аккаунтов прокси настраивается **в профиле AdsPower**, а не в `accounts.json`.
2. **LLM-провайдер:** боевой эндпоинт — `vibecode.moe` (Claude) или `api.coda.ink` (GPT-5.5)? Какой ключ актуален и есть ли на нём баланс?
3. **Версия Chromium в AdsPower:** обновлять до 148 (и драйвер 148) или зафиксировать 147 + драйвер 147? Должны совпадать.
4. **Сколько аккаунтов** планируется гонять (сейчас 1)? От этого зависит, чинить ли multi-account прокси-пул серьёзно.
