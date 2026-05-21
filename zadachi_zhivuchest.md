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

### T4 — Big warmup 2.0 — мульти-сайтовый прогрев  `status: pending`

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

### T5 — Realistic typing 2.0 — burst + опечатки  `status: pending`

`human_type` сейчас равномерно-медленный. Улучшения:
- **Бёрсты**: 3-5 символов по 50-100ms подряд, потом пауза 200-400ms.
- **Опечатки**: ~5-8% слов с ошибкой + исправление BACKSPACE.
- **Скорость на цифрах/символах**: чуть медленнее (shift/numpad).
- **Длинные слова чуть быстрее** (моторная память).

**Реализация**: переписать `bot.py:305` `human_type()` + миграция всех
вызовов (`bot.py`, `avito_messenger.py`, `outbound_messenger.py`).
Может задействовать `account.persona` для «темпа печати».

**Verify**: тесты на распределение задержек (среднее WPM 30-60),
наличие BACKSPACE в `events` иногда.

---

### T6 — Mouse movements (Bezier-траектория)  `status: pending`

Сейчас все клики через `execute_script("arguments[0].click();", el)`
= телепорт без курсора. Avito видит «click без mousemove» — сильный
сигнал бота.

**Реализация**:
- Доработать `human_click` (`bot.py:?`): курсор движется через 3-5
  промежуточных точек по Bezier-кривой к цели.
- 10-30px jitter вокруг цели на hover.
- Иногда наводимся → передумываем → уходим в сторону → возвращаемся.

**Verify**: console-injection логгер mousemove events, посчитать
плотность (≥10-30 events на клик).

---

### T7 — `navigator.webdriver` маскировка через CDP  `status: pending`

`connect_to_sphere` (`bot.py:217`) ничего не инжектит. AdsPower
скорее всего сам маскирует, но **проверить** — Avito может детектить.

**Реализация**: после `webdriver.Chrome(...)`:
```python
driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
    "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
})
```

**Verify**: до правки запустить `driver.execute_script("return navigator.webdriver")` → должно быть `True` или `None`. После — гарантированно `undefined`.

---

### T8 — Cleanup Selenium-маркеров в DOM  `status: pending`

Chromedriver инжектит `window.cdc_*` глобалы — палево. AdsPower должен
маскировать, но проверить.

**Реализация**: CDP-инжект:
```js
['cdc_adoQpoasnfa76pfcZLmcfl_Array',
 'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
 'cdc_adoQpoasnfa76pfcZLmcfl_Symbol'].forEach(k => delete window[k]);
```

**Verify**: `driver.execute_script("return Object.keys(window).filter(k => k.startsWith('cdc_'))")` → `[]`.

---

## 🟡 СРЕДНИЙ приоритет

### T9 — Scroll inertia + reading pauses  `status: pending`

`human_scroll` (`bot.py:346`) — фиксированные 0.3-1.1s между скроллами.

**Реализация**:
- Reading pauses пропорциональны видимому тексту (10-30s на абзац).
- Иногда (~15%) скроллим обратно (перечитать).
- Inertia: большой свайп 500-1000px с замедлением.
- Micro-stops 200-400ms между мелкими скроллами.

---

### T10 — Realistic dwell зависит от контента  `status: pending`

Сейчас F9 lognormal не учитывает контент.

**Реализация**: dwell ∝ `f(len(description), len(images), interest_score)`. Высокий interest → 50-300s, низкий → 5-30s.

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

1. T1 (typing speed fix) — 30 минут
2. T5 (typing 2.0 — burst + опечатки) — 1-2 часа
3. T4 (big warmup) — 2-3 часа
4. T6 (mouse movements) — 2-3 часа
5. T7 (stealth JS) — 30 минут
6. T13 (WebRTC проверка) — 5 минут

Покрывает ~80% улучшений живучести.
