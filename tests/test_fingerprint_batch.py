"""
Tier 4 batch: F10 (scroll-rng), F11 (variable dialogs), F12 (browse budget guard).

Все три — низкорисковые [XS]-задачи, минимизирующие behavioral fingerprint.

Покрытие:
- F10: scroll_gallery теперь делает random.randint(1, 12) iters вместо
  фикс 20, и в view_listing вызывается с вероятностью 0.60.
- F11: _pick_dialog_count возвращает 1..available с весовым распределением.
- F12: AvitoClient.browse_commercial_categories пропускается при
  исчерпанном A2-бюджете на "listings".
"""

from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

# ── F10: scroll_gallery variable iters ─────────────────────────────────────


def test_scroll_gallery_uses_random_iters():
    """F10: scroll_gallery должен вызывать random.randint(1, 12) хотя бы
    раз (random iters вместо фикс 20)."""
    from bot import scroll_gallery

    driver = MagicMock()
    driver.find_elements.return_value = []  # break сразу после первой итерации
    wait = MagicMock()
    wait.until.return_value = MagicMock()

    with (
        patch("bot.hp"),
        patch("bot.random") as mock_rng,
    ):
        mock_rng.randint.return_value = 3  # из 1..12 выбрали 3
        scroll_gallery(driver, wait)

    # Проверяем что random.randint вызывался с границами 1..12.
    randint_calls = [c for c in mock_rng.randint.call_args_list if c.args == (1, 12)]
    assert len(randint_calls) >= 1, "scroll_gallery должен делать random.randint(1, 12)"


def test_view_listing_calls_scroll_only_with_60pct_probability():
    """F10: view_listing вызывает scroll_gallery если random.random() < 0.60.
    При random=0.70 → НЕ должен вызвать. Используем normal-cycle random,
    чтобы пройти F9 interest score без раннего выхода."""
    from bot import view_listing

    driver = MagicMock()
    driver.title = "Office"
    driver.page_source = "ok" * 150
    driver.find_elements.return_value = []
    driver.find_element.side_effect = Exception("no")
    wait = MagicMock()
    wait.until.return_value = MagicMock()

    with (
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery") as mock_scroll,
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click"),
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state"),
        patch("bot.random") as mock_rng,
    ):
        # 0.70: НЕ < 0.15 (interest), НЕ < 0.20 (uninteresting),
        # НЕ < 0.60 (scroll_gallery), НЕ < favorite/call rates.
        mock_rng.random.return_value = 0.70
        mock_rng.uniform.return_value = 5.0
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        view_listing(driver, wait, "acc1")

    mock_scroll.assert_not_called()


def test_view_listing_calls_scroll_with_lucky_roll():
    """F10: random=0.50 < 0.60 → scroll_gallery вызывается."""
    from bot import view_listing

    driver = MagicMock()
    driver.title = "Office"
    driver.page_source = "ok" * 150
    driver.find_elements.return_value = []
    driver.find_element.side_effect = Exception("no")
    wait = MagicMock()
    wait.until.return_value = MagicMock()

    with (
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery") as mock_scroll,
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click"),
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state"),
        patch("bot.random") as mock_rng,
    ):
        # 0.50: > 0.15 (no interest), > 0.20 (no uninterest), < 0.60 → scroll.
        mock_rng.random.return_value = 0.50
        mock_rng.uniform.return_value = 5.0
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        view_listing(driver, wait, "acc1")

    mock_scroll.assert_called_once()


# ── F11: _pick_dialog_count ────────────────────────────────────────────────


def test_pick_dialog_count_returns_at_least_one():
    """F11: при available >= 1 всегда возвращаем >= 1 (никогда 0)."""
    from avito_messenger import _pick_dialog_count

    for available in (1, 2, 3, 5, 7, 10):
        for _ in range(50):
            n = _pick_dialog_count(available)
            assert 1 <= n <= available, f"для available={available}: n={n}"


def test_pick_dialog_count_zero_when_no_dialogs():
    """F11: available=0 → 0 (странный кейс, но не падаем)."""
    from avito_messenger import _pick_dialog_count

    assert _pick_dialog_count(0) == 0


def test_pick_dialog_count_caps_at_seven():
    """F11: weight-table до 7. Если available=100, всё равно ≤ 7."""
    from avito_messenger import _pick_dialog_count

    for _ in range(100):
        assert 1 <= _pick_dialog_count(100) <= 7


def test_pick_dialog_count_distribution_biased_to_small():
    """F11: статистика на 5000 выборок — пик должен быть на 1-3 (короткие
    сессии). При available=7 веса: 0.30/0.25/0.20/0.10/0.08/0.05/0.02."""
    from avito_messenger import _pick_dialog_count

    counter: Counter[int] = Counter(_pick_dialog_count(7) for _ in range(5000))
    # Топ-3 значения должны быть 1, 2, 3 (в любом порядке).
    most_common = [n for n, _ in counter.most_common(3)]
    assert set(most_common) == {1, 2, 3}, f"топ-3: {counter.most_common()}"
    # 1 чаще 7 (пик слева).
    assert counter[1] > counter[7]


# ── F12: budget guard для browse ───────────────────────────────────────────


@pytest.fixture
def avito_client_with_db():
    """AvitoClient с моком db для F12-проверок бюджета."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    db = MagicMock()
    client = AvitoClient(driver, wait, "acc_f12", log_func=MagicMock(), db_manager=db)
    return client, db


def test_browse_skipped_when_budget_exhausted(avito_client_with_db):
    """F12: если check_daily_budget=False → bot.browse_commercial_categories
    НЕ вызывается, метод возвращает None."""
    client, db = avito_client_with_db

    with (
        patch("account_state.account_state") as mock_state,
        patch("bot.browse_commercial_categories") as mock_browse,
    ):
        mock_state.check_daily_budget.return_value = False
        result = client.browse_commercial_categories()

    assert result is None
    mock_browse.assert_not_called()
    mock_state.check_daily_budget.assert_called_once_with("acc_f12", "listings", db)


def test_browse_runs_when_budget_ok(avito_client_with_db):
    """F12: если check_daily_budget=True → bot.browse_commercial_categories
    вызывается как обычно."""
    client, db = avito_client_with_db

    with (
        patch("account_state.account_state") as mock_state,
        patch("bot.browse_commercial_categories") as mock_browse,
    ):
        mock_state.check_daily_budget.return_value = True
        mock_browse.return_value = "ok"
        result = client.browse_commercial_categories()

    assert result == "ok"
    mock_browse.assert_called_once()


def test_browse_skips_check_when_no_db():
    """F12: AvitoClient без db_manager не должен пытаться проверять бюджет
    (back-compat — раньше browse мог работать без db, см. test_avito_client)."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    # db_manager не передан.
    client = AvitoClient(driver, wait, "acc_no_db", log_func=MagicMock())

    with (
        patch("account_state.account_state") as mock_state,
        patch("bot.browse_commercial_categories") as mock_browse,
    ):
        mock_browse.return_value = "ok"
        result = client.browse_commercial_categories()

    # check_daily_budget не должен вызываться (нечем проверять).
    mock_state.check_daily_budget.assert_not_called()
    mock_browse.assert_called_once()
    assert result == "ok"
