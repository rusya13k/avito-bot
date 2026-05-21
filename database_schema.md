# Database Schema

## Tables

### listings
- id (INTEGER, PRIMARY KEY)
- url (TEXT, UNIQUE) - URL of the listing
- title (TEXT) - Listing title (A2)
- category (TEXT) - Real estate category
- area (REAL) - Area in square meters (NULL, если не распарсен)
- price (REAL) - Price of the listing (NULL, если не распарсен)
- location (TEXT) - Location of the property
- description (TEXT) - Description text
- seller_name (TEXT) - Имя продавца (A2)
- profile_id (TEXT) - Avito profile id продавца (A2, ссылается на avito_accounts.profile_id)
- profile_url (TEXT) - Полный URL профиля продавца (A2)
- phone (TEXT) - Нормализованный телефон продавца (A2, ссылается на phones.phone_normalized)
- active_listings_count (INTEGER) - Активных листингов у продавца в момент парсинга (A2)
- photo_urls (TEXT) - JSON-список src первых фото (A2)
- parse_status (TEXT) - Статус парсинга: ok / captcha / error / skipped (A3)
- parse_status_at (DATETIME) - Когда был выставлен parse_status (A3)
- date_parsed (DATETIME) - When the listing was parsed
- date_published (DATETIME) - When the listing was published on Avito
- date_scraped (DATETIME) - When the listing was scraped
- classification (TEXT) - owner / agent / uncertain
- classification_confidence (REAL) - 0..1
- classification_source (TEXT) - heuristic / llm / manual
- classification_at (DATETIME)

### avito_accounts
- profile_id (TEXT, PRIMARY KEY) - Profile ID on Avito
- name (TEXT) - Name of the seller
- active_listings_count (INTEGER) - Number of active listings
- registration_date (DATE) - Registration date if available
- score (REAL) - Aggregated score for the account
- classification (TEXT) - owner / agent / uncertain
- classification_confidence (REAL)
- classification_source (TEXT)
- classification_at (DATETIME)

### phones
- id (INTEGER, PRIMARY KEY)
- phone_normalized (TEXT, UNIQUE) - Normalized phone number (+7XXXXXXXXXX)
- listing_count (INTEGER) - Count of listings associated with this phone
- score (REAL) - Scoring for the phone

### dialogs
- id (INTEGER, PRIMARY KEY)
- listing_id (INTEGER, FOREIGN KEY references listings(id))
- visitor_id (TEXT) - Идентификатор клиента (сейчас = visitor_name; TODO B3: сделать устойчивым)
- our_account (TEXT) - Our sending account
- status (TEXT) - Status of the dialog
- date_started (DATETIME) - When the dialog started
- last_message_text (TEXT)
- last_message_time (DATETIME)

### messages
- id (INTEGER, PRIMARY KEY)
- dialog_id (INTEGER, FOREIGN KEY references dialogs(id))
- direction (TEXT) - in/out
- text (TEXT) - message text
- timestamp (DATETIME)
- classification (TEXT) - message classification

### metrics (E2)
Счётчики событий per account / per hour. Атомарно инкрементятся через
`DatabaseManager.incr_metric(account_name, metric, by=1, ts=None, cursor=None)`,
читаются через `get_metrics(...)` и `get_metric_value(...)`, плюс агрегируются
в `get_daily_summary()`.

- id (INTEGER, PRIMARY KEY)
- account_name (TEXT, NOT NULL, default '') — '' для глобальных метрик
  (например, `llm_errors`, не привязанные к конкретному аккаунту)
- metric (TEXT, NOT NULL) — имя счётчика; известные значения:
  `listings_parsed`, `listings_ok`, `listings_captcha`, `listings_error`,
  `listings_skipped`, `dialogs_handled`, `messages_sent`, `llm_errors`,
  `captcha_hits`. Реальная схема ничего не валидирует — `incr_metric` примет
  любую строку. Константы есть на классе: `DatabaseManager.METRIC_*`.
- bucket_hour (TEXT, NOT NULL) — начало часового бакета в формате
  `'YYYY-MM-DD HH:00:00'`; лексикографически сортируется и удобно
  фильтруется через `LIKE 'YYYY-MM-DD%'` для дневной агрегации.
- value (INTEGER, NOT NULL, default 0)
- UNIQUE(account_name, metric, bucket_hour) — обеспечивает идемпотентность
  UPSERT'а (`INSERT ... ON CONFLICT DO UPDATE SET value = value + excluded.value`).

### behavioral_samples (T20)
Отдельные значения поведенческих событий — для percentile-аудита pattern'а
(распределение пауз, dwell, скроллов). В отличие от `metrics` (часовой
counter), здесь хранится КАЖДОЕ значение, иначе теряется distribution.
Записи идут через `record_behavioral_sample(...)`, читаются через
`get_behavioral_samples(...)` и `get_behavioral_stats(...)` (последний
возвращает count/min/max/mean/median/p25/p75/p95/stddev/histogram).

- id (INTEGER, PRIMARY KEY AUTOINCREMENT)
- account_name (TEXT, NOT NULL, default '') — '' для глобальных
- event_type (TEXT, NOT NULL) — `cycle_pause_sec` | `long_break_sec` |
  `dwell_sec` (расширяется по мере добавления hook'ов)
- value (REAL, NOT NULL) — само значение (sec / count / px / ...)
- ts (REAL, NOT NULL) — unix-time момента записи

## Indexes
- idx_listings_url ON listings(url)
- idx_listings_profile_id ON listings(profile_id)    (A2)
- idx_listings_phone ON listings(phone)              (A2)
- idx_accounts_profile_id ON avito_accounts(profile_id)
- idx_phones_normalized ON phones(phone_normalized)
- idx_dialogs_listing_id ON dialogs(listing_id)
- idx_messages_dialog_id ON messages(dialog_id)
- idx_dialogs_account_visitor ON dialogs(our_account, visitor_id, listing_id) (B3)
- idx_metrics_bucket ON metrics(bucket_hour, account_name, metric) (E2)
- idx_bsamples_account_event_ts ON behavioral_samples(account_name, event_type, ts) (T20)

## Relationships
- listings(profile_id) logically references avito_accounts(profile_id) (без FK-constraint в SQL — мягкая связь через TEXT)
- listings(phone) logically references phones(phone_normalized) (без FK)
- dialogs(listing_id) references listings(id)
- messages(dialog_id) references dialogs(id)

## Миграция
Новые колонки добавляются идемпотентно через `DatabaseManager._migrate_database()`
(вызывается автоматически при старте) и/или через standalone-скрипт
`database_migration.py`.
