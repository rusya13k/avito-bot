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

### T11 — Tab switching (Ctrl+Click)  `status: pending`

Сейчас всё в одной вкладке. ~30% кликов через Ctrl+Click → новая
вкладка → driver.switch_to → close.

---

### T12 — TG-кнопка «🔥 Большой прогрев»  `status: pending`

В `tg_bot.py` добавить кнопку для запуска 30-60 минутного мультисайтового
warmup БЕЗ парсинга/ответов. Полезно после простоев/смены прокси.

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

### T17 — Smarter captcha cooldown политики  `status: pending`

- Разные cooldown по типам капчи (Avito phone vs Yandex SmartCaptcha).
- После 3 капч за день — полный пропуск дня.
- Отключить outbound на 24h после капчи.

---

### T18 — Proxy health probe + auto-rotation  `status: pending`

Перед каждым циклом: ping `api.ipify.org` через прокси. Timeout / wrong
country / banned IP → НЕ запускать профиль, попробовать другой.

---

### T19 — Stagger cycle pauses (lognormal + долгие перерывы)  `status: pending`

Сейчас `Цикл завершён. Пауза 88 мин` — фиксированно. Реализовать:
- Lognormal 30-180 мин с длинными перерывами 4-8h 1-2 раза/день («обед/ужин»).

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
