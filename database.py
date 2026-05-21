import datetime
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager

# E1: модульный logger — без него ошибки в БД (rollback, lock-busy и т.п.)
# нельзя было увидеть в общем потоке логов.
logger = logging.getLogger(__name__)

# Database module for handling SQLite operations


class DatabaseManager:
    def __init__(self, db_path="avito_bot.db"):
        """
        Initialize database manager with the updated schema
        """
        self.db_path = db_path
        # Один лок на запись помогает избежать "database is locked" под нагрузкой
        # даже при включённом WAL (особенно на Windows / OneDrive).
        self._write_lock = threading.Lock()
        self.init_database()
        self._migrate_database()

    # ──────────────────────────────────────────────────────────────────────
    # Соединение
    # ──────────────────────────────────────────────────────────────────────

    def _connect(self):
        """
        Открывает новое соединение с включёнными WAL и таймаутом ожидания.
        Использовать ТОЛЬКО как менеджер контекста через self._cursor().
        """
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False,
        )
        # WAL — лучшая стратегия для конкурентного чтения/записи.
        # busy_timeout ждёт занятую БД вместо немедленного OperationalError.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _cursor(self, write=False):
        """
        Контекст-менеджер: открывает соединение, отдаёт cursor,
        коммитит при штатном выходе, откатывает при исключении и всегда закрывает.
        write=True — берём write-lock на время операции.
        """
        if write:
            self._write_lock.acquire()
        conn = self._connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            if write:
                self._write_lock.release()

    @contextmanager
    def _with_cursor(self, write=False, cursor=None):
        """
        C4: общий хелпер для методов, которые умеют работать как в одиночку,
        так и в составе внешней транзакции.

        - cursor is None: открываем своё соединение через self._cursor(write),
          оно само коммитит/откатывает.
        - cursor задан: используем его как есть. Коммит/rollback сделает
          внешний transaction(), мы здесь НЕ закрываем и НЕ коммитим.
        """
        if cursor is not None:
            yield cursor
            return
        with self._cursor(write=write) as cur:
            yield cur

    @contextmanager
    def transaction(self):
        """
        C4: публичная атомарная транзакция.

        Все write-операции, выполненные через переданный cursor, либо
        коммитятся вместе при штатном выходе, либо откатываются целиком
        при любом исключении. Используется в save_listing_to_db и при
        обновлении диалога после успешной отправки сообщения, чтобы
        падение между шагами не оставляло БД в полу-записанном состоянии.

        Пример:
            with db.transaction() as cur:
                db.upsert_listing(..., cursor=cur)
                db.upsert_phone(..., cursor=cur)
        """
        with self._cursor(write=True) as cur:
            yield cur

    # ──────────────────────────────────────────────────────────────────────
    # Инициализация и миграция
    # ──────────────────────────────────────────────────────────────────────

    def init_database(self):
        """
        Initialize the database with required tables
        """
        with self._cursor(write=True) as cursor:
            # Create tables according to the schema
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE,
                    title TEXT,
                    category TEXT,
                    area REAL,
                    price REAL,
                    location TEXT,
                    description TEXT,
                    seller_name TEXT,
                    profile_id TEXT,
                    profile_url TEXT,
                    phone TEXT,
                    active_listings_count INTEGER,
                    photo_urls TEXT,
                    parse_status TEXT,
                    parse_status_at TEXT,
                    date_parsed TEXT,
                    date_published TEXT,
                    date_scraped TEXT,
                    classification TEXT,
                    classification_confidence REAL,
                    classification_source TEXT,
                    classification_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS avito_accounts (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT,
                    active_listings_count INTEGER DEFAULT 0,
                    registration_date TEXT,
                    score REAL,
                    classification TEXT,
                    classification_confidence REAL,
                    classification_source TEXT,
                    classification_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS phones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone_normalized TEXT UNIQUE,
                    listing_count INTEGER DEFAULT 0,
                    score REAL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS dialogs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id INTEGER,
                    visitor_id TEXT,
                    our_account TEXT,
                    status TEXT,
                    date_started TEXT,
                    last_message_text TEXT,
                    last_message_time TEXT,
                    FOREIGN KEY(listing_id) REFERENCES listings(id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dialog_id INTEGER,
                    direction TEXT,
                    text TEXT,
                    timestamp TEXT,
                    classification TEXT,
                    FOREIGN KEY(dialog_id) REFERENCES dialogs(id)
                )
            """)

            # E2: счётчики per account / per hour. Используем UNIQUE
            # (account_name, metric, bucket_hour) + UPSERT в incr_metric,
            # чтобы атомарно прибавлять без race condition при многопоточной
            # записи. account_name="" — глобальная метрика (например,
            # llm_errors, не привязанные к конкретному аккаунту).
            # bucket_hour хранится строкой 'YYYY-MM-DD HH:00:00' —
            # лексикографически сортируется и удобно фильтровать по
            # 'LIKE "YYYY-MM-DD%"' для дневной агрегации.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL DEFAULT '',
                    metric TEXT NOT NULL,
                    bucket_hour TEXT NOT NULL,
                    value INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(account_name, metric, bucket_hour)
                )
            """)

            # T20: поведенческие сэмплы (cycle_pause, dwell, scroll, ...).
            # В отличие от metrics (часовые counter'ы), здесь храним отдельные
            # значения — нужно для percentile-аудита: «у меня дисперсия пауз
            # как у бота-генератора?». Не агрегируем в bucket, чтобы не
            # терять distribution.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS behavioral_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    value REAL NOT NULL,
                    ts REAL NOT NULL
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_bsamples_account_event_ts "
                "ON behavioral_samples(account_name, event_type, ts)"
            )

            # C3: лог капча-инцидентов (url, action, тип капчи, момент).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS captcha_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    page_url TEXT,
                    action TEXT,
                    captcha_type TEXT
                )
            """)

            # H1: журнал outbound-контактов (бот пишет первым собственнику).
            # Главное: GLOBAL dedup по profile_id — никогда не пишем одному
            # собственнику ДВАЖДЫ, даже разными аккаунтами. Это критично для
            # "стелс" — собственник видит спам только если 2+ аккаунтов
            # пишут ему одно и то же предложение. UNIQUE(profile_id) делает
            # это защитой на уровне БД, а не только на уровне приложения.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS outbound_contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT NOT NULL UNIQUE,
                    account_name TEXT NOT NULL,
                    listing_id INTEGER,
                    listing_url TEXT,
                    contacted_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sent',
                    persona TEXT,
                    message_text TEXT,
                    FOREIGN KEY(listing_id) REFERENCES listings(id)
                )
            """)

            # Create indexes for better performance.
            # ВАЖНО: индексы по "новым" колонкам (profile_id, phone) создаются
            # в _migrate_database() ПОСЛЕ ALTER TABLE, чтобы не падать на
            # старых БД, где этих колонок ещё нет.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_listings_url ON listings(url)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_accounts_profile_id ON avito_accounts(profile_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_phones_normalized ON phones(phone_normalized)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_dialogs_listing_id ON dialogs(listing_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_dialog_id ON messages(dialog_id)"
            )

    def _migrate_database(self):
        """
        Идемпотентная миграция: добавляет новые колонки в существующие БД.
        Безопасно вызывать на каждый старт.
        """
        # (table, column, type)
        new_columns = [
            # --- A2: ранее парсились, но терялись ---
            ("listings", "title", "TEXT"),
            ("listings", "seller_name", "TEXT"),
            ("listings", "profile_id", "TEXT"),
            ("listings", "profile_url", "TEXT"),
            ("listings", "phone", "TEXT"),
            ("listings", "active_listings_count", "INTEGER"),
            ("listings", "photo_urls", "TEXT"),
            # --- A3: статус парсинга листинга (ok / captcha / error / skipped) ---
            ("listings", "parse_status", "TEXT"),
            ("listings", "parse_status_at", "TEXT"),
            # --- classification ---
            ("listings", "classification", "TEXT"),
            ("listings", "classification_confidence", "REAL"),
            ("listings", "classification_source", "TEXT"),
            ("listings", "classification_at", "TEXT"),
            ("avito_accounts", "classification", "TEXT"),
            ("avito_accounts", "classification_confidence", "REAL"),
            ("avito_accounts", "classification_source", "TEXT"),
            ("avito_accounts", "classification_at", "TEXT"),
            ("dialogs", "visitor_id", "TEXT"),
            ("dialogs", "last_message_text", "TEXT"),
            ("dialogs", "last_message_time", "TEXT"),
        ]
        with self._cursor(write=True) as cursor:
            for table, column, col_type in new_columns:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                except sqlite3.OperationalError as e:
                    # Уже существует — пропускаем
                    if "duplicate column name" not in str(e).lower():
                        # Любая другая ошибка — пробрасываем
                        raise

            # Индексы для новых колонок (идемпотентно)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_profile_id ON listings(profile_id)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_listings_phone ON listings(phone)")
            # B3: ускоряет get_dialog по (our_account, visitor_id[, listing_id])
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_dialogs_account_visitor "
                "ON dialogs(our_account, visitor_id, listing_id)"
            )

            # E2: ускоряет get_metrics по (since, account, metric).
            # На старых БД, где init_database уже был вызван, таблица
            # metrics создаётся именно отсюда (CREATE TABLE IF NOT EXISTS
            # выше — повторяем здесь, чтобы миграция была самодостаточна).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL DEFAULT '',
                    metric TEXT NOT NULL,
                    bucket_hour TEXT NOT NULL,
                    value INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(account_name, metric, bucket_hour)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_bucket "
                "ON metrics(bucket_hour, account_name, metric)"
            )

            # T20: behavioral_samples — миграция для старых БД.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS behavioral_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    value REAL NOT NULL,
                    ts REAL NOT NULL
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_bsamples_account_event_ts "
                "ON behavioral_samples(account_name, event_type, ts)"
            )

            # H1: для старых БД создаём outbound_contacts через миграцию.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS outbound_contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT NOT NULL UNIQUE,
                    account_name TEXT NOT NULL,
                    listing_id INTEGER,
                    listing_url TEXT,
                    contacted_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sent',
                    persona TEXT,
                    message_text TEXT,
                    FOREIGN KEY(listing_id) REFERENCES listings(id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbound_account "
                "ON outbound_contacts(account_name, contacted_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbound_listing ON outbound_contacts(listing_id)"
            )

            # C3: captcha_log — идемпотентно для старых БД
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS captcha_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    page_url TEXT,
                    action TEXT,
                    captcha_type TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_captcha_log_account "
                "ON captcha_log(account_name, ts)"
            )

    # ──────────────────────────────────────────────────────────────────────
    # Listings
    # ──────────────────────────────────────────────────────────────────────

    def upsert_listing(
        self,
        url,
        category,
        area,
        price,
        location,
        description,
        date_parsed,
        date_published,
        date_scraped,
        title=None,
        seller_name=None,
        profile_id=None,
        profile_url=None,
        phone=None,
        active_listings_count=None,
        photo_urls=None,
        cursor=None,
    ):
        """
        Insert or update a listing in the database.

        Важно:
        - при повторном парсинге того же URL обновляются ТОЛЬКО парсинговые
          поля. Поля classification_* сохраняются, чтобы не терять результат
          классификации.
        - если парсер не достал какое-то поле (None) — оно НЕ затирает
          уже имеющееся значение в БД (COALESCE).
        - photo_urls может быть list/tuple — сохраняем как JSON TEXT.

        C4: параметр cursor позволяет вызвать метод в составе внешней
        транзакции (db.transaction()), чтобы upsert листинга, телефона и
        счёта продавца коммитились атомарно.

        Возвращает listing_id (int).
        """
        # photo_urls: list -> JSON string
        if photo_urls is not None and not isinstance(photo_urls, str):
            try:
                photo_urls = json.dumps(list(photo_urls), ensure_ascii=False)
            except (TypeError, ValueError):
                photo_urls = None

        with self._with_cursor(write=True, cursor=cursor) as cursor:
            cursor.execute(
                """
                INSERT INTO listings
                (url, title, category, area, price, location, description,
                 seller_name, profile_id, profile_url, phone,
                 active_listings_count, photo_urls,
                 date_parsed, date_published, date_scraped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title=COALESCE(excluded.title, listings.title),
                    category=COALESCE(excluded.category, listings.category),
                    area=COALESCE(excluded.area, listings.area),
                    price=COALESCE(excluded.price, listings.price),
                    location=COALESCE(excluded.location, listings.location),
                    description=COALESCE(excluded.description, listings.description),
                    seller_name=COALESCE(excluded.seller_name, listings.seller_name),
                    profile_id=COALESCE(excluded.profile_id, listings.profile_id),
                    profile_url=COALESCE(excluded.profile_url, listings.profile_url),
                    phone=COALESCE(excluded.phone, listings.phone),
                    active_listings_count=COALESCE(excluded.active_listings_count,
                                                    listings.active_listings_count),
                    photo_urls=COALESCE(excluded.photo_urls, listings.photo_urls),
                    date_parsed=COALESCE(excluded.date_parsed, listings.date_parsed),
                    date_published=COALESCE(excluded.date_published, listings.date_published),
                    date_scraped=COALESCE(excluded.date_scraped, listings.date_scraped)
            """,
                (
                    url,
                    title,
                    category,
                    area,
                    price,
                    location,
                    description,
                    seller_name,
                    profile_id,
                    profile_url,
                    phone,
                    active_listings_count,
                    photo_urls,
                    date_parsed,
                    date_published,
                    date_scraped,
                ),
            )

            # Получаем id (новой или существующей строки) — удобно вызывающим.
            cursor.execute("SELECT id FROM listings WHERE url = ?", (url,))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_listings_count(self):
        """
        Get the total number of listings in the database
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM listings")
            return cursor.fetchone()[0]

    def get_daily_summary(self, since_date: str) -> dict:
        """
        E3: агрегированная статистика для TG /report — по дню или часу.

        since_date — строка вида 'YYYY-MM-DD HH:MM:SS' (lower bound,
        inclusive). Возвращает dict со счётчиками:
            listings_parsed, listings_ok, listings_captcha, listings_error,
            classified_owner, classified_agent, classified_uncertain,
            dialogs_active, messages_total
        """
        out: dict = {
            "since": since_date,
            "listings_parsed": 0,
            "listings_ok": 0,
            "listings_captcha": 0,
            "listings_error": 0,
            "classified_owner": 0,
            "classified_agent": 0,
            "classified_uncertain": 0,
            "dialogs_active": 0,
            "messages_total": 0,
            # E2: счётчики из metrics-таблицы; заполняются ниже.
            "llm_errors": 0,
            "captcha_hits": 0,
            "dialogs_handled": 0,
            "messages_sent": 0,
        }
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN parse_status='ok' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN parse_status='captcha' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN parse_status='error' THEN 1 ELSE 0 END) "
                "FROM listings WHERE date_parsed >= ?",
                (since_date,),
            )
            row = cur.fetchone() or (0, 0, 0, 0)
            out["listings_parsed"] = row[0] or 0
            out["listings_ok"] = row[1] or 0
            out["listings_captcha"] = row[2] or 0
            out["listings_error"] = row[3] or 0

            cur.execute(
                "SELECT classification, COUNT(*) FROM listings "
                "WHERE classification_at >= ? GROUP BY classification",
                (since_date,),
            )
            for cls, n in cur.fetchall():
                key = f"classified_{cls or 'uncertain'}"
                if key in out:
                    out[key] = n

            cur.execute(
                "SELECT COUNT(*) FROM dialogs WHERE COALESCE(last_message_time, date_started) >= ?",
                (since_date,),
            )
            out["dialogs_active"] = (cur.fetchone() or (0,))[0] or 0

            cur.execute(
                "SELECT COUNT(*) FROM messages WHERE timestamp >= ?",
                (since_date,),
            )
            out["messages_total"] = (cur.fetchone() or (0,))[0] or 0

            # E2: подмешиваем счётчики из metrics за тот же период.
            # `since_date` имеет формат 'YYYY-MM-DD HH:MM:SS' и
            # сравнивается лексикографически с bucket_hour (тоже строкой).
            cur.execute(
                "SELECT metric, SUM(value) FROM metrics WHERE bucket_hour >= ? GROUP BY metric",
                (since_date,),
            )
            counters = {m: int(v or 0) for m, v in cur.fetchall()}
            # Только те метрики, которые имеет смысл показывать в общей
            # сводке. Остальные доступны через get_metrics(...).
            out["llm_errors"] = counters.get(self.METRIC_LLM_ERRORS, 0)
            out["captcha_hits"] = counters.get(self.METRIC_CAPTCHA_HITS, 0)
            out["dialogs_handled"] = counters.get(self.METRIC_DIALOGS_HANDLED, 0)
            out["messages_sent"] = counters.get(self.METRIC_MESSAGES_SENT, 0)

        return out

    # ──────────────────────────────────────────────────────────────────────
    # E2: Metrics (счётчики per account / per hour)
    # ──────────────────────────────────────────────────────────────────────

    # Известные метрики (для документации; БД ничего не валидирует —
    # incr_metric примет любую строку).
    METRIC_LISTINGS_PARSED = "listings_parsed"
    METRIC_LISTINGS_OK = "listings_ok"
    METRIC_LISTINGS_CAPTCHA = "listings_captcha"
    METRIC_LISTINGS_ERROR = "listings_error"
    METRIC_LISTINGS_SKIPPED = "listings_skipped"
    METRIC_DIALOGS_HANDLED = "dialogs_handled"
    METRIC_MESSAGES_SENT = "messages_sent"
    METRIC_LLM_ERRORS = "llm_errors"
    METRIC_CAPTCHA_HITS = "captcha_hits"
    METRIC_PHONE_CLICKS = "phone_clicks"  # A3: клики "Показать телефон"
    # K2: ответы LLM, заблокированные sanitize_llm_reply (телефон / @telegram /
    # wa.me / email / слишком длинно/коротко). Высокое значение этой метрики
    # ⇒ LLM регулярно тянет в чат опасный контент (либо jailbreak, либо
    # prompt-injection из листингов) — повод понизить temperature/менять промпт.
    METRIC_LLM_RESPONSE_BLOCKED = "llm_response_blocked"

    @staticmethod
    def _bucket_hour(ts: float | None = None) -> str:
        """
        Округляет момент времени до начала часа в формате 'YYYY-MM-DD HH:00:00'.
        Это и ключ группировки, и сортируемая строка.
        """
        if ts is None:
            ts = time.time()
        return time.strftime("%Y-%m-%d %H:00:00", time.localtime(ts))

    def incr_metric(
        self, account_name: str, metric: str, by: int = 1, ts: float | None = None, cursor=None
    ) -> None:
        """
        E2: атомарно прибавляет `by` к счётчику (account, metric, current_hour).

        - account_name="" допустим (используем для глобальных счётчиков:
          llm_errors, etc).
        - by может быть отрицательным — но обычно это +1.
        - ts (unix-time) — для тестов/бэкфилла; по умолчанию текущий момент.
        - cursor — позволяет атомарно влить инкремент в общую транзакцию
          (например, save_listing_to_db: парсинг + метрика коммитятся вместе).

        UPSERT через ON CONFLICT — совместим с SQLite >= 3.24.
        """
        if not metric:
            return  # пустое имя метрики — не пишем
        bucket = self._bucket_hour(ts)
        with self._with_cursor(write=True, cursor=cursor) as cur:
            cur.execute(
                """
                INSERT INTO metrics (account_name, metric, bucket_hour, value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_name, metric, bucket_hour)
                DO UPDATE SET value = value + excluded.value
                """,
                (account_name or "", metric, bucket, int(by)),
            )

    def get_metric_value(self, account_name: str, metric: str, ts: float | None = None) -> int:
        """
        Текущее значение конкретного счётчика в часовом бакете.
        Удобно для тестов и быстрых проверок. Если записи нет — 0.
        """
        bucket = self._bucket_hour(ts)
        with self._cursor() as cur:
            cur.execute(
                "SELECT value FROM metrics "
                "WHERE account_name = ? AND metric = ? AND bucket_hour = ?",
                (account_name or "", metric, bucket),
            )
            row = cur.fetchone()
            return row[0] if row else 0

    def get_metrics(
        self,
        since: str | None = None,
        until: str | None = None,
        account_name: str | None = None,
        metric: str | None = None,
        group_by: str = "hour",
    ) -> list[dict]:
        """
        E2: гибкий запрос метрик с агрегацией.

        Args:
            since / until — границы по bucket_hour ('YYYY-MM-DD HH:MM:SS').
                since включительно, until — исключительно.
            account_name — None => все аккаунты; "" => только глобальные.
            metric — None => все метрики.
            group_by:
                "hour"   — по часу (без агрегации, как лежит)
                "day"    — суммировать в дневные бакеты
                "metric" — суммировать в один ряд per (account, metric)
                "account_metric" — alias для "metric"

        Возвращает список dict'ов с ключами в зависимости от group_by:
            hour:           account_name, metric, bucket, value
            day:            account_name, metric, bucket (YYYY-MM-DD), value
            metric:         account_name, metric, value
        """
        where = []
        params: list = []
        if since:
            where.append("bucket_hour >= ?")
            params.append(since)
        if until:
            where.append("bucket_hour < ?")
            params.append(until)
        if account_name is not None:
            where.append("account_name = ?")
            params.append(account_name)
        if metric is not None:
            where.append("metric = ?")
            params.append(metric)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        if group_by == "hour":
            sql = (
                "SELECT account_name, metric, bucket_hour AS bucket, value "
                f"FROM metrics {where_sql} "
                "ORDER BY bucket_hour, account_name, metric"
            )
        elif group_by == "day":
            sql = (
                "SELECT account_name, metric, "
                "substr(bucket_hour, 1, 10) AS bucket, "
                "SUM(value) AS value "
                f"FROM metrics {where_sql} "
                "GROUP BY account_name, metric, bucket "
                "ORDER BY bucket, account_name, metric"
            )
        elif group_by in ("metric", "account_metric"):
            sql = (
                "SELECT account_name, metric, SUM(value) AS value "
                f"FROM metrics {where_sql} "
                "GROUP BY account_name, metric "
                "ORDER BY account_name, metric"
            )
        else:
            raise ValueError(f"group_by must be one of hour/day/metric, got {group_by!r}")

        with self._cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ──────────────────────────────────────────────────────────────────────
    # T20: Behavioral samples — отдельные значения для percentile-аудита
    # ──────────────────────────────────────────────────────────────────────

    def record_behavioral_sample(
        self,
        account_name: str,
        event_type: str,
        value: float,
        ts: float | None = None,
        cursor=None,
    ) -> None:
        """
        T20: записать одно поведенческое значение (cycle_pause_sec, dwell_sec,
        scroll_count, ...).

        В отличие от incr_metric (часовой counter), здесь храним каждый sample
        отдельно — это нужно для percentile-аудита: «у меня дисперсия пауз
        как у бота-генератора или как у живого человека?». Агрегация в bucket
        потеряла бы distribution.

        Args:
            account_name: имя аккаунта (или "" для глобальных).
            event_type: тип события — `cycle_pause_sec`, `dwell_sec`,
                `scroll_count`, и т.д.
            value: численное значение sample'а (sec / count / px / ...).
            ts: unix-time (по умолчанию сейчас).
            cursor: для участия в общей транзакции (см. C4).
        """
        ts_val = float(ts) if ts is not None else time.time()
        with self._with_cursor(write=True, cursor=cursor) as cur:
            cur.execute(
                "INSERT INTO behavioral_samples "
                "(account_name, event_type, value, ts) VALUES (?, ?, ?, ?)",
                (account_name or "", event_type, float(value), ts_val),
            )

    def get_behavioral_samples(
        self,
        account_name: str | None = None,
        event_type: str | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """
        T20: вернуть raw sample'ы по фильтру (для дебага и тестов).

        Args:
            account_name: None => все; "" => только глобальные.
            event_type: None => все типы.
            since_ts / until_ts: unix-time границы (since включительно,
                until — исключительно).
            limit: ограничить количество (последние).
        """
        where: list[str] = []
        params: list = []
        if account_name is not None:
            where.append("account_name = ?")
            params.append(account_name)
        if event_type is not None:
            where.append("event_type = ?")
            params.append(event_type)
        if since_ts is not None:
            where.append("ts >= ?")
            params.append(float(since_ts))
        if until_ts is not None:
            where.append("ts < ?")
            params.append(float(until_ts))
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = (
            f"SELECT account_name, event_type, value, ts "
            f"FROM behavioral_samples {where_sql} "
            f"ORDER BY ts DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_behavioral_stats(
        self,
        account_name: str | None = None,
        event_type: str | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
        bins: int = 10,
    ) -> dict:
        """
        T20: статистика для аудита pattern'а.

        Загружает все sample'ы в память и считает через `statistics` —
        для типичных объёмов (≤10k за 7 дней) это норм.

        Возвращает dict с ключами:
            count: int
            min, max, mean, median, p25, p75, p95: float | None
            stddev: float | None  (population std через statistics.pstdev)
            histogram: list[dict] — bins с {left, right, count}
        """
        where: list[str] = []
        params: list = []
        if account_name is not None:
            where.append("account_name = ?")
            params.append(account_name)
        if event_type is not None:
            where.append("event_type = ?")
            params.append(event_type)
        if since_ts is not None:
            where.append("ts >= ?")
            params.append(float(since_ts))
        if until_ts is not None:
            where.append("ts < ?")
            params.append(float(until_ts))
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT value FROM behavioral_samples {where_sql} ORDER BY value"
        with self._cursor() as cur:
            cur.execute(sql, params)
            values = [row[0] for row in cur.fetchall()]

        empty = {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "p95": None,
            "stddev": None,
            "histogram": [],
        }
        if not values:
            return empty

        n = len(values)
        # values уже отсортированы по SQL ORDER BY value.
        v_min = float(values[0])
        v_max = float(values[-1])
        mean = sum(values) / n

        def _percentile(sorted_vals: list[float], p: float) -> float:
            # Linear-interpolation percentile на отсортированном списке.
            # p в [0, 1].
            if len(sorted_vals) == 1:
                return float(sorted_vals[0])
            k = (len(sorted_vals) - 1) * p
            lo = int(k)
            hi = min(lo + 1, len(sorted_vals) - 1)
            frac = k - lo
            return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)

        median = _percentile(values, 0.5)
        p25 = _percentile(values, 0.25)
        p75 = _percentile(values, 0.75)
        p95 = _percentile(values, 0.95)

        if n >= 2:
            # Population stddev (как pstdev из statistics) — без bessel-correction.
            mean_ = mean
            stddev = (sum((x - mean_) ** 2 for x in values) / n) ** 0.5
        else:
            stddev = 0.0

        # Histogram: bins равной ширины от min до max.
        bins = max(1, int(bins))
        histogram: list[dict] = []
        if v_max > v_min:
            width = (v_max - v_min) / bins
            counts = [0] * bins
            for v in values:
                # Последний бин включает v_max (правый край).
                idx = int((v - v_min) / width) if v < v_max else bins - 1
                idx = max(0, min(bins - 1, idx))
                counts[idx] += 1
            for i in range(bins):
                histogram.append(
                    {
                        "left": v_min + width * i,
                        "right": v_min + width * (i + 1),
                        "count": counts[i],
                    }
                )
        else:
            # Все значения одинаковые — один бин.
            histogram.append({"left": v_min, "right": v_max, "count": n})

        return {
            "count": n,
            "min": v_min,
            "max": v_max,
            "mean": float(mean),
            "median": median,
            "p25": p25,
            "p75": p75,
            "p95": p95,
            "stddev": float(stddev),
            "histogram": histogram,
        }

    # ──────────────────────────────────────────────────────────────────────
    # C3: Captcha log — журнал инцидентов для пост-mortem анализа
    # ──────────────────────────────────────────────────────────────────────

    def log_captcha(
        self,
        account_name: str,
        page_url: str = "",
        action: str = "",
        captcha_type: str = "",
        ts: float | None = None,
        cursor=None,
    ) -> None:
        """
        C3: записать капча-инцидент в captcha_log.

        Args:
            account_name: имя аккаунта.
            page_url: URL страницы, где была капча.
            action: что делал бот ("phone_click", "login", ...).
            captcha_type: тип капчи ("phone_captcha", "login_captcha", ...).
            ts: unix-time инцидента (по умолчанию сейчас).
            cursor: для участия в общей транзакции.
        """
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))
        with self._with_cursor(write=True, cursor=cursor) as cur:
            cur.execute(
                "INSERT INTO captcha_log (account_name, ts, page_url, action, captcha_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_name or "", ts_str, page_url or "", action or "", captcha_type or ""),
            )

    def get_captcha_log(self, account_name: str, limit: int = 5) -> list[dict]:
        """
        C3: последние N капча-инцидентов для аккаунта (для /lastcaptcha).
        Возвращает list[dict] с ключами: id, account_name, ts, page_url, action, captcha_type.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, account_name, ts, page_url, action, captcha_type "
                "FROM captcha_log WHERE account_name = ? "
                "ORDER BY ts DESC LIMIT ?",
                (account_name or "", int(limit)),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_new_listings_count(self, since_date):
        """
        Get the number of new listings since a specific date
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM listings WHERE date_parsed >= ?", (since_date,))
            return cursor.fetchone()[0]

    def get_unclassified_listings(self):
        """
        Get listings that haven't been classified yet
        """
        with self._cursor() as cursor:
            cursor.execute(
                'SELECT * FROM listings WHERE classification IS NULL OR classification = ""'
            )
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [self._row_to_listing_dict(row, columns) for row in rows]

    @staticmethod
    def _row_to_listing_dict(row, columns):
        """
        Преобразует строку listings в dict, дополнительно парсит photo_urls из JSON.
        Если photo_urls — невалидный JSON, возвращает пустой список (чтобы
        потребитель мог безопасно итерироваться).
        """
        data = dict(zip(columns, row))
        raw_photos = data.get("photo_urls")
        if isinstance(raw_photos, str) and raw_photos.strip():
            try:
                parsed = json.loads(raw_photos)
                data["photo_urls"] = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                data["photo_urls"] = []
        elif raw_photos is None:
            data["photo_urls"] = []
        return data

    def get_listing_by_id(self, listing_id):
        """
        Get a specific listing by ID
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM listings WHERE id = ?", (listing_id,))
            row = cursor.fetchone()
            if not row:
                return None
            columns = [d[0] for d in cursor.description]
            return self._row_to_listing_dict(row, columns)

    def get_listing_by_url(self, url):
        """
        Get a specific listing by URL
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM listings WHERE url = ?", (url,))
            row = cursor.fetchone()
            if not row:
                return None
            columns = [d[0] for d in cursor.description]
            return self._row_to_listing_dict(row, columns)

    def is_new_listing(self, url):
        """
        Возвращает True, если листинга с таким URL ещё нет в БД.
        Используется, например, в bot.py для корректного счётчика
        new_listings_count (C3). Должна вызываться ДО upsert_listing.
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT 1 FROM listings WHERE url = ? LIMIT 1", (url,))
            return cursor.fetchone() is None

    def mark_listing_parse_status(self, url, status, listing_id=None, cursor=None):
        """
        Помечает листинг статусом парсинга:
            'ok'      — успешно распарсен
            'captcha' — попали на капчу (A3)
            'error'   — другая ошибка
            'skipped' — пропустили намеренно

        Идентификация по url ИЛИ listing_id (один из двух обязателен).
        Если листинг не нашёлся — создаёт минимальную запись по url
        (чтобы не терять факт инцидента, например при детекте капчи ещё
        до полноценного парсинга).

        C4: параметр cursor позволяет включить запись статуса в общую
        транзакцию вместе с upsert_listing.

        Возвращает listing_id.
        """
        if not url and not listing_id:
            raise ValueError("mark_listing_parse_status: url or listing_id required")

        now = time.strftime("%Y-%m-%d %H:%M:%S")

        with self._with_cursor(write=True, cursor=cursor) as cursor:
            if listing_id is not None:
                cursor.execute(
                    "UPDATE listings SET parse_status = ?, parse_status_at = ? WHERE id = ?",
                    (status, now, listing_id),
                )
                return listing_id

            # path: by url
            cursor.execute(
                "UPDATE listings SET parse_status = ?, parse_status_at = ? WHERE url = ?",
                (status, now, url),
            )
            if cursor.rowcount == 0:
                # Создаём минимальную запись.
                cursor.execute(
                    "INSERT INTO listings (url, parse_status, parse_status_at, date_scraped) "
                    "VALUES (?, ?, ?, ?)",
                    (url, status, now, now),
                )
                return cursor.lastrowid
            # Если update прошёл — достанем id.
            cursor.execute("SELECT id FROM listings WHERE url = ?", (url,))
            row = cursor.fetchone()
            return row[0] if row else None

    def update_listing_classification(
        self, listing_id, classification, confidence, source, classified_at
    ):
        """
        Update listing with classification results
        """
        with self._cursor(write=True) as cursor:
            cursor.execute(
                """
                UPDATE listings
                SET classification = ?,
                    classification_confidence = ?,
                    classification_source = ?,
                    classification_at = ?
                WHERE id = ?
            """,
                (classification, confidence, source, classified_at, listing_id),
            )

    def get_classification_stats(self) -> dict:
        """
        K3: агрегированная статистика по полю `classification` для всех листингов.

        Возвращает:
            {"by_label": {"owner": N, "agent": M, "uncertain": K}, "total": N+M+K}

        Используется в TG-боте (callback `classification_stats`). Раньше TG-бот
        обходил DatabaseManager и открывал `sqlite3.connect(db.db_path)` сам, что
        нарушало AGENTS.md и теряло WAL/busy_timeout/write_lock.

        NULL и пустые `classification` исключаются (это «ещё не классифицировано»).
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT classification, COUNT(*) AS cnt FROM listings "
                "WHERE classification IS NOT NULL AND classification != '' "
                "GROUP BY classification"
            )
            rows = cur.fetchall()
        by_label: dict[str, int] = {row[0]: int(row[1]) for row in rows}
        return {"by_label": by_label, "total": sum(by_label.values())}

    # ──────────────────────────────────────────────────────────────────────
    # Accounts
    # ──────────────────────────────────────────────────────────────────────

    def upsert_account(
        self, profile_id, name, active_listings_count, registration_date, score, cursor=None
    ):
        """
        Insert or update an account in the database.

        Сохраняет поля classification_* при upsert (как и для listings).
        C4: cursor=... — для участия в общей транзакции.
        """
        with self._with_cursor(write=True, cursor=cursor) as cursor:
            cursor.execute(
                """
                INSERT INTO avito_accounts
                (profile_id, name, active_listings_count, registration_date, score)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    name=excluded.name,
                    active_listings_count=excluded.active_listings_count,
                    registration_date=excluded.registration_date,
                    score=excluded.score
            """,
                (profile_id, name, active_listings_count, registration_date, score),
            )

    def get_account_active_listings(self, profile_id):
        """
        Get the number of active listings for an account
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT active_listings_count FROM avito_accounts WHERE profile_id = ?",
                (profile_id,),
            )
            result = cursor.fetchone()
            return result[0] if result else 0

    def update_account_classification(
        self, profile_id, classification, confidence, source, classified_at
    ):
        """
        Update account with classification results
        """
        with self._cursor(write=True) as cursor:
            cursor.execute(
                """
                UPDATE avito_accounts
                SET classification = ?,
                    classification_confidence = ?,
                    classification_source = ?,
                    classification_at = ?
                WHERE profile_id = ?
            """,
                (classification, confidence, source, classified_at, profile_id),
            )

    # ──────────────────────────────────────────────────────────────────────
    # Phones
    # ──────────────────────────────────────────────────────────────────────

    def upsert_phone(self, phone_normalized, listing_count=1, score=0.0, cursor=None):
        """
        Insert or update a phone number in the database.

        C4: cursor=... — для участия в общей транзакции с upsert_listing.
        """
        with self._with_cursor(write=True, cursor=cursor) as cursor:
            cursor.execute(
                "SELECT listing_count FROM phones WHERE phone_normalized = ?", (phone_normalized,)
            )
            result = cursor.fetchone()

            if result:
                new_count = result[0] + listing_count
                cursor.execute(
                    """
                    UPDATE phones
                    SET listing_count = ?
                    WHERE phone_normalized = ?
                """,
                    (new_count, phone_normalized),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO phones (phone_normalized, listing_count, score)
                    VALUES (?, ?, ?)
                """,
                    (phone_normalized, listing_count, score),
                )

    def get_phone_count(self, phone_normalized):
        """
        Get the number of listings associated with a specific phone number
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT listing_count FROM phones WHERE phone_normalized = ?", (phone_normalized,)
            )
            result = cursor.fetchone()
            return result[0] if result else 0

    # ──────────────────────────────────────────────────────────────────────
    # Dialogs & Messages
    # ──────────────────────────────────────────────────────────────────────

    def get_dialog(self, our_account, visitor_id, listing_id=None, cursor=None):
        """
        Get dialog by account and visitor.

        C4: cursor=... — чтобы внутри upsert_dialog лукап и UPDATE/INSERT
        делались на одном соединении и видели один и тот же snapshot.
        """
        with self._with_cursor(write=False, cursor=cursor) as cursor:
            if listing_id:
                cursor.execute(
                    """
                    SELECT * FROM dialogs
                    WHERE our_account = ? AND visitor_id = ? AND listing_id = ?
                """,
                    (our_account, visitor_id, listing_id),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM dialogs
                    WHERE our_account = ? AND visitor_id = ?
                """,
                    (our_account, visitor_id),
                )

            row = cursor.fetchone()
            if not row:
                return None
            columns = [d[0] for d in cursor.description]
            return dict(zip(columns, row))

    def upsert_dialog(
        self,
        our_account,
        visitor_id,
        listing_id,
        status,
        last_message_text,
        last_message_time,
        cursor=None,
    ):
        """
        Insert or update a dialog.

        C4: cursor=... — позволяет атомарно объединить обновление статуса
        диалога с записью исходящего сообщения после успешной отправки.
        """
        with self._with_cursor(write=True, cursor=cursor) as cursor:
            existing = self.get_dialog(our_account, visitor_id, listing_id, cursor=cursor)
            if existing:
                cursor.execute(
                    """
                    UPDATE dialogs
                    SET status = ?, last_message_text = ?, last_message_time = ?
                    WHERE id = ?
                """,
                    (status, last_message_text, last_message_time, existing["id"]),
                )
                return existing["id"]

            cursor.execute(
                """
                INSERT INTO dialogs
                (listing_id, visitor_id, our_account, status, date_started,
                 last_message_text, last_message_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    listing_id,
                    visitor_id,
                    our_account,
                    status,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    last_message_text,
                    last_message_time,
                ),
            )
            return cursor.lastrowid

    def add_message(self, dialog_id, direction, text, timestamp, classification=None, cursor=None):
        """
        Add a message to the database if it doesn't exist.

        C4: cursor=... — для атомарной связки "обновить диалог + добавить
        исходящее сообщение" сразу после _send_message.
        """
        with self._with_cursor(write=True, cursor=cursor) as cursor:
            cursor.execute(
                """
                SELECT 1 FROM messages
                WHERE dialog_id = ? AND direction = ? AND text = ? AND timestamp = ?
            """,
                (dialog_id, direction, text, timestamp),
            )

            if cursor.fetchone():
                return

            cursor.execute(
                """
                INSERT INTO messages (dialog_id, direction, text, timestamp, classification)
                VALUES (?, ?, ?, ?, ?)
            """,
                (dialog_id, direction, text, timestamp, classification),
            )

    def get_first_in_message_age_seconds(self, dialog_id: int, text: str) -> float | None:
        """
        F5: возраст в секундах самой ранней записи in-сообщения с указанным
        text в диалоге `dialog_id`. None — если такого in-сообщения нет.

        Используется для определения «когда мы ВПЕРВЫЕ увидели это сообщение»
        чтобы отвечать с реалистичной задержкой (не сразу после прихода).

        Использование MIN(timestamp) принципиально: `add_message` дедуплицирует
        только по полному кортежу `(dialog_id, direction, text, timestamp)`,
        поэтому при повторном перепарсивании одного и того же in-сообщения
        в разных циклах оно записывается заново с новым timestamp. Брать
        MAX дало бы «возраст последнего повторного просмотра» (≈ now),
        а нам нужно — самое раннее появление.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT MIN(timestamp) FROM messages "
                "WHERE dialog_id = ? AND direction = 'in' AND text = ?",
                (dialog_id, text),
            )
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        try:
            first_seen = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            # Старые/повреждённые записи с нестандартным форматом — лучше
            # «не знаем, отвечаем сразу», чем падать.
            return None
        return (datetime.datetime.now() - first_seen).total_seconds()

    def get_messages(self, dialog_id, limit=20):
        """
        Get messages for a dialog (chronological order)
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE dialog_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (dialog_id, limit),
            )
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            results = [dict(zip(columns, row)) for row in rows]
            return results[::-1]

    # ──────────────────────────────────────────────────────────────────────
    # H1: Outbound contacts (proactive: бот пишет собственнику первым)
    # ──────────────────────────────────────────────────────────────────────
    # Главное правило: НИКОГДА не пишем одному profile_id дважды, даже
    # с разных аккаунтов. UNIQUE(profile_id) в схеме делает это hard
    # constraint на уровне БД — нельзя случайно «забыть» проверить.

    def was_owner_contacted(self, profile_id: str) -> bool:
        """H1: проверка глобального dedup. True если К ЛЮБОМУ из наших
        аккаунтов уже отправляли первое сообщение этому собственнику.
        Используется в outbound_messenger перед попыткой написать.
        """
        if not profile_id:
            return False
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM outbound_contacts WHERE profile_id = ? LIMIT 1",
                (profile_id,),
            )
            return cursor.fetchone() is not None

    def record_outbound(
        self,
        account_name: str,
        profile_id: str,
        listing_id: int | None = None,
        listing_url: str | None = None,
        status: str = "sent",
        persona: str | None = None,
        message_text: str | None = None,
        cursor=None,
    ) -> bool:
        """H1: записать факт outbound-контакта. Возвращает True если
        запись создана, False если profile_id уже был контактирован
        (UNIQUE constraint hit — race condition между потоками).

        Хранится: какой аккаунт писал, по какому листингу, когда, какой
        текст и от какой персоны (для пост-анализа эффективности).

        message_text сохраняется для аналитики/анти-спама — если потом
        захотим увидеть какие шаблоны имеют наилучший response rate.
        Содержит реальный отправленный текст (не sanitized — sanitizer
        отрезает запрещённые поля но не меняет «smell»).
        """
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            INSERT OR IGNORE INTO outbound_contacts
                (profile_id, account_name, listing_id, listing_url,
                 contacted_at, status, persona, message_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            profile_id,
            account_name,
            listing_id,
            listing_url,
            timestamp,
            status,
            persona,
            message_text,
        )
        if cursor is not None:
            cursor.execute(sql, params)
            return cursor.rowcount > 0
        with self._cursor(write=True) as cur:
            cur.execute(sql, params)
            return cur.rowcount > 0

    def get_owners_to_contact(
        self, account_name: str, limit: int = 10, *, min_age_hours: float = 0.0
    ) -> list:
        """H1: вернуть кандидатов для outbound от этого аккаунта.

        Критерии:
          - listing.classification = 'owner'
          - listing.profile_id NOT NULL и НЕ в outbound_contacts
          - listing.parse_status = 'ok' (исключаем captcha/error)
          - Опционально: листинг старше min_age_hours (свежие листинги
            подождать чтобы не выглядеть как «бот, сразу пишущий»).

        Сортировка по date_scraped DESC: сначала более свежие (но не
        слишком — фильтр min_age_hours отрежет «только что распарсенные»).

        Возвращает list[dict] с полями: id, url, title, profile_id,
        seller_name, location, area, price, description, category.
        """
        cutoff = None
        if min_age_hours > 0:
            cutoff = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(time.time() - min_age_hours * 3600),
            )
        with self._cursor() as cursor:
            sql = """
                SELECT id, url, title, profile_id, seller_name, location,
                       area, price, description, category
                FROM listings
                WHERE classification = 'owner'
                  AND profile_id IS NOT NULL
                  AND profile_id != ''
                  AND profile_id != 'unknown'
                  AND (parse_status IS NULL OR parse_status = 'ok')
                  AND profile_id NOT IN (SELECT profile_id FROM outbound_contacts)
            """
            params: list = []
            if cutoff is not None:
                sql += " AND date_scraped <= ?"
                params.append(cutoff)
            sql += " ORDER BY date_scraped DESC LIMIT ?"
            params.append(limit)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    def get_outbound_count_today(self, account_name: str) -> int:
        """H1: сколько outbound-контактов аккаунт сделал сегодня.
        Используется для проверки дневного бюджета перед каждым новым
        контактом (доп. защита помимо metrics).
        """
        today = time.strftime("%Y-%m-%d")
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM outbound_contacts "
                "WHERE account_name = ? AND contacted_at LIKE ?",
                (account_name, f"{today}%"),
            )
            return int(cursor.fetchone()[0])
