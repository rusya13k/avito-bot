"""
F1: тесты реалистичных вероятностей favorite/call в view_listing.

Проверяем:
- view_listing принимает favorite_rate / call_rate и уважает их.
- «Позвонить» пропускается при should_skip_phone=True (A3-интеграция).
- record_phone_click вызывается после успешного клика «Позвонить».
- Клик не происходит если random.random() >= call_rate или favorite_rate.
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


def _run_view_listing(favorite_rate=0.08, call_rate=0.05, random_return=0.01, **state_overrides):
    """
    Запускает view_listing с полностью замоканным окружением.

    random_return: значение, которое всегда возвращает random.random().
      0.01 → все rate-блоки достигаются (0.01 < 0.05 < 0.08).
      0.90 → ни один rate-блок не достигается.

    state_overrides: переопределения атрибутов mock account_state:
      should_skip_phone=True/False, и т.п.
    """
    from bot import view_listing

    driver = _make_driver()
    wait = _make_wait()

    state_mock = MagicMock()
    state_mock.should_skip_phone.return_value = state_overrides.get("should_skip_phone", False)

    with (
        # F9: hp возвращает число (потраченные секунды) — нужно для f-string
        # `dwell time: {dwell_time:.1f}s`. Без return_value MagicMock не
        # умеет :.1f и тест падает с TypeError.
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery"),
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click") as mock_move_click,
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
    ):
        mock_rng.random.return_value = random_return
        mock_rng.uniform.return_value = 5.0
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None  # порядок не меняем
        mock_wdw.return_value.until.return_value = MagicMock()

        view_listing(driver, wait, "acc1", favorite_rate=favorite_rate, call_rate=call_rate)

    return mock_move_click, state_mock


# ── тесты A3-интеграции для кнопки «Позвонить» ───────────────────────────


def test_call_blocked_when_phone_limit_reached():
    """F1+A3: «Позвонить» не нажимается если should_skip_phone=True."""
    _, state = _run_view_listing(
        random_return=0.01,  # попадаем в call_rate-блок
        should_skip_phone=True,
    )

    state.should_skip_phone.assert_called_once_with("acc1")
    state.record_phone_click.assert_not_called()


def test_call_records_phone_click_on_success():
    """F1+A3: record_phone_click вызывается после успешного клика «Позвонить»."""
    _, state = _run_view_listing(
        random_return=0.01,  # попадаем в call_rate-блок
        should_skip_phone=False,
    )

    state.record_phone_click.assert_called_once_with("acc1")


# ── тесты rate-логики ──────────────────────────────────────────────────────


def test_call_not_attempted_when_rate_exceeded():
    """F1: call_rate=0.05 — при random.random()=0.9 кнопка не нажимается."""
    _, state = _run_view_listing(call_rate=0.05, random_return=0.90)

    # should_skip_phone НЕ вызывался — до блока не дошли
    state.should_skip_phone.assert_not_called()
    state.record_phone_click.assert_not_called()


def test_favorite_not_clicked_when_rate_exceeded():
    """F1: favorite_rate=0.08 — при random.random()=0.9 избранное не нажимается."""
    mock_move_click, _ = _run_view_listing(favorite_rate=0.08, random_return=0.90)

    mock_move_click.assert_not_called()


# ── тест передачи rates через AvitoClient ──────────────────────────────────


def test_avito_client_passes_rates_to_browse():
    """F1: AvitoClient передаёт favorite_rate/call_rate в browse_commercial_categories."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()

    client = AvitoClient(driver, wait, "acc1", favorite_rate=0.03, call_rate=0.02)

    with patch("bot.browse_commercial_categories") as mock_browse:
        client.browse_commercial_categories()

    mock_browse.assert_called_once()
    _, kwargs = mock_browse.call_args
    assert kwargs.get("favorite_rate") == 0.03
    assert kwargs.get("call_rate") == 0.02
