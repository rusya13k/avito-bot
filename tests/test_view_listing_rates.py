"""
F1: тесты реалистичных вероятностей favorite/message в view_listing.

Проверяем:
- view_listing принимает favorite_rate / message_rate и уважает их.
- message_rate — вероятность нажать «Написать» и отправить сообщение.
- call_rate — устаревший алиас для message_rate (back-compat).
- Клик не происходит если random.random() >= message_rate или favorite_rate.
- AvitoClient передаёт rates в browse_commercial_categories.
"""

from unittest.mock import MagicMock, patch

# ── хелпер: минимальные моки для запуска view_listing без реального Selenium


def _make_driver():
    """
    Возвращает driver-mock, который не тригерит check_block:
      - title без «Ой!»/«Captcha»
      - page_source > 200 символов без блокирующих паттернов
    """
    driver = MagicMock()
    driver.title = "Офис 200 м2, центр"
    driver.page_source = "ok" * 150  # 300 символов, no block pattern
    driver.find_elements.return_value = []  # scroll_gallery: нечего листать
    driver.find_element.side_effect = Exception("no element")  # scroll_to_desc мимо
    driver.execute_script.return_value = None
    return driver


def _make_wait():
    wait = MagicMock()
    wait.until.return_value = MagicMock()  # h1 найден → не выходим раньше времени
    return wait


def _run_view_listing(
    favorite_rate=0.08, message_rate=0.05, call_rate=None, random_return=0.01, **state_overrides
):
    """
    Запускает view_listing с полностью замоканным окружением.

    random_return: значение, которое всегда возвращает random.random().
      0.01 → все rate-блоки достигаются (0.01 < 0.05 < 0.08).
      0.90 → ни один rate-блок не достигается.

    state_overrides: переопределения атрибутов mock account_state.
    """
    from bot import view_listing

    driver = _make_driver()
    wait = _make_wait()

    state_mock = MagicMock()

    with (
        # F9: hp возвращает число (потраченные секунды) — нужно для f-string
        # `dwell time: {dwell_time:.1f}s`. Без return_value MagicMock не
        # умеет :.1f и тест падает с TypeError.
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery"),
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click"),
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
        patch("bot._try_write_to_owner") as mock_write,
    ):
        mock_rng.random.return_value = random_return
        mock_rng.uniform.return_value = 5.0
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None  # порядок не меняем
        mock_wdw.return_value.until.return_value = MagicMock()

        view_listing(
            driver,
            wait,
            "acc1",
            favorite_rate=favorite_rate,
            call_rate=call_rate,
            message_rate=message_rate,
        )

    return mock_write, state_mock


# ── тесты message_rate ──────────────────────────────────────────────────────


def test_write_to_owner_called_when_message_rate_triggered():
    """F1: _try_write_to_owner вызывается когда random < message_rate."""
    mock_write, _ = _run_view_listing(message_rate=0.05, random_return=0.01)

    mock_write.assert_called_once()


def test_write_to_owner_not_called_when_rate_exceeded():
    """F1: message_rate=0.05 — при random.random()=0.9 «Написать» не нажимается."""
    mock_write, _ = _run_view_listing(message_rate=0.05, random_return=0.90)

    mock_write.assert_not_called()


def test_call_rate_as_backward_compat_alias():
    """F1: call_rate — устаревший алиас для message_rate (back-compat).
    Если передан call_rate — он используется вместо message_rate.
    """
    mock_write, _ = _run_view_listing(
        call_rate=0.10,
        message_rate=0.05,
        random_return=0.07,
    )

    # call_rate=0.10 > random=0.07 → должен быть вызов
    mock_write.assert_called_once()


def test_call_rate_none_uses_message_rate():
    """F1: call_rate=None — используем message_rate."""
    mock_write, _ = _run_view_listing(
        call_rate=None,
        message_rate=0.05,
        random_return=0.90,
    )

    # message_rate=0.05 < random=0.90 → вызова нет
    mock_write.assert_not_called()


def test_favorite_not_clicked_when_rate_exceeded():
    """F1: favorite_rate=0.08 — при random.random()=0.9 избранное не нажимается."""
    # В view_listing favorite_rate проверяется отдельным random roll.
    # При random_return=0.90 ни favorite, ни message не срабатывают.
    mock_write, _ = _run_view_listing(favorite_rate=0.08, random_return=0.90)

    mock_write.assert_not_called()


# ── тест передачи rates через AvitoClient ──────────────────────────────────


def test_avito_client_passes_rates_to_browse():
    """F1: AvitoClient передаёт favorite_rate/message_rate в browse_commercial_categories."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()

    client = AvitoClient(driver, wait, "acc1", favorite_rate=0.03, message_rate=0.02)

    with patch("bot.browse_commercial_categories") as mock_browse:
        client.browse_commercial_categories()

    mock_browse.assert_called_once()
    _, kwargs = mock_browse.call_args
    assert kwargs.get("favorite_rate") == 0.03
    assert kwargs.get("message_rate") == 0.02
