# Задачи на максимальную живучесть аккаунтов

Цель: продлить лайфтайм Avito-аккаунта до 3-6+ месяцев за счёт более
реалистичного поведения и анти-детекта.

Базовые конвенции (см. `AGENTS.md`): `ruff check .`, `pytest tests/ -q`,
смоук-импорт перед PR. Логика — за фасадом `AvitoClient`, persistence —
через `DatabaseManager`.

Легенда статусов: `pending`, `in_progress`, `done`, `blocked`, `skip`.

---

## 🔴 КРИТИЧНОЕ

### T1 — Typing speed fix (3 «бабушкиных» места)  `status: done`

В коде уже есть нормальный `human_type()` в `bot.py:305` (50-250ms/char
+ 10% «задумчивых» пауз). Но в трёх местах он не используется и стоит
тупой `time.sleep(random.uniform(0.5, 1.5))` per char ≈ 8-12 WPM
(бабушка).

| Файл | Строки | Что |
|---|---|---|
| `bot.py` | 800-803 | yandex_warmup ввод запроса в поисковую строку |
| `bot.py` | 1154-1157 | логин: ввод телефона |
| `bot.py` | 1214-1217 | логин: ввод пароля |

**Реализация**: заменить inline-loop на вызов `human_type(box, query)`
с подходящими `speed_range` (для телефона/пароля чуть медленнее: их
обычно вводят чуть аккуратнее, ~100-350ms/char). Юнит-тесты на
вызовы human_type где надо.

**Verify**: `ruff check .` + `pytest tests/ -q`. Глаза: запустить бот,
посмотреть в AdsPower-Chrome как печатает (быстро, бёрстами).

---

### T2 — Yandex warmup селекторы устарели  `status: done`

`bot.py:809-816` ждёт `//li[contains(@class,'serp-item')] | //a[contains(@class,'organic')]` — на текущем Яндексе CSS поменялся.
Плюс на коммерческих прокси Яндекс сразу показывает `/showcaptcha`.

**Реализация**:
1. Сменить ожидание результатов на актуальные селекторы (исследовать
   текущий Яндекс — `data-testid`, `[data-fast-tag]`, итд).
2. Альтернатива — ходить напрямую `https://yandex.ru/search/?text=...&lr=213` без homepage и его капчи.
3. Тесты на `_pick_queries` уже есть, добавить regression на новый
   путь.

**Verify**: запуск с реальным прокси, лог `Warmup completed (N queries successful)`.

---

### T3 — Warmup не критичен (уже фикс в `bot.py:1801-1812`)  `status: done`

Сделано в текущей сессии: если warmup упал → `WARN` и продолжаем к
Avito (раньше был `return` = смерть потока).

---

## 🟠 ВЫСОКИЙ приоритет

### T4 — Big warmup 2.0 — мульти-сайтовый прогрев  `status: done`

Заменить ya.ru → 2 query → click flow на 8-15 минутный сеанс
по 3-5 нейтральным сайтам:

- Пул: `ya.ru`, `mail.ru`, `dzen.ru`, `lenta.ru`, `vk.com`, `dom.click`,
  `cian.ru` (последние два — тематический шум для коммерческой
  недвижимости).
- На каждом сайте: 30-90s dwell, 2-4 скролла, 1-2 клика по случайной
  ссылке.
- 1-2 поисковых запроса в Яндексе с обновлёнными селекторами.
- Иногда заходим на Avito через органическую выдачу (через bridge_to_avito).

**Реализация**: новый модуль `warmup.py` или метод
`AvitoClient.big_warmup()`. Список сайтов + поведенческие профили
конфигурируются. Логирование durations + успешности.

**Verify**: 1) Тесты на picker сайтов, 2) запуск с реальным прокси
и наблюдение, 3) `/health` показывает healthy после warmup.

---

### T5 — Realistic typing 2.0 — burst + опечатки  `status: done`

Старая `human_type` была равномерно-медленной (uniform sleep per char) —
антифрод детектил по гистограмме. Сделано:

- **Новый модуль `human_typing.py`** с публичным API
  `type_human(element, text, *, speed_range, speed_multiplier, typo_rate,
  enable_bursts, enable_typos, stop_event) -> bool`.
- **Бёрсты**: пачки по 3-5 символов с задержкой 50-200ms, отдых
  180-380ms между бёрстами. Иногда (~5%) — «задумчивая» пауза 300-800ms.
- **Опечатки** (`typo_rate=0.06` по умолчанию): для слов ≥ 3 символов с
  QWERTY/ЙЦУКЕН-соседом — печатается «не та» буква, замечается через
  100-400ms, BACKSPACE, правильная буква. Регистр сохраняется.
- **Цифры / пунктуация ×1.5-2.0** медленнее обычных букв (shift/numpad).
- **Длинные слова (≥ 8 символов) ×0.85** — моторная память.
- **Persona-driven темп**: `persona_speed_multiplier(persona_id)` —
  словарь по PERSONAS из outbound_messenger. Молодые (`retail_starter`,
  `ecommerce_warehouse`, `fitness_studio`) → 0.95×; нейтральные
  (`cafe_owner`, ...) → 1.0×; «солидные» (`investor`, `clinic_medical`,
  `tatarstan_developer`) → 1.10-1.20×.

Миграция всех точек ввода:

| Файл | Что изменилось |
|---|---|
| `bot.py:305` | `human_type` — тонкая обёртка в `_type_human`. Сохранена сигнатура `(element, text, speed_range=...)`. Новые kwargs `speed_multiplier`, `enable_typos`. |
| `bot.py:1245, 1306` | `perform_login` — `enable_typos=False` для phone/password (BACKSPACE рискован для валидаторов). |
| `avito_messenger.py:138` | `human_type` → `_type_human`. `AvitoMessenger.__init__` принимает `persona`, держит `self.typing_speed_multiplier`. `_send_message` передаёт его в `human_type`. |
| `outbound_messenger.py:516-520` | inline-loop с `time.sleep(0.03..0.09)` заменён на `_type_human(speed_multiplier=persona_speed_multiplier(self.persona_id))`. |
| `bot.py:_build_avito_client` | прокидывает `persona` в `messenger_config` (T5). |

`stop_event` поддерживается во всех точках — `/stop` прерывает typing
по сэмплам ≤ 100ms.

**Тесты** (`tests/test_human_typing.py`, 30 шт.): полнота ввода;
BACKSPACE при `typo_rate=1.0`; отсутствие BACKSPACE при `typos=False`
или `typo_rate=0`; цифры/пунктуация медленнее букв; длинные слова
быстрее коротких; speed_multiplier масштабирует total sleep ~линейно;
stop_event прерывает на любом этапе; WPM в диапазоне 25-80;
persona_speed_multiplier для known/unknown/empty.

**Verify**: `ruff check .` ✓, `pytest tests/ -q` → 489 passed,
smoke-import OK.

---

### T6 — Mouse movements (Bezier-траектория)  `status: done`

Раньше клики были двух видов: `element.click()` (Selenium-телепорт,
mousemove не отправляется) и `execute_script("arguments[0].click();", el)`
(JS-клик без вообще каких-либо event'ов мыши). Теперь все клики идут
через `human_mouse.human_click()`:

- **Новый модуль `human_mouse.py`** с публичным API
  `human_move_to(driver, element, *, steps_range, curvature, jitter_px,
  pause_range, overshoot_chance, stop_event)` и
  `human_click(driver, element, *, scroll_into_view, hesitate_chance,
  pre_click_pause, post_click_pause, stop_event)`.
- **Bezier-траектория**: `bezier_path(start, end, num_points, curvature)`
  даёт N точек квадратичной Bezier-кривой со случайным изгибом
  (±curvature × distance, перпендикулярно прямой start→end).
- **Реализация** через Selenium 4 W3C-pointer API:
  `actions.w3c_actions.pointer_action.move_to_location(x, y)` — это
  абсолютные viewport-координаты; каждая точка → настоящее `mousemove`
  событие в браузере.
- **15-30 промежуточных mousemove** на каждый клик (`steps_range=(15,30)`).
- **Jitter ±3 px** вокруг центра элемента (`_element_viewport_target`
  с safety-margin, чтобы клик не уходил за рамки rect).
- **Overshoot/correction**: 10% шанс — курсор «промахивается» на 8-20px
  дальше цели и возвращается через мини-Bezier (4 точки, curvature 0.05).
- **Hesitate**: 10% шанс «прицеливания» — курсор дёрнулся в сторону
  ±30/±20 px, паузнул, вернулся к цели. Имитирует «прицелился, перевёл
  взгляд, кликнул».
- **Last-position tracking**: `_LAST_POS` per `id(driver)` — следующий
  клик стартует с реальной позиции, а не телепортируется. После
  `driver.get()` позицию можно сбросить через `reset_last_pos(driver)`,
  но это не критично — Bezier всё равно построится.
- **Fallback цепочка** в `human_click`: ActionChains-click → native
  `element.click()` → JS `execute_script(arguments[0].click())`. Никогда
  не raise. Это критично для legacy мест — старая семантика сохранена.
- **stop_event** поддерживается на каждом mousemove и между фазами.

Миграция всех точек клика:

| Файл | Что изменилось |
|---|---|
| `bot.py:move_click` | Стал тонкой обёрткой над `human_click(...)` — старая ActionChains-телепорт-реализация удалена. Все 4 вызова `move_click(...)` (избранное, Позвонить, login submit ×2) автоматически получили Bezier. |
| `bot.py:scroll_gallery` | `execute_script("arguments[0].click();", btns[0])` → `_human_click(driver, btns[0])`. |
| `bot.py:perform_login` | `phone_input.click()` и `password_input.click()` → `_human_click(...)`. |
| `commercial_parser.py:get_phone` | `phone_btn.click()` (самый палевный клик) → `_human_click(driver, phone_btn)`. |
| `avito_messenger.py:process_messages` | `execute_script("arguments[0].click();", dialog)` → `_human_click(...)`. |
| `avito_messenger.py:_send_message` | `input_box.click()` и `send_btn.click()` → `_human_click(...)`. |
| `outbound_messenger.py:_open_chat_overlay` | `btn.click()` + JS-fallback → один `_human_click(...)` (он сам делает scrollIntoView и fallback'и). |
| `outbound_messenger.py:_type_and_send` | `input_el.click()` и `send_el.click()` → `_human_click(...)`. |
| `warmup.py:_random_click_link` | `target.click()` → `_human_click(driver, target)`. |

**Тесты** (`tests/test_human_mouse.py`, 29 шт.): endpoint-инварианты
Bezier; кол-во точек строго в `steps_range`; finest-jitter держит точку
внутри rect; `_LAST_POS` запоминается → следующий путь стартует с неё;
overshoot добавляет ≥3 точек; `stop_event` прерывает; рекавери на
WebDriverException (move_to → False, click → fallback'ы); все три
пути fallback тестированы.

**Verify**: `ruff check .` ✓, `pytest tests/ -q` → 518 passed,
smoke-import OK.

---

### T7 — `navigator.webdriver` маскировка через CDP  `status: done`

После `webdriver.Chrome(...)` в `connect_to_sphere` (`bot.py:219`)
теперь регистрируется stealth-скрипт через
`Page.addScriptToEvaluateOnNewDocument` — он выполняется ДО любого JS
каждой загружаемой страницы. Скрипт переопределяет
`navigator.webdriver` через `Object.defineProperty` так, чтобы геттер
возвращал `undefined` (как в обычном Chrome без WebDriver).

**Реализация**: новый модуль `stealth.py` с публичным API:

- `apply_stealth(driver) -> bool` — регистрирует скрипт через CDP.
  Никогда не raise; вернёт False если драйвер не поддерживает CDP
  или CDP-вызов упал. Вызывается из `connect_to_sphere` после
  `webdriver.Chrome(...)`.
- `verify_stealth(driver) -> dict[str, Any]` — диагностика для
  ручной проверки: возвращает `{"webdriver": ..., "cdc_keys": [...],
  "user_agent": ...}` после запроса соответствующих свойств у
  текущей страницы.

**Verify**: `verify_stealth(driver)` после первого `driver.get(...)`
должен показать `{"webdriver": None, ...}` (None ≡ undefined в JSON).

---

### T8 — Cleanup Selenium-маркеров в DOM  `status: done`

В том же stealth-скрипте (T7) — на каждом тике (0ms / 50ms / 200ms
после первого выполнения, плюс immediately) делаем
`Object.getOwnPropertyNames(window).filter(k => k.startsWith('cdc_'))`
и удаляем. Multi-tick нужен потому, что chromedriver может инжектить
свои `cdc_adoQpoasnfa76pfcZLmcfl_*` глобалы ПОСЛЕ выполнения
stealth-скрипта (порядок не гарантирован).

**Verify**: `verify_stealth(driver)["cdc_keys"]` → `[]`.

**Тесты** (`tests/test_stealth.py`, 10 шт.): CDP вызывается с
правильной командой; скрипт содержит `navigator.webdriver` /
`undefined` / `defineProperty` / `cdc_` / `delete` / `startsWith`;
драйвер без CDP → False; WebDriverException → False (без raise);
RuntimeError тоже → False; `verify_stealth` корректно работает с
чистым/грязным браузером и устойчив к ошибкам `execute_script`.

---

## 🟡 СРЕДНИЙ приоритет

### T9 — Scroll inertia + reading pauses  `status: done`

Старый `bot.human_scroll` делал «дёрг-дёрг-дёрг» равными порциями
(`scrollBy(0, 150..500)` + `sleep(0.3..1.1)`) — без инерции, без
зависимости от контента, без длинных reading-пауз. Теперь:

- **Новый модуль `human_scroll.py`** с публичным API:
  `human_scroll(driver, direction, *, swipes, swipe_range,
  back_scroll_chance, reading_pause, stop_event)` и
  `inertia_scroll(driver, amount_px, *, steps_range, pause_range,
  stop_event)`.
- **Inertia-scroll**: один свайп (300-900px) разбивается на 10-20
  микро-шагов с замедлением по cubic ease-out. Между микро-шагами
  10-30ms. Это даёт настоящее «инерционное» движение, какое
  получается от touchpad'а или mouse-wheel.
- **Reading pauses адаптивные** через `visible_text_chars(driver)` —
  JS поверх `getBoundingClientRect` грубо считает, сколько символов
  видимого текста на экране. Чем гуще — тем дольше пауза
  (`reading_time_for_chars`: 30-60 chars/сек, clamp 0.5-30 сек).
  Распределение — lognormal (`human_delay`).
- **15% back-scroll** между свайпами — листаем 100-300px назад,
  «перечитываем кусок».
- **Micro-stops 200-400ms** между свайпами — моторная пауза «палец
  возвращается на колесо».
- **Старая обёртка `bot.human_scroll(direction, iters)`** сохранена
  и теперь делегирует в `human_scroll.human_scroll(swipes=iters)` —
  все 4 вызова в `bot.py` (view_listing, browse_commercial, login flow)
  автоматически получили inertia.

**Тесты** (`tests/test_human_scroll.py`, 31 шт.): cubic ease-out
монотонный + decelerates; sum(scrollBy) ≈ amount_px; первая половина
inertia-свайпа > вторая (deceleration); negative amount → все вверх;
zero amount → no-op; stop_event прерывает; WebDriverException → False;
back_scroll_chance=1.0 даёт реверс-микроскроллы; reading_pause=True
вызывает visible_text_chars per свайп.

---

### T10 — Realistic dwell зависит от контента  `status: done`

`view_listing` теперь рассчитывает dwell по контенту листинга, а не
просто `lognormal(5, 120)`:

- **`compute_reading_dwell(description, image_count, interest)`** в
  `human_scroll.py` — `base × (1 + 0.6·log(text+1)) × (1 + 0.04·images)
  × (0.5 + interest)` с clamp в [5, 300] сек и шумом ±10%.
- **`_read_listing_meta(driver)`** в `bot.py` — best-effort парсит
  description и считает `data-marker='image-frame'`. На любую ошибку
  → `("", 0)` (короткий dwell), не падаем.
- **3-уровневая interest score**:
  - 20% — «совсем не моё» (interest 0.0-0.2). При `< 0.10` — early
    return без scroll/favorite/call.
  - 15% — «очень интересно» (0.7-1.0).
  - 65% — нейтрально (0.3-0.7).
- **Dwell-фазы**: 30% от рассчитанного dwell — на initial_glance,
  50% — на active reading в `scroll_to_desc` (раньше было
  фиксированные 15-90s lognormal независимо от контента), оставшиеся
  20% распределены по scroll/mouse_move/pause фазам.

Профильные dwell:
  - Empty description, 0 фото, interest=0 → ~5 сек (быстро закрыл).
  - Длинное описание, 10 фото, interest=0.5 → ~40 сек (типичный).
  - Длинное описание, 15 фото, interest=1.0 → ~150 сек (зацепило).

**Тесты** (расширен `tests/test_dwell_times.py` под T10, +5 в
`test_human_scroll.py`): early-exit при низком interest;
neutral/interesting flow проходит до конца; `_read_listing_meta`
парсит DOM и handles missing elements; `compute_reading_dwell`
монотонно растёт по text/images/interest, кламп в [base_min, base_max],
interest за пределами [0, 1] кламп'ится.

---

### T11 — Tab switching (Ctrl+Click)  `status: done`

Раньше всё открывалось в одной вкладке (`safe_get` + `driver.back`),
что давало ~0% `window.open` events — антифрод-аномалия. Теперь ~30%
листингов открываются через Ctrl+Click → новая вкладка → close.

- **Новый модуль `tab_switch.py`** с публичным API:
  - `open_in_new_tab_via_click(driver, element, *, stop_event)` —
    `ActionChains.key_down(CTRL) + click + key_up(CTRL)`; новая
    `window_handle` ловится через diff с 3-секундным дедлайном;
    после успеха — `switch_to.window(new_handle)`. На любом сбое →
    False (caller использует fallback).
  - `close_current_tab(driver)` — закрывает текущую вкладку,
    переключается на первую из оставшихся. Защита от случая «одна
    вкладка» (не закрываем, иначе браузер).
  - `new_tab_for_listing(driver, element, *, stop_event)` —
    контекст-менеджер. `with new_tab_for_listing(...) as ok: ...` —
    `ok=True` если открыли в новой вкладке, `ok=False` → caller
    делает fallback на `safe_get` + `driver.back`. На finally
    закрытие вкладки гарантировано (в т.ч. при exception внутри).
- **Интеграция в `bot.py:find_and_view_commercial_listings`**:
  `random.random() < 0.30` решает, открывать в новой вкладке или
  обычной навигацией. Сохранён parity по metrics (`processed_count`,
  `new_listings_count` инкрементируются в обеих ветках).

**Тесты** (`tests/test_tab_switch.py`, 11 шт.): happy path; нет
новой handle → False (без switch); `ActionChains.perform()` крашится
→ False; `stop_event` прерывает; пустые handles → False;
`close_current_tab` со старой вкладкой; one-tab safeguard; finally
закрывает вкладку даже при exception внутри контекста.

**Verify**: `ruff check .` ✓, `pytest tests/ -q` → 583 passed,
smoke-import OK.

---

### T12 — TG-кнопка «🔥 Большой прогрев»  `status: done`

Раньше big_warmup был доступен только в начале `run_thread` для
новых аккаунтов. Теперь — ручной запуск из TG для любого аккаунта
(после простоев / смены прокси / получения капчи).

- **Helper `bot.run_big_warmup_for_account(account, cfg, adspower)`** —
  полный standalone lifecycle: apply proxy (`_apply_account_proxy`) →
  AdsPower `start_profile` + connect → `warmup.big_warmup(num_sites=10,
  with_yandex_search=True)` → driver.quit + `stop_profile`. Возвращает
  dict `{"ok": bool, "stats": {...big_warmup stats...},
  "error": str | None}`.
- **TG-кнопка в `kb_account_detail`** — `🔥 Большой прогрев`, callback
  `acc_bigwarmup_<idx>`.
- **TG-команда `/bigwarmup <name>`** — то же самое из CLI.
- **Двухшаговое подтверждение**: `acc_bigwarmup_<idx>` → kb_confirm с
  предупреждением о длительности (15-30 мин) и блокировке профиля →
  `acc_bigwarmup_ok_<idx>` → старт.
- **Запуск в `threading.Thread(daemon=True)`** — TG-handler не
  блокируется. По завершении — `self.notify(...)` с
  `sites_visited/sites_failed/yandex_ok/duration`.
- **Защита от двойного запуска**: `self._big_warmup_running: set[str]`
  хранит имена аккаунтов с активным фоновым прогревом.
- **Защита от коллизии**: если в `active_threads` есть thread с
  именем `acc-<name>` — отказ (AdsPower-профиль уже занят основным
  циклом).

**Тесты** (`tests/test_tg_bigwarmup.py`, 18 шт.): кнопка
присутствует в kb_account_detail; prefix-routing `acc_bigwarmup_ok_`
выигрывает у `acc_bigwarmup_`; не путается с `acc_detail_`/`acc_del_`;
confirm-step показывает kb_confirm; блок при already running;
блок при active thread; OK-step стартует daemon-thread; double-run
guard; happy path background task; failure path; exception clears
state; `/bigwarmup` no-args/unknown/active-thread/no-access; команда
зарегистрирована в `_setup`.

**Verify**: `ruff check .` ✓, `pytest tests/ -q` → 590 passed,
smoke-import OK.

---

## 🟢 НИЗКИЙ приоритет

### T13 — WebRTC IP leak (проверка AdsPower)  `status: pending`

Chrome через прокси может утечь реальный IP через WebRTC. Проверить
в AdsPower настройках профиля флажок «WebRTC: Replace public IP».

**Verify**: `https://browserleaks.com/webrtc` через бот-Chrome
показывает proxy-IP, не реальный.

---

### T14 — Geo/locale/timezone согласование с прокси  `status: pending`

Прокси в RU → должны совпадать:
- `Accept-Language: ru-RU,ru;q=0.9` ✓
- `Intl.DateTimeFormat().resolvedOptions().timeZone === 'Europe/Moscow'`
- Шрифты RU-Chrome

В AdsPower per-profile. Убедиться. Можно автоматизировать через CDP.

---

### T15 — Cookie + localStorage persistence  `status: pending`

Сейчас `cookies.json` грузится, но `localStorage`/`sessionStorage`
нет → между сессиями теряется search filters / undo-stack.

**Реализация**: dump/load через CDP `Storage.getDOMStorage`.

---

### T16 — Browser history depth  `status: pending`

Реальный пользователь имеет десятки страниц в history. После
warmup посетить 10-20 несвязанных страниц для наполнения.

---

### T17 — Smarter captcha cooldown политики  `status: done`

Раньше все капчи обрабатывались одинаково: фиксированный 30-мин
cooldown + B4 long-cooldown при ≥3 капчах за 24h. Это игнорирует
разницу между Avito phone-капчей (привязка телефона к аккаунту,
самое опасное) и Yandex SmartCaptcha (часто прокси-issue, не
аккаунт-сигнал). T17 разделяет их.

- **Type-specific cooldown multipliers** — `account_state.mark_captcha`
  теперь принимает `captcha_type` (default `"generic"`):
  - `"avito_phone"` (×2.0) — клик «Показать телефон» → капча. Самое
    палевное (бот сам триггернул, привязка к телефону).
  - `"avito_listing"` (×1.5) — капча на странице листинга / browse.
  - `"avito_message_send"` (×1.5) — капча после отправки outbound.
  - `"yandex_search"` (×0.5) — Yandex SmartCaptcha. Часто прокси,
    не аккаунт.
  - `"generic"` (×1.0) — back-compat для старых вызовов.
  - Multiplier применяется к short-cooldown ветке; **B4 long-cooldown
    (≥3/24h → 4-8h) и day-cooldown (≥5/24h → до завтра) остаются
    жёсткими** — их multiplier не трогает.
- **Outbound disable после Avito-капчи** — любая Avito-капча выключает
  outbound (proactive контакты собственникам) на 24h:
  - `_Entry.outbound_disabled_until: float = 0.0` хранит дедлайн.
  - `account_state.is_outbound_disabled(name) -> bool` /
    `get_outbound_disabled_until(name) -> float` — публичный API.
  - **Yandex-капча НЕ блокирует outbound** (это прокси-issue).
  - `mark_captcha` для типов из `_CAPTCHA_TYPES_DISABLE_OUTBOUND` берёт
    `max(prev, now + 24h)` — повторная капча продлевает дедлайн.
  - Дедлайн настраивается через `OUTBOUND_DISABLE_HOURS_AFTER_CAPTCHA`.
    `=0` → disable полностью отключён.
- **Интеграция в `bot._pick_cycle_kind`** — добавлен kwarg
  `outbound_disabled: bool`. При `True` — вес `outbound_only` зануляется
  в копии словаря, остальные kinds разбирают «массу» пропорционально.
- **Интеграция в `bot._run_main_loop`** — перед каждым `_pick_cycle_kind`
  вычисляется `outbound_disabled = account_state.is_outbound_disabled(...)`
  и логируется до какого времени блокировка.
- **Конфиг** через `config.json` (читаются в `configure_from_cfg`):
  - `captcha_cooldown_multipliers: dict[type, float]` — переопределяет
    multiplier'ы. Неизвестные типы / `<=0` — warning + ignore.
  - `outbound_disable_hours_after_captcha: float` — длительность
    disable. Невалидные значения / `<0` — warning + ignore.
- **Обновлены call-sites `mark_captcha`**:
  - `commercial_parser.py:309` — `captcha_type="avito_listing"` (pre-click).
  - `commercial_parser.py:336` — `captcha_type="avito_phone"` (post-click).
  - `outbound_messenger.py:399` — `captcha_type="avito_listing"` (pre-send).
  - `outbound_messenger.py:568` — `captcha_type="avito_message_send"` (post-send).

**Тесты** (`tests/test_smarter_captcha.py`, 28 шт.): default type =
generic; multipliers для всех известных типов; неизвестный → 1.0;
явный `cooldown_minutes` * multiplier; outbound_disable для
avito_phone/avito_listing/avito_message_send/generic; yandex_search
НЕ блокирует и НЕ перезаписывает существующий disable; повторная
капча через час продлевает дедлайн; `expires_after_24h` через
`patch.time`; `_pick_cycle_kind` зануляет outbound_only при
`outbound_disabled=True`; warmup поведение независимо; конфиг
`captcha_cooldown_multipliers` (правильно/неправильно/неизвестный/
отрицательный); `outbound_disable_hours_after_captcha` (valid/0/
negative/invalid); back-compat существующих тестов; B4 long-cooldown
работает с типами.

**Verify**: `ruff check .` ✓, `pytest tests/ -q` → 657 passed,
smoke-import OK.

---

### T18 — Proxy health probe + auto-rotation  `status: done`

Раньше прокси применялся «наобум»: `_apply_account_proxy` вызывал
AdsPower API без проверки, а потом `_connect_with_retry` уже ловил
selenium-таймауты, тратя 30+ сек на мёртвый прокси.

- **Новый модуль `proxy_health.py`** — pure-функции без побочных
  эффектов кроме HTTP. Public API:
  - `parse_proxy(s)` → `(host, port, user, pass)`. Поддерживает
    `host:port`, `host:port:user:pass`, `user:pass@host:port`,
    `scheme://...` (схема отбрасывается).
  - `build_proxy_url(s, scheme="socks5h")` → URL для requests
    proxies dict, с url-encoded user/pass.
  - `redact_proxy(s)` → `host:port` (без креденшелов) для логов.
  - `probe_proxy(s, *, timeout, probe_url, expected_country, ...)`
    → `ProbeResult(ok, ip, country, latency_ms, error)`. Делает
    GET `api.ipify.org` через прокси (через `socks5h://` по
    дефолту — DNS резолвится на стороне прокси). Парсит ответ
    (JSON `{"ip"}` или `{"origin"}` для httpbin, или plain text).
    Опционально дёргает geo-сервис (`ip-api.com`) для проверки
    страны — несовпадение валит probe; если geo сам не ответил —
    soft-pass (не валим из-за внешнего сервиса).
  - `pick_healthy_proxy(candidates, ...)` → перебирает кандидатов,
    возвращает `(proxy_str, ProbeResult)` для первого живого, или
    `(None, last_result)`. Учитывает `max_attempts` (default 5).
- **Интеграция в `bot.py::_apply_account_proxy`** — добавлен
  опциональный параметр `cfg`. Если `cfg=None` — legacy путь
  (back-compat для существующих тестов / вызовов). Если `cfg=dict`
  и `proxy_probe_enabled` (default True) — собираем кандидатов
  (per-account первый + шаффленный proxies.txt, дедуп) и
  через `pick_healthy_proxy` находим живой. AdsPower update_proxy
  вызывается только для прошедшего probe — никаких «вслепую».
- **Также в `_connect_with_retry`** — на rotation после connect
  failure используется новый `_pick_rotation_proxy(cfg, exclude)`,
  который тоже probe-driven. Параметр `exclude` не даёт повторно
  пробовать уже мёртвый прокси из текущей сессии.
- **Опции в config.json** (все опциональны, дефолты в коде):
  `proxy_probe_enabled` (default true),
  `proxy_probe_timeout_sec` (default 10),
  `proxy_probe_url` (default `https://api.ipify.org?format=json`),
  `proxy_expected_country` (default null — не проверять; `"RU"` —
  валим probe если IP не в РФ),
  `proxy_max_probe_attempts` (default 5),
  `proxy_probe_scheme` (default `socks5h`).
- **`requirements.txt`** — `requests` теперь с `[socks]` extras
  (подтягивает `pysocks` для socks5h поддержки в requests).

**Тесты** — 51 шт. покрывают:
- `tests/test_proxy_health.py` (45 шт.): parse_proxy для всех форматов
  (host:port, креды, @-нотация, schema-prefix, password с ':',
  invalid → ValueError); build_proxy_url с/без auth, кастомная схема,
  url-encoding спецсимволов; redact_proxy маскирует креды; probe_proxy
  success path (JSON `ip`, JSON `origin`, plain text), failure paths
  (parse, timeout, ConnectionError, RequestException, non-200,
  no_ip, ip_banned), country check (match/mismatch/case-insensitive/
  softpass-on-geo-fail/snake_case `country_code` поле); pick_healthy_proxy
  first-works/skip-dead/all-fail/respects-max-attempts/empty-candidates;
  редактирование лога (нет пароля в логе).
- `tests/test_proxy.py` (+11 шт.): probe disabled/enabled пути в
  `_apply_account_proxy`, прокидка cfg-параметров,
  per-account первый кандидат, dedup пер-account vs proxies.txt,
  fail-all → ERROR, no-candidates → ERROR, AdsPower-fail-after-probe;
  `_pick_rotation_proxy` legacy/probe/exclude/empty/all-fail.

**Verify**: `ruff check .` ✓, `pytest tests/ -q` → 718 passed,
smoke-import OK.

---

### T19 — Stagger cycle pauses (lognormal + долгие перерывы)  `status: done`

Раньше пауза между циклами была `random.uniform(pause_min*60,
pause_max*60)` (по умолчанию uniform 30-90 мин) — равномерная коробка
без длинных перерывов. Гистограмма пауз — равномерная, fingerprinting
видит «как из генератора».

- **Новый модуль `cycle_pause.py`** с public API
  `pick_cycle_pause(account, cfg, *, account_state, account_name,
  now=None, rng=None) -> tuple[float, str]`. Pure-функция (через
  параметры `now`/`rng` тестируется детерминированно). Side effect —
  при выборе long break дёргает `account_state.record_long_break`.
- **Lognormal** для обычных пауз — sample вычисляется как
  `random.lognormvariate(0, 0.4) * mid * 0.7`, clamp в [pause_min,
  pause_max*2] минут. Это даёт пик ~средины, длинный правый хвост,
  минимум pause_min.
- **Long breaks «обед/ужин»**: 1-2 раза в день — uniform 120-300 мин
  (2-5 часов). Вероятность выпадения:
  - В окне обеда (12:00-14:00) или ужина (18:00-21:00) → 30%.
  - Вне окна → 5% (всё равно бывает, моторная пауза «отвлёкся»).
  Лимит `long_breaks_per_day=2` по умолчанию.
- **Per-account/cfg overrides**: `session_pause_min/max`,
  `long_break_min_min/max_min`, `long_breaks_per_day`,
  `long_break_chance_in_window/out_window`.
- **State**: счётчик `long_breaks_today`/`long_breaks_date` в
  `_Entry` + методы `account_state.count_long_breaks_today(name)` и
  `account_state.record_long_break(name)` с авто-сбросом per-day.
  `record_long_break` вызывается ДО `sleep` — даже при крэше/рестарте
  бот не «забудет», что уже отдыхал.
- **Интеграция в `bot._run_main_loop`** — заменили
  `pause_secs = random.uniform(...)` + `distribution="uniform"` на
  `pick_cycle_pause(...)`. Лог отличает «Длинный перерыв» от
  «Цикл завершён».

**Тесты** (`tests/test_cycle_pause.py`, 39 шт.): _is_meal_window
для всех 24 часов; LUNCH/DINNER не пересекаются; _resolve приоритет
account > cfg > default + skip None; lognormal floor/upper/long-tail/
zero-range; uniform range/zero-range; regular path при
limit_reached/long_breaks_per_day=0; regular pause в нужном
диапазоне; long_break при chance=1.0 + lunch; regular при chance=0;
long break в out-window реже чем в in-window (×3+); record_long_break
вызывается; dinner window тоже триггерит; account overrides длительности;
cfg fallback; defaults без config; integration с настоящим
AccountState: счётчик растёт, авто-сброс при смене даты,
независимые счётчики per-account.

**Verify**: `ruff check .` ✓, `pytest tests/ -q` → 629 passed,
smoke-import OK.

---

### T20 — Behavioral telemetry в `/health`  `status: pending`

В `/health` за 7 дней — гистограммы distribution (паузы, dwell,
скроллы) для ручного аудита pattern.

---

## 🔵 БОНУСНЫЕ (reach goals)

### T21 — CDP network throttling (3G/4G)  `status: pending`

`Network.emulateNetworkConditions` для имитации mobile network у
части аккаунтов.

---

### T22 — Browser plugins / extension list эмуляция  `status: pending`

Через CDP эмуляция популярных расширений (uBlock, AdBlock).

---

### T23 — Persona-driven поведение  `status: pending`

В `accounts.json` уже есть `persona`. Использовать НЕ только для
outbound-текста, но и для:
- Скорости печати (молодой = быстрее, инвестор = медленнее).
- Скролл-стиля.
- Распределения `cycle_kind`.

---

### T24 — ML-детектор «бот ли я выгляжу»  `status: pending`

Записать на 1-2 живых сессиях телеметрию (mousemove events, key
timings, scroll velocity). Сравнить с ботом, померить divergence.

---

## Минимальный must-have пакет (на сейчас)

1. T1 (typing speed fix) — 30 минут ✓ done
2. T5 (typing 2.0 — burst + опечатки) — 1-2 часа ✓ done
3. T4 (big warmup) — 2-3 часа ✓ done
4. T6 (mouse movements) — 2-3 часа ✓ done
5. T7 (stealth JS — navigator.webdriver) — 30 минут ✓ done
6. T8 (stealth JS — cdc_* cleanup) — 30 минут ✓ done
7. T13 (WebRTC проверка) — 5 минут

Покрывает ~80% улучшений живучести. Осталось сделать T13 (это ручная
проверка настройки AdsPower-профиля, не код).
