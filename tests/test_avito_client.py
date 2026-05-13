"""
G1: тесты AvitoClient — фасада над Selenium-флоу.

Подход: мокируем драйвер и нижележащие функции (`bot.safe_get`,
`bot.is_session_authenticated`, ...) и проверяем, что:
  - методы клиента корректно делегируют в нужные модули с правильными
    аргументами;
  - composite-логика `login()` идёт по 3-уровневому пути в правильном порядке;
  - guard'ы (отсутствие db_manager / llm_classifier) поднимают понятный
    RuntimeError.

Selenium-стак сюда НЕ тащим.
"""

from unittest.mock import MagicMock, patch

import pytest

from avito_client import AvitoClient


@pytest.fixture
def driver():
    return MagicMock(name="driver")


@pytest.fixture
def wait():
    return MagicMock(name="wait")


@pytest.fixture
def log():
    return MagicMock(name="log")


@pytest.fixture
def client(driver, wait, log):
    return AvitoClient(
        driver,
        wait,
        "acc1",
        log_func=log,
        db_manager=MagicMock(name="db"),
        llm_classifier=MagicMock(name="llm"),
    )


# ── Construction & defaults ────────────────────────────────────────────


def test_log_default_is_no_op(driver, wait):
    """Без log_func клиент не падает на self.log(...)."""
    c = AvitoClient(driver, wait, "acc1")
    # должен молча проглотить вызов
    c.log("acc1", "test message")


def test_init_stores_attributes(driver, wait, log):
    db = MagicMock()
    llm = MagicMock()
    c = AvitoClient(driver, wait, "acc1", log_func=log, db_manager=db, llm_classifier=llm)
    assert c.driver is driver
    assert c.wait is wait
    assert c.account_name == "acc1"
    assert c.log is log
    assert c.db is db
    assert c.llm is llm


# ── Navigation: safe_get / goto_listing / check_block ──────────────────


def test_safe_get_delegates(client, driver):
    with patch("bot.safe_get", return_value=True) as m:
        assert client.safe_get("https://x") is True
        m.assert_called_once_with(driver, "https://x", "acc1", retries=2)


def test_check_block_delegates(client, driver):
    with patch("bot.check_block", return_value=False) as m:
        assert client.check_block() is False
        m.assert_called_once_with(driver, "acc1")


def test_goto_listing_returns_false_if_safe_get_fails(client):
    with (
        patch("bot.safe_get", return_value=False),
        patch("bot.check_block", return_value=False) as cb,
    ):
        assert client.goto_listing("https://x") is False
        # check_block НЕ должен вызываться, если safe_get не открыл страницу
        cb.assert_not_called()


def test_goto_listing_returns_false_on_block(client):
    with patch("bot.safe_get", return_value=True), patch("bot.check_block", return_value=True):
        assert client.goto_listing("https://x") is False


def test_goto_listing_happy_path(client):
    with patch("bot.safe_get", return_value=True), patch("bot.check_block", return_value=False):
        assert client.goto_listing("https://x") is True


# ── Login flow: 3-уровневый composite ──────────────────────────────────


def test_login_unreachable_avito_returns_false(client):
    """Если safe_get на main page вернул False — сразу False."""
    with patch("bot.safe_get", return_value=False):
        assert client.login(thinking_delay=False) is False


def test_login_native_session_short_circuits(client):
    """is_session_authenticated=True после safe_get -> сразу True, никаких
    cookies / manual попыток."""
    with (
        patch("bot.safe_get", return_value=True),
        patch("bot.is_session_authenticated", return_value=True),
        patch("bot.load_cookies") as cookies_call,
        patch("bot.perform_login") as manual_call,
    ):
        assert (
            client.login(
                cookies_path="some/path",
                phone="+7",
                password="x",
                thinking_delay=False,
            )
            is True
        )
        cookies_call.assert_not_called()
        manual_call.assert_not_called()


def test_login_falls_through_to_cookies(client, tmp_path):
    """Native session нет, cookies существуют и срабатывают."""
    cookies = tmp_path / "cookies.json"
    cookies.write_text("[]", encoding="utf-8")
    # is_session_authenticated: сначала False (после native), потом True (после cookies).
    auth_results = iter([False, True])
    with (
        patch("bot.safe_get", return_value=True),
        patch("bot.is_session_authenticated", side_effect=lambda *a, **kw: next(auth_results)),
        patch("bot.load_cookies") as cookies_call,
        patch("bot.perform_login") as manual_call,
    ):
        assert (
            client.login(
                cookies_path=cookies,
                phone="+7",
                password="x",
                thinking_delay=False,
            )
            is True
        )
        cookies_call.assert_called_once()
        manual_call.assert_not_called()


def test_login_skips_cookies_if_path_missing(client, tmp_path):
    """cookies_path указан, но файла нет — не падаем, идём к manual."""
    auth_results = iter([False, False])  # native -> manual flow
    with (
        patch("bot.safe_get", return_value=True),
        patch("bot.is_session_authenticated", side_effect=lambda *a, **kw: next(auth_results)),
        patch("bot.load_cookies") as cookies_call,
        patch("bot.perform_login", return_value=True) as manual_call,
    ):
        assert (
            client.login(
                cookies_path=tmp_path / "missing.json",
                phone="+7",
                password="x",
                thinking_delay=False,
            )
            is True
        )
        cookies_call.assert_not_called()
        manual_call.assert_called_once()


def test_login_falls_through_to_manual(client, tmp_path):
    """Cookies не сработали, manual login успешен."""
    cookies = tmp_path / "cookies.json"
    cookies.write_text("[]", encoding="utf-8")
    auth_results = iter([False, False])  # native -> cookies -> manual
    with (
        patch("bot.safe_get", return_value=True),
        patch("bot.is_session_authenticated", side_effect=lambda *a, **kw: next(auth_results)),
        patch("bot.load_cookies"),
        patch("bot.perform_login", return_value=True) as manual_call,
    ):
        assert (
            client.login(
                cookies_path=cookies,
                phone="+7",
                password="x",
                thinking_delay=False,
            )
            is True
        )
        manual_call.assert_called_once_with(
            client.driver,
            client.wait,
            "acc1",
            "+7",
            "x",
        )


def test_login_no_credentials_returns_false(client):
    """Native session нет, нет cookies/credentials — все попытки исчерпаны."""
    with (
        patch("bot.safe_get", return_value=True),
        patch("bot.is_session_authenticated", return_value=False),
    ):
        assert client.login(thinking_delay=False) is False


def test_login_manual_failure_returns_false(client):
    auth_results = iter([False])  # после native
    with (
        patch("bot.safe_get", return_value=True),
        patch("bot.is_session_authenticated", side_effect=lambda *a, **kw: next(auth_results)),
        patch("bot.perform_login", return_value=False),
    ):
        assert client.login(phone="+7", password="x", thinking_delay=False) is False


def test_login_swallows_cookies_exception(client, tmp_path):
    """G1: исключение в load_cookies не должно ломать login — просто
    продолжаем к manual. Это важно для битых cookies-файлов."""
    cookies = tmp_path / "bad.json"
    cookies.write_text("[]", encoding="utf-8")
    auth_results = iter([False, False])
    with (
        patch("bot.safe_get", return_value=True),
        patch("bot.is_session_authenticated", side_effect=lambda *a, **kw: next(auth_results)),
        patch("bot.load_cookies", side_effect=RuntimeError("bad cookie")),
        patch("bot.perform_login", return_value=True),
    ):
        assert (
            client.login(
                cookies_path=cookies,
                phone="+7",
                password="x",
                thinking_delay=False,
            )
            is True
        )


# ── Search / listings ───────────────────────────────────────────────


def test_warmup_yandex_delegates(client, driver, wait):
    with patch("bot.yandex_warmup", return_value=True) as m:
        assert client.warmup_yandex(num_queries=3) is True
        m.assert_called_once_with(driver, wait, "acc1", num_queries=3)


def test_extract_listing_data_delegates(client, driver, wait, log):
    with patch("commercial_parser.extract_listing_data", return_value={"url": "x"}) as m:
        result = client.extract_listing_data()
        assert result == {"url": "x"}
        m.assert_called_once_with(driver, wait, "acc1", log)


def test_save_listing_requires_db_manager(driver, wait):
    c = AvitoClient(driver, wait, "acc1")  # no db_manager
    with pytest.raises(RuntimeError, match="db_manager"):
        c.save_listing({"url": "x"})


def test_save_listing_delegates(client, log):
    with patch("commercial_parser.save_listing_to_db", return_value=42) as m:
        assert client.save_listing({"url": "x"}) == 42
        m.assert_called_once_with({"url": "x"}, client.db, log, "acc1")


def test_find_and_view_commercial_listings_requires_db(driver, wait):
    c = AvitoClient(driver, wait, "acc1")  # no db_manager
    with pytest.raises(RuntimeError, match="db_manager"):
        c.find_and_view_commercial_listings()


def test_find_and_view_commercial_listings_delegates(client, driver, wait):
    with patch("bot.find_and_view_commercial_listings", return_value=(5, 2, 1)) as m:
        assert client.find_and_view_commercial_listings() == (5, 2, 1)
        m.assert_called_once()
        _, kwargs = m.call_args
        assert kwargs.get("search_filters") is None
        assert "max_listings_per_search" in kwargs  # F2


# ── Messenger ───────────────────────────────────────────────


def test_process_messages_requires_db_and_llm(driver, wait):
    c = AvitoClient(driver, wait, "acc1")  # no db_manager / llm
    with pytest.raises(RuntimeError, match="db_manager"):
        c.process_messages()
    c.db = MagicMock()
    with pytest.raises(RuntimeError, match="llm"):
        c.process_messages()


def test_process_messages_creates_messenger_and_runs(client, driver, wait, log):
    """G1: client.process_messages создаёт AvitoMessenger и запускает loop."""
    fake_messenger = MagicMock()
    fake_messenger_cls = MagicMock(return_value=fake_messenger)
    with patch("avito_messenger.AvitoMessenger", fake_messenger_cls):
        client.process_messages()
    fake_messenger_cls.assert_called_once_with(
        driver,
        wait,
        client.db,
        client.llm,
        "acc1",
    )
    fake_messenger.process_messages.assert_called_once_with(log)
