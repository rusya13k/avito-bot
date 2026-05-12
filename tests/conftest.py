"""
F1: общие фикстуры для unit-тестов. Изолированная SQLite-БД на тест.
"""

import os
import sys
import tempfile

import pytest

# Тесты лежат в подпапке — добавим корень репо в sys.path, чтобы
# работали `from database import ...` и т.п.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def db():
    """
    Свежая SQLite-БД на каждый тест.

    DatabaseManager открывает соединения в нескольких потоках (write-lock,
    разные методы), поэтому ":memory:" не годится — каждое соединение
    видит свою БД. Используем именованный temp-файл и удаляем после.
    """
    from database import DatabaseManager

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield DatabaseManager(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
