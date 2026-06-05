"""
F2: тесты variable batch sizes.

Проверяем:
- _weighted_listing_count: распределение, never-zero, max соблюдается.
- find_and_view_commercial_listings использует _weighted_listing_count.
- browse_commercial_categories использует _weighted_listing_count для
  num_categories и ads_per_category.
- AvitoClient передаёт max_* параметры.
"""

import math
from unittest.mock import MagicMock, call, patch

import pytest

from bot import _weighted_listing_count

# ── _weighted_listing_count — базовые свойства ─────────────────────────────


def test_listing_count_never_zero():
    """F2: _weighted_listing_count всегда возвращает ≥ 1."""
    for _ in range(1000):
        assert _weighted_listing_count() >= 1


def test_listing_count_respects_max_n():
    """F2: результат никогда не превышает max_n."""
    for _ in range(1000):
        assert _weighted_listing_count(max_n=3) <= 3
        assert _weighted_listing_count(max_n=7) <= 7


def test_listing_count_distribution():
    """F2: на 10000 вызовах mean ≈ 3.1, std ≈ 1.3, max ≤ 7."""
    samples = [_weighted_listing_count(max_n=7) for _ in range(10_000)]
    mean = sum(samples) / len(samples)
    variance = sum((x - mean) ** 2 for x in samples) / len(samples)
    std = math.sqrt(variance)

    assert 2.7 <= mean <= 3.5, f"mean={mean:.3f} вне диапазона [2.7, 3.5]"
    assert 1.0 <= std <= 1.6, f"std={std:.3f} вне диапазона [1.0, 1.6]"
    assert max(samples) <= 7


def test_category_count_distribution():
    """F2: max_n=4 → mean ≈ 2.7, результат 1..4."""
    samples = [_weighted_listing_count(max_n=4) for _ in range(5_000)]
    mean = sum(samples) / len(samples)

    assert 2.0 <= mean <= 3.5, f"mean={mean:.3f} при max_n=4 вне диапазона"
    assert all(1 <= x <= 4 for x in samples)


def test_listing_count_max_n_1_always_returns_1():
    """F2: max_n=1 → всегда 1."""
    for _ in range(100):
        assert _weighted_listing_count(max_n=1) == 1


# ── find_and_view_commercial_listings — использует _weighted_listing_count ─


def _safe_get_first_true(*_args, **_kwargs):
    """side_effect: первый вызов (SERP) → True, остальные (листинги) → False."""
    if not hasattr(_safe_get_first_true, "_called"):
        _safe_get_first_true._called = True
        return True
    return False


def test_find_and_view_uses_weighted_count():
    """F2: find_and_view_commercial_listings вызывает _weighted_listing_count
    и передаёт результат в random.sample."""
    from bot import find_and_view_commercial_listings

    driver = MagicMock()
    fake_links = [MagicMock() for _ in range(10)]
    for lnk in fake_links:
        lnk.get_attribute.return_value = (
            f"https://avito.ru/moskva/kommercheskaya_nedvizhimost/ofis_{id(lnk)}"
        )
    driver.find_elements.return_value = fake_links

    db = MagicMock()
    db.is_new_listing.return_value = False
    db.is_listing_url_seen.return_value = False  # D6: нет просмотренных
    if hasattr(_safe_get_first_true, "_called"):
        del _safe_get_first_true._called

    with (
        patch("bot.account_state.is_cooled_down", return_value=False),
        patch("bot.safe_get", side_effect=_safe_get_first_true),
        patch("bot.human_scroll"),
        patch("bot._weighted_listing_count", return_value=3) as mock_wlc,
    ):
        find_and_view_commercial_listings(
            driver, MagicMock(), "acc1", db, max_listings_per_search=7
        )

    mock_wlc.assert_called_once_with(max_n=7)


def test_find_and_view_respects_max_listings_param():
    """F2: max_listings_per_search передаётся в _weighted_listing_count."""
    from bot import find_and_view_commercial_listings

    driver = MagicMock()
    # Нет ссылок — выйдем сразу после SERP, до _weighted_listing_count
    # Чтобы дойти до _weighted_listing_count, нужны ссылки на SERP
    fake_links = [MagicMock() for _ in range(5)]
    for lnk in fake_links:
        lnk.get_attribute.return_value = (
            f"https://avito.ru/moskva/kommercheskaya_nedvizhimost/sklad_{id(lnk)}"
        )
    driver.find_elements.return_value = fake_links

    db = MagicMock()
    db.is_new_listing.return_value = False
    db.is_listing_url_seen.return_value = False  # D6: нет просмотренных
    if hasattr(_safe_get_first_true, "_called"):
        del _safe_get_first_true._called

    with (
        patch("bot.account_state.is_cooled_down", return_value=False),
        patch("bot.safe_get", side_effect=_safe_get_first_true),
        patch("bot.human_scroll"),
        patch("bot._weighted_listing_count", return_value=2) as mock_wlc,
    ):
        find_and_view_commercial_listings(
            driver, MagicMock(), "acc1", db, max_listings_per_search=5
        )

    mock_wlc.assert_called_once_with(max_n=5)


# ── browse_commercial_categories — variable counts ─────────────────────────


def test_browse_uses_weighted_count_for_categories():
    """F2: browse без явного num_categories вызывает _weighted_listing_count."""
    from bot import browse_commercial_categories

    with (
        patch("bot.safe_get", return_value=False),
        patch("bot.random_mouse_move"),
        patch("bot.human_scroll"),
        patch("bot._weighted_listing_count", return_value=2) as mock_wlc,
    ):
        browse_commercial_categories(MagicMock(), MagicMock(), "acc1")

    # Вызван минимум дважды: для num_categories и для n_ads
    assert mock_wlc.call_count >= 2


def test_browse_explicit_num_categories_skips_weighted():
    """F2: явный num_categories не вызывает _weighted_listing_count для категорий."""
    from bot import browse_commercial_categories

    # Мокируем safe_get чтобы не ходить в сеть, возвращаем True чтобы пройти дальше
    with (
        patch("bot.safe_get", return_value=False),
        patch("bot.random_mouse_move"),
        patch("bot.human_scroll"),
        patch("bot._weighted_listing_count", return_value=2) as mock_wlc,
    ):
        browse_commercial_categories(
            MagicMock(),
            MagicMock(),
            "acc1",
            num_categories=2,  # явно задано
        )

    # Для категорий НЕ вызываем weighted, только для ads_per_category
    calls = mock_wlc.call_args_list
    # Все вызовы должны быть только для ads (max_n=max_listings_per_browse=4)
    for c in calls:
        assert c == call(max_n=4)


# ── AvitoClient — передаёт max_* параметры ────────────────────────────────


def test_avito_client_stores_max_params():
    """F2: AvitoClient сохраняет max_* параметры в атрибутах."""
    from avito_client import AvitoClient

    client = AvitoClient(
        MagicMock(),
        MagicMock(),
        "acc1",
        max_listings_per_search=5,
        max_categories_per_browse=3,
        max_listings_per_browse=2,
    )

    assert client.max_listings_per_search == 5
    assert client.max_categories_per_browse == 3
    assert client.max_listings_per_browse == 2


def test_avito_client_passes_max_listings_to_find():
    """F2: AvitoClient передаёт max_listings_per_search в find_and_view."""
    from avito_client import AvitoClient

    db = MagicMock()
    client = AvitoClient(MagicMock(), MagicMock(), "acc1", db_manager=db, max_listings_per_search=5)

    with (
        patch("account_state.account_state") as mock_state,
        patch("bot.find_and_view_commercial_listings", return_value=(0, 0, 0)) as mock_fn,
    ):
        mock_state.check_daily_budget.return_value = True
        mock_state._get_daily_total_from_db.return_value = 0
        mock_state.check_budget_alert.return_value = None
        client.find_and_view_commercial_listings()

    _, kwargs = mock_fn.call_args
    assert kwargs.get("max_listings_per_search") == 5


def test_avito_client_passes_max_browse_to_browse():
    """F2: AvitoClient передаёт max_categories_per_browse и max_listings_per_browse."""
    from avito_client import AvitoClient

    client = AvitoClient(
        MagicMock(),
        MagicMock(),
        "acc1",
        max_categories_per_browse=3,
        max_listings_per_browse=2,
    )

    with patch("bot.browse_commercial_categories") as mock_browse:
        client.browse_commercial_categories()

    _, kwargs = mock_browse.call_args
    assert kwargs.get("max_categories_per_browse") == 3
    assert kwargs.get("max_listings_per_browse") == 2
