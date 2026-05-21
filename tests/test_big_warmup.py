"""
T4: тесты Big warmup (multi-site).

Покрываем:
- BIG_WARMUP_SITES структура (поля, размер).
- _pick_warmup_sites: уникальные, mix тематических и общих, граничные num.
- _visit_site: успех, падение safe_get, exception.
- big_warmup: основной flow, idle (num=0), with_yandex_search=False, exception в _visit_site.
- AvitoClient.big_warmup: фасад делегирует с правильными аргументами.
"""

from unittest.mock import MagicMock, patch

import pytest

import warmup
from warmup import (
    BIG_WARMUP_SITES,
    _pick_warmup_sites,
    _visit_site,
    big_warmup,
)

# ── BIG_WARMUP_SITES структура ─────────────────────────────────────────────


def test_big_warmup_sites_count():
    """T4: пул содержит >= 5 сайтов."""
    assert len(BIG_WARMUP_SITES) >= 5, f"мало сайтов: {len(BIG_WARMUP_SITES)}"


def test_big_warmup_sites_required_fields():
    """T4: каждый сайт имеет необходимые поля и валидные диапазоны."""
    for s in BIG_WARMUP_SITES:
        assert "name" in s and isinstance(s["name"], str)
        assert "url" in s and s["url"].startswith("http")
        lo, hi = s["dwell_range"]
        assert 0 < lo <= hi, f"{s['name']}: dwell_range invalid {s['dwell_range']}"
        slo, shi = s["scrolls_range"]
        assert 0 < slo <= shi, f"{s['name']}: scrolls_range invalid {s['scrolls_range']}"
        assert 0.0 <= float(s["click_chance"]) <= 1.0


def test_big_warmup_has_thematic_sites():
    """T4: в пуле есть хотя бы 1 тематический сайт (cian/dom для коммерческой)."""
    thematic = [s for s in BIG_WARMUP_SITES if s.get("thematic")]
    assert thematic, "нет тематических сайтов — T4 теряет 'тематический шум'"


def test_big_warmup_has_general_sites():
    """T4: в пуле есть несколько общих (новости/соцсети) сайтов."""
    general = [s for s in BIG_WARMUP_SITES if not s.get("thematic")]
    assert len(general) >= 3, f"мало общих сайтов: {len(general)}"


# ── _pick_warmup_sites ─────────────────────────────────────────────────────


def test_pick_warmup_sites_unique():
    """T4: _pick_warmup_sites не повторяет сайты."""
    for _ in range(50):
        chosen = _pick_warmup_sites(4)
        names = [s["name"] for s in chosen]
        assert len(names) == len(set(names)), f"повтор сайтов: {names}"


def test_pick_warmup_sites_count_respected():
    """T4: возвращает запрошенное число (или меньше если пул маленький)."""
    for n in (1, 2, 3, 4, 5):
        chosen = _pick_warmup_sites(n)
        assert len(chosen) == min(n, len(BIG_WARMUP_SITES))


def test_pick_warmup_sites_zero_clamped_to_one():
    """T4: num=0 → возвращает >=1 (никогда не пустой)."""
    chosen = _pick_warmup_sites(0)
    assert len(chosen) >= 1


def test_pick_warmup_sites_thematic_inclusion_for_n_ge_2():
    """T4: при num >= 2 хотя бы в большинстве вызовов попадает 1 тематический."""
    thematic_names = {s["name"] for s in BIG_WARMUP_SITES if s.get("thematic")}
    hits = 0
    for _ in range(200):
        chosen = _pick_warmup_sites(3)
        if any(s["name"] in thematic_names for s in chosen):
            hits += 1
    # _pick_warmup_sites гарантирует тематический при n>=2 → 100% случаев.
    assert hits >= 180, f"тематический сайт попадал только в {hits}/200 вызовов"


def test_pick_warmup_sites_capped_to_pool():
    """T4: num > len(pool) → возвращает len(pool), не больше."""
    chosen = _pick_warmup_sites(100)
    assert len(chosen) == len(BIG_WARMUP_SITES)


# ── _visit_site ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_log():
    calls = []

    def _log(name, msg):
        calls.append((name, msg))

    _log.calls = calls
    return _log


def test_visit_site_success(fake_log):
    """T4: _visit_site при удачном safe_get возвращает ok=True + duration."""
    driver = MagicMock()
    driver.find_elements.return_value = []  # не ищем ссылок
    site = {
        "name": "ya.ru",
        "url": "https://ya.ru",
        "dwell_range": (0.01, 0.02),  # быстрый тест
        "scrolls_range": (1, 1),
        "click_chance": 0.0,
        "thematic": False,
    }
    with (
        patch("bot.safe_get", return_value=True),
        patch("warmup.time.sleep"),
    ):
        stats = _visit_site(driver, site, "acc1", fake_log)
    assert stats["ok"] is True
    assert stats["name"] == "ya.ru"
    assert stats["scrolled"] == 1
    assert stats["clicked"] is False


def test_visit_site_safe_get_failure(fake_log):
    """T4: если safe_get вернул False (block/empty), возвращаем ok=False."""
    driver = MagicMock()
    site = {
        "name": "mail.ru",
        "url": "https://mail.ru",
        "dwell_range": (1, 2),
        "scrolls_range": (1, 1),
        "click_chance": 0.0,
        "thematic": False,
    }
    with (
        patch("bot.safe_get", return_value=False),
        patch("warmup.time.sleep"),
    ):
        stats = _visit_site(driver, site, "acc1", fake_log)
    assert stats["ok"] is False
    assert stats["scrolled"] == 0


def test_visit_site_safe_get_raises(fake_log):
    """T4: exception в safe_get → ok=False, не пробрасывается наружу."""
    driver = MagicMock()
    site = {
        "name": "x",
        "url": "https://x",
        "dwell_range": (1, 2),
        "scrolls_range": (1, 1),
        "click_chance": 0.0,
        "thematic": False,
    }
    with (
        patch("bot.safe_get", side_effect=RuntimeError("boom")),
        patch("warmup.time.sleep"),
    ):
        stats = _visit_site(driver, site, "acc1", fake_log)
    assert stats["ok"] is False


# ── big_warmup ─────────────────────────────────────────────────────────────


def test_big_warmup_main_flow(fake_log):
    """T4: big_warmup посещает 3 сайта, возвращает stats."""
    driver = MagicMock()

    def fake_visit(driver, site, account_name, log_func):
        return {
            "name": site["name"],
            "ok": True,
            "duration_s": 1.0,
            "scrolled": 2,
            "clicked": False,
        }

    with (
        patch("warmup._visit_site", side_effect=fake_visit),
        patch("bot.yandex_warmup", return_value=True),
    ):
        stats = big_warmup(driver, "acc1", num_sites=3, log_func=fake_log)

    assert stats["sites_visited"] == 3
    assert stats["sites_failed"] == 0
    assert stats["yandex_ok"] is True
    assert len(stats["details"]) == 3


def test_big_warmup_partial_failure(fake_log):
    """T4: если часть сайтов упала, big_warmup НЕ падает целиком."""
    driver = MagicMock()
    call_count = {"n": 0}

    def fake_visit(driver, site, account_name, log_func):
        call_count["n"] += 1
        ok = call_count["n"] != 2  # второй сайт упал
        return {
            "name": site["name"],
            "ok": ok,
            "duration_s": 1.0,
            "scrolled": 0 if not ok else 2,
            "clicked": False,
        }

    with (
        patch("warmup._visit_site", side_effect=fake_visit),
        patch("bot.yandex_warmup", return_value=True),
    ):
        stats = big_warmup(driver, "acc1", num_sites=3, log_func=fake_log)

    assert stats["sites_visited"] == 2
    assert stats["sites_failed"] == 1


def test_big_warmup_visit_site_exception_caught(fake_log):
    """T4: если _visit_site бросает (не должно), big_warmup ловит и идёт дальше."""
    driver = MagicMock()

    def fake_visit(driver, site, account_name, log_func):
        raise RuntimeError("DOM mutation observer failed")

    with (
        patch("warmup._visit_site", side_effect=fake_visit),
        patch("bot.yandex_warmup", return_value=False),
    ):
        stats = big_warmup(driver, "acc1", num_sites=2, log_func=fake_log)

    assert stats["sites_visited"] == 0
    assert stats["sites_failed"] == 2
    assert "details" in stats and len(stats["details"]) == 2
    assert all("error" in d for d in stats["details"])


def test_big_warmup_without_yandex(fake_log):
    """T4: with_yandex_search=False → yandex_ok=None, бот.yandex_warmup не зовётся."""
    driver = MagicMock()

    with (
        patch(
            "warmup._visit_site",
            return_value={
                "name": "x",
                "ok": True,
                "duration_s": 0.0,
                "scrolled": 1,
                "clicked": False,
            },
        ),
        patch("bot.yandex_warmup") as mock_yw,
    ):
        stats = big_warmup(driver, "acc1", num_sites=2, log_func=fake_log, with_yandex_search=False)

    mock_yw.assert_not_called()
    assert stats["yandex_ok"] is None


def test_big_warmup_yandex_failure_does_not_kill(fake_log):
    """T4: исключение в yandex_warmup ловится → yandex_ok=False."""
    driver = MagicMock()

    with (
        patch(
            "warmup._visit_site",
            return_value={
                "name": "x",
                "ok": True,
                "duration_s": 0.0,
                "scrolled": 1,
                "clicked": False,
            },
        ),
        patch("bot.yandex_warmup", side_effect=RuntimeError("yandex disconnected")),
    ):
        stats = big_warmup(driver, "acc1", num_sites=1, log_func=fake_log)

    assert stats["yandex_ok"] is False


def test_big_warmup_default_num_sites_is_3_to_5():
    """T4: num_sites=None → random.randint(3, 5) → реально 3..5 сайтов."""
    driver = MagicMock()

    def fake_visit(driver, site, account_name, log_func):
        return {
            "name": site["name"],
            "ok": True,
            "duration_s": 0.0,
            "scrolled": 1,
            "clicked": False,
        }

    counts = []
    with (
        patch("warmup._visit_site", side_effect=fake_visit),
        patch("bot.yandex_warmup", return_value=True),
    ):
        for _ in range(20):
            stats = big_warmup(driver, "acc1")
            counts.append(len(stats["details"]))

    # Все вызовы должны попадать в [3..5].
    assert all(3 <= c <= 5 for c in counts), f"out-of-range counts: {counts}"
    # И должно быть разнообразие (не всегда один).
    assert len(set(counts)) >= 2, f"нет рандома: counts={counts}"


# ── AvitoClient фасад ──────────────────────────────────────────────────────


def test_avitoclient_big_warmup_delegates():
    """T4: AvitoClient.big_warmup делегирует в warmup.big_warmup с правильными args."""
    from avito_client import AvitoClient

    driver = MagicMock()
    wait = MagicMock()
    client = AvitoClient(driver, wait, "acc1")

    with patch("warmup.big_warmup") as mock_bw:
        mock_bw.return_value = {"sites_visited": 3}
        result = client.big_warmup(num_sites=4, with_yandex_search=False, yandex_queries=2)

    assert result == {"sites_visited": 3}
    mock_bw.assert_called_once()
    args, kwargs = mock_bw.call_args
    assert args[0] is driver
    assert args[1] == "acc1"
    assert kwargs["num_sites"] == 4
    assert kwargs["with_yandex_search"] is False
    assert kwargs["yandex_queries"] == 2
    # log_func прокидывается из client.log.
    assert kwargs["log_func"] is client.log


def test_avitoclient_has_big_warmup_method():
    """T4: AvitoClient.big_warmup существует и доступен."""
    from avito_client import AvitoClient

    assert hasattr(AvitoClient, "big_warmup")
    assert callable(AvitoClient.big_warmup)


# ── warmup модуль импортируется ───────────────────────────────────────────


def test_warmup_module_smoke_import():
    """T4: warmup модуль импортируется без ошибок."""
    assert hasattr(warmup, "big_warmup")
    assert hasattr(warmup, "BIG_WARMUP_SITES")
    assert hasattr(warmup, "_pick_warmup_sites")
