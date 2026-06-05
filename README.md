# Avito Commercial Real Estate Bot

Бот для парсинга и обработки объявлений коммерческой недвижимости на Avito.ru
с управлением через Telegram.

Возможности:
- Парсинг листингов в million-cities (несколько потоков, каждый под своим
  AdsPower-профилем).
- Двухстадийная классификация (heuristic → LLM-fallback).
- Автоответы в чатах через LLM.
- Управление через Telegram-бот: запуск/остановка, настройки, логи,
  суточная сводка `/report`.
- Транзакционная запись в SQLite (атомарность multi-step операций).

## Требования

- **Python 3.11+** (типизация и target-version в `pyproject.toml`).
- **AdsPower** локально запущен с настроенным API
  (`http://local.adspower.net:50325`). Профили создаются вручную в самом
  AdsPower; бот только запускает/останавливает профили.
- **Chrome** установлен — webdriver-manager сам скачает совместимый chromedriver.
- (опционально) **OpenAI API key** для классификации/ответов. Без ключа
  бот работает только через эвристический скорер.
- (опционально) **Telegram Bot API token** для управления.

## Установка

```bash
git clone <repo>
cd "new bot"
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

## Конфигурация

### 1. Секреты — через `.env` (предпочтительный путь)

```bash
cp .env.example .env
# отредактируй .env: впиши OPENAI_API_KEY, TELEGRAM_BOT_TOKEN,
# TELEGRAM_ADMIN_ID, ADSPOWER_API_KEY
```

`.env` уже в `.gitignore`. ENV-переменные имеют приоритет над `config.json`,
поэтому секреты можно держать строго в `.env`.

### 2. Структура `config.json`

`config.json` теперь содержит ТОЛЬКО глобальные настройки (без секретов
аккаунтов). Список аккаунтов — в отдельном `accounts.json` (см. ниже).

Минимальный пример (`config.json`, рядом с `bot.py`):

```json
{
  "adspower_api_url": "http://local.adspower.net:50325",
  "adspower_api_key": "",
  "openai_api_key": "",
  "openai_model": "gpt-4o-mini",
  "openai_api_base": "https://api.openai.com/v1",
  "telegram_bot_token": "",
  "telegram_admin_id": 0,
  "threads": 0,
  "captcha_cooldown_minutes": 30
}
```

Поля:

- `adspower_api_url` — URL локального AdsPower API.
- `threads` — лимит потоков; `0` = по числу аккаунтов.
- `captcha_cooldown_minutes` — глобальный default паузы после капчи (A3);
  можно переопределить per-account (см. `accounts.json`).

### 2.1. `accounts.json` (G2)

Список аккаунтов лежит отдельным файлом. `accounts.json` находится в
`.gitignore` (содержит phone/password). Шаблон — `accounts.example.json`.

```json
[
  {
    "name": "main_account",
    "adspower_id": "k1c2utgb",
    "phone": "+79991234567",
    "password": "your-avito-password",
    "cookies_path": "accounts/main_account/cookies.json",
    "enabled": true,
    "captcha_cooldown_minutes": 60
  }
]
```

Поля:

- `name` — обязательный, уникальный. Используется в логах и БД.
- `adspower_id` — id профиля AdsPower (alias: `user_id`, оба поддерживаются).
- `phone` / `password` — для ручного логина (B1).
- `cookies_path` — путь к cookies.json (warm-старт, опционально).
- `enabled` — `false` = скипнуть аккаунт без удаления записи.
- `captcha_cooldown_minutes` — per-account override глобального default'а.

**Backward compatibility:** если `accounts.json` отсутствует, бот читает
устаревший блок `cfg["accounts"]` из `config.json` и логирует deprecation
warning. Перенесите аккаунты в `accounts.json` при удобном случае.

### 3. Логи

Управляются env-переменными:

- `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` (default `INFO`).
- `LOG_FORMAT=human|json` (default `human`). `json` — для prod / парсинга.

## Запуск

### Быстрый старт

```bash
# Активировать окружение (Windows)
.venv\Scripts\activate
# Активировать окружение (Linux/macOS)
source .venv/bin/activate

# Полный запуск (с TG-управлением, если задан telegram_bot_token):
python bot.py
```

Если `telegram_bot_token` задан — бот ждёт команды в Telegram (запуск через
inline-кнопку **▶ Запустить**). Если не задан — стартует сразу.

### Ручное управление (без Telegram)

```bash
# Запуск напрямую (без TG-интерфейса — бот стартует мгновенно)
python bot.py

# Или программно из другого скрипта:
python -c "from bot import main; main()"
```

### Предзапусковые проверки (перед PR / деплоем)

```bash
# 1. Линтер (pycodestyle, pyflakes, isort, pyupgrade)
python -m ruff check .

# 2. Автоформатирование (black-compatible)
python -m ruff format .

# 3. Юнит-тесты (все модули)
python -m pytest tests/ -v

# 4. Smoke-импорт — быстрая проверка, что все модули импортируются
python -c "
import bot, tg_bot, database, commercial_parser
import avito_messenger, avito_client, llm_classifier
import listing_classifier, heuristic_scorer, logging_setup
import env_config, accounts, llm_cache
print('OK')
"
```

### TG-команды (при запущенном боте)

| Команда | Описание |
|---------|----------|
| `/start` / `/menu` | Главное меню управления |
| `/report` | Сводка за сегодня (листинги, диалоги, метрики) |
| `/report all` | Сводка за всё время |
| `/budget` | Остаток дневного бюджета по аккаунту |
| `/health` | Health-score аккаунта (капчи / листинги за 7 дней) |
| `/warmup` | Ручной запуск big_warmup для аккаунта |
| `/lastcaptcha` | Время последней капчи и причина |
| `/stop` | Остановить аккаунт |
| `/skipday` | Принудительно сделать сегодня «выходным» |

### Управление процессом

```bash
# Запуск в фоне (Linux)
nohup python bot.py > bot.log 2>&1 &

# Запуск в фоне (Windows — через PowerShell)
Start-Process python -ArgumentList "bot.py" -WindowStyle Hidden

# Просмотр логов в реальном времени
tail -f bot.log        # Linux
Get-Content bot.log -Wait  # PowerShell
```

### Telegram-команды

- `/start` или `/menu` — главное меню (управление аккаунтами/прокси/
  настройками).
- `/report` — сводка за сегодня (распарсено, классифицировано, диалоги).
- `/report all` — сводка за всё время.
- `/cancel` — отмена текущего диалога ввода.

ERROR/CRITICAL-логи автоматически отправляются админу (E4).

## Verification (что прогнать перед PR)

```bash
# Линтер
python -m ruff check .

# Тесты (84 unit-тестов на critical paths)
python -m pytest tests/

# Smoke-импорт (быстрый sanity check)
python -c "import bot; import tg_bot; print('OK')"
```

## Troubleshooting

### AdsPower API не отвечает
- Запусти AdsPower-приложение и проверь, что в `Settings → API`
  включён локальный API на `127.0.0.1:50325`.
- Проверь `adspower_api_url` в `config.json` (или `ADSPOWER_API_URL` в `.env`).

### Avito изменил селекторы
- В логах появятся `TimeoutException` / `Element not found` от
  `commercial_parser` или `avito_messenger`.
- XPaths собраны в `commercial_parser.py:extract_listing_data` и
  `avito_messenger.py:_handle_current_chat`. Обнови XPath по data-marker.
- После правок — `pytest tests/test_smoke_imports.py` (синтаксис) и
  ручной запуск.

### Капча на «Показать телефон»
- A3 уже встроен: бот детектит SmartCaptcha, помечает листинг
  `parse_status='captcha'` и ставит аккаунт в cooldown.
- Cooldown настраивается через `captcha_cooldown_minutes`.

### `Database is locked` под нагрузкой
- WAL и `busy_timeout=5000` уже включены, плюс write-lock в DatabaseManager.
- Если воспроизводится — убедись, что несколько процессов не пишут в одну
  и ту же `*.db` (один процесс — много потоков — это ок).

### LLM возвращает не-JSON / не работает
- Бот делает fallback на эвристику автоматически (см. `LLMClassifier`).
- Проверь, что `OPENAI_API_KEY` начинается с `sk-` (не `r8_`, это Replicate).

## Структура

```
bot.py                — главный entrypoint, потоки-аккаунты, AdsPower
tg_bot.py             — Telegram-контроллер
avito_client.py       — фасад над всем Selenium-флоу Avito (G1)
accounts.py           — загрузка accounts.json (G2)
database.py           — SQLite + транзакции + metrics (E2)
commercial_parser.py  — парсер листингов
avito_messenger.py    — обработка чатов
heuristic_scorer.py   — эвристика owner/agent
llm_classifier.py     — LLM-fallback + ответы
listing_classifier.py — ансамбль heuristic+LLM
account_state.py      — cooldown / стоп-сигналы по аккаунту
captcha_detect.py     — детект Yandex SmartCaptcha
human_delay.py        — нормально-распределённые паузы
logging_setup.py      — единый logger + TG-handlers
env_config.py         — загрузка .env / override config.json
classification_config.py — веса/пороги эвристики
prompts/              — шаблоны LLM-промптов
tests/                — pytest unit-тесты
```

## Лицензия

Используйте на свой страх и риск, соблюдая правила Avito.

---

## Деплой на Hostkey VDS

Сервер: Ubuntu 24.04, **тариф v2-nano** (2 vCPU / 4 GB RAM / 60 GB NVMe), 720₽/мес.  
3 аккаунта Avito с AdsPower.

### 1. Создать VDS

**Тариф:** v2-nano (720₽/мес) — оплата за год.  
**ОС:** Ubuntu 24.04.  
**Регион:** Москва.

При создании добавить SSH-ключ (см. ниже).

### 2. SSH-ключ (создать если нет)

**Windows (PowerShell):**
```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\avito_bot"
```
Файлы:
- Приватный: `C:\Users\USER\.ssh\avito_bot` — никому не показывать
- Публичный: `C:\Users\USER\.ssh\avito_bot.pub` — скопировать содержимое в панель Hostkey при создании сервера

Подключаться:
```bash
ssh -i C:\Users\USER\.ssh\avito_bot root@<IP-сервера>
```

### 3. Войти и запустить deploy

```bash
ssh root@<IP-сервера>
apt-get update -qq && apt-get install -y -qq git curl
git clone https://github.com/rusya13k/avito-bot.git /opt/avito-bot
cd /opt/avito-bot && bash deploy.sh
```

### 4. Заполнить секреты

```bash
nano /opt/avito-bot/.env
```

Обязательно: `DEEPSEEK_API_KEY=sk-...`

### 5. Создать 3 профиля в AdsPower → записать в accounts.json

```bash
nano /opt/avito-bot/accounts.json
```

### 6. Запустить

```bash
systemctl start xvfb      # виртуальный дисплей
sleep 3
systemctl start adsower   # AdsPower
sleep 30                  # ждать загрузку профилей
systemctl start avito-bot # бот
```

### 7. Проверить

```bash
journalctl -u avito-bot -f
```
