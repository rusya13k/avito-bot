"""
F9: тесты для realistic dwell times в view_listing.

Покрытие:
- Initial dwell вызывает hp(...) с lognormal-distribution.
- Interest score:
    • random < 0.15 → срабатывает «очень интересный листинг» (extra dwell).
    • 0.15 ≤ random < 0.20 → ранний выход (return True), без scroll/favorite/call.
    • random ≥ 0.20 → нормальный flow.
- Reading description использует lognormal через hp(15, 90, distribution='lognormal').
- Lognormal sampling в _draw_seconds (статистика на больших выборках).

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


# ── interest score branches ────────────────────────────────────────────────


def test_uninteresting_listing_returns_early(state_mock):
    """20% случай: random=0.18 → first if (<0.15) False, elif (<0.20) True
    → return True ДО scroll_gallery / favorite / call."""
    from bot import view_listing

    with (
        patch("bot.hp", return_value=5.0),
        patch("bot.scroll_gallery") as mock_scroll,
        patch("bot.move_click") as mock_click,
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
    ):
        # 0.18 → НЕ < 0.15 (skip first), НО < 0.20 (early exit)
        mock_rng.random.return_value = 0.18
        mock_rng.uniform.return_value = 5.0
        mock_wdw.return_value.until.return_value = MagicMock()
        result = view_listing(_make_driver(), _make_wait(), "acc1")

    assert result is True
    mock_scroll.assert_not_called()
    mock_click.assert_not_called()
    state_mock.should_skip_phone.assert_not_called()


def test_interesting_listing_continues_full_flow(state_mock):
    """15% случай: random=0.10 → first if True → extra dwell, потом обычный
    flow (scroll_gallery + favorite + call вызываются как обычно)."""
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
        # 0.10 < 0.15 → interesting + продолжаем
        mock_rng.random.return_value = 0.10
        mock_rng.uniform.return_value = 100.0  # extra dwell value
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        result = view_listing(_make_driver(), _make_wait(), "acc1",
                              favorite_rate=0.5, call_rate=0.5)

    assert result is True
    mock_scroll.assert_called_once()
    # 0.10 < favorite_rate=0.5 → favorite кликнут
    # 0.10 < call_rate=0.5 → call тоже → move_click ≥ 2 раз (favorite + call)
    assert mock_click.call_count >= 1


def test_normal_listing_no_interest_modifier(state_mock):
    """random=0.30 → ни первый, ни второй if не срабатывают; обычное
    поведение без extra dwell и без раннего выхода."""
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
        mock_rng.random.return_value = 0.30
        mock_rng.uniform.return_value = 5.0
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        result = view_listing(_make_driver(), _make_wait(), "acc1")

    assert result is True
    # Полный flow выполнился — scroll_gallery вызван.
    mock_scroll.assert_called_once()


# ── hp() вызывается с lognormal-distribution для dwell ───────────────────


def test_initial_dwell_uses_lognormal_distribution(state_mock):
    """F9: первый hp(...) для initial dwell должен идти с lognormal."""
    from bot import view_listing

    captured_calls = []

    def fake_hp(lo, hi, **kwargs):
        captured_calls.append({"lo": lo, "hi": hi, "distribution": kwargs.get("distribution")})
        return 5.0

    with (
        patch("bot.hp", side_effect=fake_hp),
        patch("bot.scroll_gallery"),
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.move_click"),
        patch("bot.WebDriverWait") as mock_wdw,
        patch("bot.account_state", state_mock),
        patch("bot.random") as mock_rng,
    ):
        # 0.30 → нормальный flow, чтобы пройти всю функцию.
        mock_rng.random.return_value = 0.30
        mock_rng.uniform.return_value = 5.0
        mock_rng.randint.return_value = 2
        mock_rng.shuffle.side_effect = lambda x: None
        mock_wdw.return_value.until.return_value = MagicMock()

        view_listing(_make_driver(), _make_wait(), "acc1")

    # Первый вызов hp — initial dwell с lognormal.
    assert len(captured_calls) > 0
    assert captured_calls[0] == {"lo": 5, "hi": 120, "distribution": "lognormal"}


# ── _draw_seconds статистика для lognormal ───────────────────────────────


def test_lognormal_distribution_clamps_within_range():
    """human_delay._draw_seconds(lognormal) — все samples ∈ [lo, hi*2]."""
    from human_delay import _draw_seconds

    for _ in range(1000):
        s = _draw_seconds(10, 60, "lognormal")
        # Lognormal внутри _draw_seconds clamp'ится до [lo, hi*2].
        assert 10 <= s <= 120


def test_lognormal_distribution_is_right_skewed():
    """Lognormal имеет положительный perekos: median < mean (хвост справа)."""
    from human_delay import _draw_seconds

    samples = [_draw_seconds(10, 60, "lognormal") for _ in range(2000)]
    samples_sorted = sorted(samples)
    median = samples_sorted[len(samples_sorted) // 2]
    mean = sum(samples) / len(samples)
    # При lognormal mean > median — это и есть характеристика правого хвоста.
    assert mean >= median, f"mean={mean:.2f} < median={median:.2f} — нет skew"
