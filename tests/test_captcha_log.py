"""
C3: тесты таблицы captcha_log в DatabaseManager.

Проверяем:
- log_captcha: запись сохраняется.
- get_captcha_log: возвращает последние N записей для аккаунта.
- get_captcha_log: изолирует записи по account_name.
- get_captcha_log: пустой список если записей нет.
- Поля: account_name, ts, page_url, action, captcha_type.
"""

import pytest


@pytest.fixture
def db(tmp_path):
    from database import DatabaseManager

    db_file = tmp_path / "test.db"
    mgr = DatabaseManager(str(db_file))
    return mgr


def test_log_captcha_stored(db):
    db.log_captcha(
        "acc1", page_url="https://avito.ru/1", action="phone_click", captcha_type="phone_captcha"
    )
    rows = db.get_captcha_log("acc1")
    assert len(rows) == 1
    assert rows[0]["account_name"] == "acc1"
    assert rows[0]["page_url"] == "https://avito.ru/1"
    assert rows[0]["action"] == "phone_click"
    assert rows[0]["captcha_type"] == "phone_captcha"


def test_get_captcha_log_empty(db):
    assert db.get_captcha_log("unknown") == []


def test_get_captcha_log_limit(db):
    for i in range(10):
        db.log_captcha("acc1", page_url=f"https://avito.ru/{i}")
    rows = db.get_captcha_log("acc1", limit=3)
    assert len(rows) == 3


def test_get_captcha_log_account_isolation(db):
    db.log_captcha("acc1", page_url="https://avito.ru/1")
    db.log_captcha("acc2", page_url="https://avito.ru/2")
    assert len(db.get_captcha_log("acc1")) == 1
    assert len(db.get_captcha_log("acc2")) == 1


def test_get_captcha_log_order_desc(db):
    """Записи возвращаются от новых к старым."""
    import time

    db.log_captcha("acc1", ts=time.time() - 100)
    db.log_captcha("acc1", ts=time.time())
    rows = db.get_captcha_log("acc1", limit=2)
    # Первая запись должна быть новее
    assert rows[0]["ts"] >= rows[1]["ts"]


def test_log_captcha_in_transaction(db):
    """log_captcha работает внутри транзакции (cursor= передан)."""
    with db.transaction() as cur:
        db.log_captcha("acc1", page_url="tx_test", cursor=cur)
    rows = db.get_captcha_log("acc1")
    assert rows[0]["page_url"] == "tx_test"
