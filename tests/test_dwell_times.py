"""
T10: тесты для content-aware dwell times в view_listing.

Покрытие:
- _read_listing_meta достаёт description-text и image_count из DOM.
- view_listing использует compute_reading_dwell для расчёта dwell.
- interest score: 20% «не моё» (early exit), 15% «очень интересно»,
  65% нейтрально.
- При interest < 0.10 — ранний выход без scroll/favorite/call.
- При нормальном flow — view_listing проходит до конца, scroll_gallery
  и move_click могут быть вызваны (по своим вероятностям).

LLM/Selenium не вызываются — view_listing мокается тщательно.
"""

from unittest.mock import MagicMock, patch

import pytest


def _make_driver():
    """Driver-mock не тригерящий check_block."""
    driver = MagicMock()
    driver.title = "Office 200m, center"
    driver.page_source = "ok" * 150
    driver.find_elements.return_value = []
    driver.find_element.side_effect = Exception("no element")
    driver.execute_script.return_value = None
    return driver


def _make_wait():
    wait = MagicMock()
    wait.until.return_value = MagicMock()
    return wait


@pytest.fixture
def state_mock():
    state = MagicMock()
    state.should_skip_phone.return_value = False
    return state


# ── interest branches: early exit / continue ─────────────────────────────


def test_uninteresting_listing_returns_early(state_mock):
    """interest < 0.10 → early return True, без scroll/favorite.
    Но _try_write_to_owner вызывается ДО ранней остановки — сообщение
    отправляется даже неинтересным собственникам."""
    from bot import view_listing

    with (
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery") as mock_scroll,
        patch("bot.move_click"),
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
        patch("bot._try_write_to_owner") as mock_write,
    ):
        # interest_roll < 0.20 → ветка «совсем не моё»
        # interest = uniform(0.0, 0.20). Через mock uniform=0.05 → interest=0.05 < 0.10.
        mock_rng.random.return_value = 0.10  # interest_roll
        mock_rng.uniform.return_value = 0.05  # interest = 0.05
        mock_wdw.return_value.until.return_value = MagicMock()
        result = view_listing(_make_driver(), _make_wait(), "acc1")

    assert result is True
    mock_scroll.assert_not_called()
    # message_rate=0.05 по умолчанию → при random.random()=0.10 > 0.05
    # «Написать» не нажимается. Пишем не всем — стелс-режим.
    mock_write.assert_not_called()


def test_interesting_listing_continues_full_flow(state_mock):
    """interest ≥ 0.10 → продолжаем, scroll_gallery / move_click могут вызваться."""
    from bot import view_listing

    with (
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery") as mock_scroll,
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click") as mock_click,
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
    ):
        # interest_roll = 0.30 → нейтральная ветка → interest = uniform(0.30, 0.70).
        # mock_rng.random возвращает 0.30 — т.е. ВСЁ random.random() в коде.
        # Чтобы не сработал scroll_gallery (random < 0.60) тоже True для 0.30,
        # И favorite_rate тоже сработает при 0.30 < favorite_rate=0.5.
        mock_rng.random.return_value = 0.30
        mock_rng.uniform.return_value = 0.50  # interest = 0.50
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        result = view_listing(
            _make_driver(), _make_wait(), "acc1", favorite_rate=0.5, call_rate=0.5
        )

    assert result is True
    # scroll_gallery должен быть вызван (random=0.30 < 0.60).
    mock_scroll.assert_called_once()
    # 0.30 < 0.5 → fav + call → move_click ≥ 1.
    assert mock_click.call_count >= 1


def test_neutral_listing_no_early_exit(state_mock):
    """interest_roll в нейтральном диапазоне → не выходим рано."""
    from bot import view_listing

    with (
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery") as mock_scroll,
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click"),
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
    ):
        mock_rng.random.return_value = 0.50  # interest_roll=0.50, нейтральная
        mock_rng.uniform.return_value = 0.50
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        result = view_listing(_make_driver(), _make_wait(), "acc1")

    assert result is True
    # Полный flow выполнился — scroll_gallery вызван.
    mock_scroll.assert_called_once()


# ── compute_reading_dwell вызывается с правильными аргументами ───────────


def test_view_listing_calls_compute_reading_dwell(state_mock):
    """T10: view_listing должен вызвать compute_reading_dwell с
    description / image_count / interest."""
    from bot import view_listing

    captured = []

    def fake_compute(*, description, image_count, interest):
        captured.append(
            {
                "description": description,
                "image_count": image_count,
                "interest": interest,
            }
        )
        return 30.0

    with (
        patch("bot.hp", return_value=1.0),
        patch("bot._compute_reading_dwell", side_effect=fake_compute),
        patch("bot.scroll_gallery"),
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click"),
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
    ):
        mock_rng.random.return_value = 0.50  # interest_roll нейтральный
        mock_rng.uniform.return_value = 0.50
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        view_listing(_make_driver(), _make_wait(), "acc1")

    assert len(captured) == 1
    call = captured[0]
    # description / image_count берутся из DOM (driver mock возвращает
    # пустой rect / empty text), но ключи должны быть переданы.
    assert "description" in call
    assert "image_count" in call
    assert "interest" in call
    # interest должен быть в диапазоне [0, 1] (не raw mock-value 0.50, а
    # уже посчитанный).
    assert 0.0 <= call["interest"] <= 1.0


# ── _read_listing_meta — best-effort парсинг DOM ─────────────────────────


def test_read_listing_meta_returns_empty_on_missing_dom():
    """Если desc-element и image-frame не найдены → ('', 0)."""
    from bot import _read_listing_meta

    driver = MagicMock()
    driver.find_element.side_effect = Exception("no element")
    driver.find_elements.return_value = []
    desc, img_count = _read_listing_meta(driver)
    assert desc == ""
    assert img_count == 0


def test_read_listing_meta_parses_desc_and_images():
    """Если DOM есть — парсим текст и считаем фото."""
    from bot import _read_listing_meta

    desc_el = MagicMock()
    desc_el.text = "  Описание объекта 200 м2 в центре.  "
    driver = MagicMock()
    driver.find_element.return_value = desc_el
    # 5 «изображений»
    driver.find_elements.return_value = [MagicMock() for _ in range(5)]

    desc, img_count = _read_listing_meta(driver)
    assert desc == "Описание объекта 200 м2 в центре."
    assert img_count == 5


# ── compute_reading_dwell — статистика (sanity для T10) ───────────────────


def test_compute_dwell_zero_text_short():
    """Пустое описание + 0 фото → быстрый dwell."""
    from human_scroll import compute_reading_dwell

    samples = [
        compute_reading_dwell(description=None, image_count=0, interest=0.5) for _ in range(100)
    ]
    # ≥ 50% sample'ов должны быть < 30 сек.
    quick = [s for s in samples if s < 30.0]
    assert len(quick) >= 50, f"только {len(quick)}/100 быстрых dwell'ов"


def test_compute_dwell_long_text_with_images_can_be_long():
    """Длинное описание + много фото + interest → dwell может быть > 60s."""
    from human_scroll import compute_reading_dwell

    samples = [
        compute_reading_dwell(description="x" * 2000, image_count=15, interest=1.0)
        for _ in range(100)
    ]
    long = [s for s in samples if s > 60.0]
    # Хотя бы 30% должны быть «долгими» при идеальных условиях.
    assert len(long) >= 30, f"только {len(long)}/100 долгих dwell'ов"
