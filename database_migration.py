"""
Standalone миграция SQLite БД.

Миграция — ИДЕМПОТЕНТНАЯ: безопасно запускать многократно.
Добавляет:
  - (A2) поля парсинга в listings: title, seller_name, profile_id,
    profile_url, phone, active_listings_count, photo_urls
  - поля classification_* в listings и avito_accounts
  - поля visitor_id / last_message_* в dialogs
  - таблицу messages, если её нет
  - (E2) таблицу metrics + индекс по bucket_hour
  - индексы по новым колонкам

Запуск:
    python database_migration.py
или из Python:
    from database_migration import migrate_database
    migrate_database("avito_bot.db")

Замечание: database.py при инициализации DatabaseManager вызывает ту же
логику в _migrate_database(). Этот скрипт полезен для ручного запуска
без поднятия бота.
"""

import sqlite3

# (table, column, type) — все поля, которые могут отсутствовать в старой БД.
NEW_COLUMNS = [
    # --- A2: данные парсинга, которые раньше терялись ---
    ("listings", "title", "TEXT"),
    ("listings", "seller_name", "TEXT"),
    ("listings", "profile_id", "TEXT"),
    ("listings", "profile_url", "TEXT"),
    ("listings", "phone", "TEXT"),
    ("listings", "active_listings_count", "INTEGER"),
    ("listings", "photo_urls", "TEXT"),
    # --- A3: статус парсинга листинга ---
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
    # --- messaging ---
    ("dialogs", "visitor_id", "TEXT"),
    ("dialogs", "last_message_text", "TEXT"),
    ("dialogs", "last_message_time", "TEXT"),
]

NEW_INDEXES = [
    ("idx_listings_profile_id", "listings", "profile_id"),
    ("idx_listings_phone", "listings", "phone"),
]


def _add_column_if_missing(cursor, table, column, col_type):
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        print(f"  + {table}.{column} ({col_type})")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False
        # другая ошибка — пробрасываем
        raise


def migrate_database(db_path="avito_bot.db"):
    """
    Add parsing + classification + messaging fields to the database schema.
    Safe to run multiple times.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        added = 0
        for table, column, col_type in NEW_COLUMNS:
            if _add_column_if_missing(cursor, table, column, col_type):
                added += 1

        # Create messages table if it doesn't exist
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

        # E2: metrics table — счётчики per account / per hour.
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

        # Indexes (idempotent)
        for idx_name, table, column in NEW_INDEXES:
            cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({column})")

        conn.commit()
        print(f"Database migration completed. New columns added: {added}")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_database()
