"""
E2: тесты per-account search_filters.

Проверяем:
- find_and_view_commercial_listings: города из search_filters используются в URL.
- find_and_view_commercial_listings: deal_type (строка) определяет /sdam- или /prodam-.
- find_and_view_commercial_listings: deal_types (список) работает наравне с deal_type.
- find_and_view_commercial_listings: price_min переопределяет дефолт из конфига.
- find_and_view_commercial_listings: price_max добавляется только если задан.
- find_and_view_commercial_listings: без search_filters — дефолтный random.
- browse_commercial_categories: city prefix добавляется в URL если задан.
- browse_commercial_categories: без cities — URL без city prefix.
- AvitoClient: search_filters прокидываются в find_and_view и browse.
"""

from unittest.mock import MagicMock, patch

import pytest

from bot import browse_commercial_categories, find_and_view_commercial_listings

# ── helpers ────────────────────────────────────────────────────────────────────


def _driver_no_links():
    """Mock-driver, у которого find_elements всегда возвращает []."""
    d = MagicMock()
    d.find_elements.return_value = []
    return d


def _db():
    db = MagicMock()
    db.is_new_listing.return_value = True
    return db


def _run_find(search_filters=None):
    """Запускает find_and_view_commercial_listings и возвращает захваченный URL."""
    captured = {}

    def fake_safe_get(drv, url, name, **kwargs):
        captured["url"] = url
        return True

    with (
        patch("bot.account_state.is_cooled_down", return_value=False),
        patch("bot.safe_get", side_effect=fake_safe_get),
        patch("bot.human_scroll"),
    ):
        find_and_view_commercial_listings(
            _driver_no_links(), MagicMock(), "acc1", _db(), search_filters=search_filters
        )

    return captured.get("url", "")


def _run_browse(search_filters=None, num_categories=1):
    """Запускает browse_commercial_categories и возвращает список захваченных URL."""
    captured_urls = []

    def fake_safe_get(drv, url, name, **kwargs):
        captured_urls.append(url)
        return True

    with (
        patch("bot.safe_get", side_effect=fake_safe_get),
        patch("bot.random_mouse_move"),
        patch("bot.human_scroll"),
    ):
        browse_commercial_categories(
            _driver_no_links(),
            MagicMock(),
            "acc1",
            num_categories=num_categories,
            search_filters=search_filters,
        )

    return captured_urls


# ── find_and_view_commercial_listings — города ─────────────────────────────────


def test_custom_city_used_in_url():
    """Если cities=['novosibirsk'] — только этот город в URL."""
    url = _run_find({"cities": ["novosibirsk"]})
    assert "novosibirsk" in url


def test_multiple_cities_one_chosen():
    """Если cities=['kazan', 'samara'] — один из них попадает в URL."""
    url = _run_find({"cities": ["kazan", "samara"]})
    assert "kazan" in url or "samara" in url


def test_default_city_pool_when_no_filters():
    """Без search_filters URL содержит один из MILLION_CITIES."""
    from commercial_realestate_config import MILLION_CITIES

    url = _run_find()
    assert any(city in url for city in MILLION_CITIES)


# ── find_and_view_commercial_listings — тип сделки ─────────────────────────────


def test_deal_type_rent_gives_sdam_in_url():
    """deal_type='rent' → URL содержит sdam (аренда)."""
    url = _run_find({"deal_type": "rent"})
    assert "sdam" in url


def test_deal_type_sale_gives_prodam_in_url():
    """deal_type='sale' → URL содержит prodam (продажа)."""
    url = _run_find({"deal_type": "sale"})
    assert "prodam" in url


def test_deal_types_list_single_rent():
    """deal_types=['rent'] (список из одного) → всегда sdam."""
    url = _run_find({"deal_types": ["rent"]})
    assert "sdam" in url


def test_deal_types_list_single_sale():
    """deal_types=['sale'] (список из одного) → всегда prodam."""
    url = _run_find({"deal_types": ["sale"]})
    assert "prodam" in url


# ── find_and_view_commercial_listings — цена ───────────────────────────────────


def test_price_min_used_in_url():
    """price_min=123456 → ?pmin=123456 в URL."""
    url = _run_find({"price_min": 123456})
    assert "pmin=123456" in url


def test_price_max_added_when_set():
    """price_max=999000 → &pmax=999000 в URL."""
    url = _run_find({"price_max": 999000})
    assert "pmax=999000" in url


def test_no_price_max_without_filter():
    """Без price_max — pmax отсутствует в URL."""
    url = _run_find()
    assert "pmax" not in url


# ── find_and_view_commercial_listings — комбо ──────────────────────────────────


def test_all_filters_combined():
    """Все фильтры вместе применяются одновременно."""
    url = _run_find(
        {
            "cities": ["kazan"],
            "deal_type": "rent",
            "price_min": 50000,
            "price_max": 500000,
        }
    )
    assert "kazan" in url
    assert "sdam" in url
    assert "pmin=50000" in url
    assert "pmax=500000" in url


# ── browse_commercial_categories — города ─────────────────────────────────────


def test_browse_no_city_filter_no_prefix():
    """Без search_filters URL начинается сразу с /nedvizhimost."""
    urls = _run_browse()
    assert urls, "safe_get не был вызван"
    assert urls[0].startswith("https://www.avito.ru/nedvizhimost")


def test_browse_city_prefix_added():
    """cities=['volgograd'] → /volgograd/ перед путём категории."""
    urls = _run_browse(search_filters={"cities": ["volgograd"]})
    assert urls, "safe_get не был вызван"
    assert urls[0].startswith("https://www.avito.ru/volgograd/nedvizhimost")


def test_browse_multiple_categories_same_city():
    """При num_categories=2 оба URL содержат выбранный город."""
    urls = _run_browse(search_filters={"cities": ["omsk"]}, num_categories=2)
    assert len(urls) >= 1
    assert all("omsk" in u for u in urls)


# ── AvitoClient — search_filters прокидываются ───────────────────────────────


def test_avito_client_passes_search_filters_to_find():
    """AvitoClient передаёт self.search_filters в bot.find_and_view_commercial_listings."""
    from account_state import account_state as _astate
    from avito_client import AvitoClient

    sf = {"cities": ["kazan"], "deal_type": "rent"}
    db_mock = _db()

    with (
        patch.object(_astate, "_get_daily_total_from_db", return_value=0),
        patch.object(_astate, "check_budget_alert", return_value=None),
        patch.object(_astate, "check_daily_budget", return_value=True),
        patch("bot.find_and_view_commercial_listings", return_value=(0, 0, 0)) as mock_find,
    ):
        client = AvitoClient(
            MagicMock(), MagicMock(), "acc1", db_manager=db_mock, search_filters=sf
        )
        client.find_and_view_commercial_listings()

    mock_find.assert_called_once()
    _, kwargs = mock_find.call_args
    assert kwargs.get("search_filters") == sf
    assert "max_listings_per_search" in kwargs  # F2


def test_avito_client_passes_search_filters_to_browse():
    """AvitoClient передаёт self.search_filters в bot.browse_commercial_categories."""
    from avito_client import AvitoClient

    sf = {"cities": ["spb"]}

    with patch("bot.browse_commercial_categories") as mock_browse:
        client = AvitoClient(MagicMock(), MagicMock(), "acc1", search_filters=sf)
        client.browse_commercial_categories()

    mock_browse.assert_called_once()
    _, kwargs = mock_browse.call_args
    assert kwargs.get("search_filters") == sf
    # F1: rates тоже прокидываются (defaults)
    assert "favorite_rate" in kwargs
    assert "call_rate" in kwargs


def test_avito_client_empty_filters_passes_none():
    """AvitoClient с search_filters={} передаёт None в browse."""
    from avito_client import AvitoClient

    with patch("bot.browse_commercial_categories") as mock_browse:
        client = AvitoClient(MagicMock(), MagicMock(), "acc1", search_filters={})
        client.browse_commercial_categories()

    # search_filters={} нормализуется до None
    _, kwargs = mock_browse.call_args
    assert kwargs.get("search_filters") is None
