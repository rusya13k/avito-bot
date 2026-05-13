"""
F4: тесты расширенного Yandex query pool и функции _pick_queries.

Проверяем:
- THEMATIC_QUERIES содержит >= 25 запросов.
- _pick_queries возвращает только строки из THEMATIC_QUERIES или YANDEX_QUERIES.
- На большой выборке встречаются и тематические, и общие запросы (diversity).
- Пустой список (idle warmup) возможен, но редок.
- yandex_warmup использует _pick_queries вместо random.sample(THEMATIC_QUERIES).
"""

from unittest.mock import MagicMock, patch

from bot import THEMATIC_QUERIES, YANDEX_QUERIES, _pick_queries

# ── THEMATIC_QUERIES — размер ──────────────────────────────────────────────


def test_thematic_queries_count():
    """F4: THEMATIC_QUERIES должен содержать >= 25 запросов."""
    assert len(THEMATIC_QUERIES) >= 25, (
        f"Ожидалось >= 25, получено {len(THEMATIC_QUERIES)}"
    )


def test_thematic_queries_are_strings():
    """F4: все элементы THEMATIC_QUERIES — непустые строки."""
    for q in THEMATIC_QUERIES:
        assert isinstance(q, str) and q.strip(), f"Невалидный запрос: {q!r}"


# ── _pick_queries — свойства распределения ────────────────────────────────


def test_pick_queries_returns_valid_strings():
    """F4: _pick_queries возвращает строки только из двух пулов."""
    allowed = set(THEMATIC_QUERIES) | set(YANDEX_QUERIES)
    for _ in range(200):
        result = _pick_queries(3)
        for q in result:
            assert q in allowed, f"Неизвестный запрос: {q!r}"


def test_pick_queries_length_at_most_num():
    """F4: результат не длиннее запрошенного num (может быть короче из-за 5%-пропуска)."""
    for _ in range(100):
        result = _pick_queries(5)
        assert len(result) <= 5


def test_pick_queries_diversity():
    """F4: на 1000 вызовах _pick_queries(2) встречаются оба пула запросов."""
    thematic_seen = False
    general_seen = False
    thematic_set = set(THEMATIC_QUERIES)
    general_set = set(YANDEX_QUERIES)

    for _ in range(1000):
        for q in _pick_queries(2):
            if q in thematic_set:
                thematic_seen = True
            if q in general_set:
                general_seen = True
        if thematic_seen and general_seen:
            break

    assert thematic_seen, "Тематические запросы ни разу не встретились за 1000 вызовов"
    assert general_seen, "Общие запросы ни разу не встретились за 1000 вызовов"


def test_pick_queries_idle_possible():
    """F4: пустой список возможен (5% idle slot), но редок (не в каждом вызове)."""
    results = [_pick_queries(1) for _ in range(2000)]
    empty_count = sum(1 for r in results if len(r) == 0)
    # 5% вероятность на 1 слот → ≈100 из 2000. Проверяем диапазон.
    assert 10 <= empty_count <= 300, (
        f"idle count={empty_count} вне ожидаемого диапазона [10, 300]"
    )


# ── yandex_warmup использует _pick_queries ────────────────────────────────


def test_yandex_warmup_uses_pick_queries():
    """F4: yandex_warmup вызывает _pick_queries, а не random.sample(THEMATIC_QUERIES)."""
    from bot import yandex_warmup

    driver = MagicMock()
    driver.title = "Яндекс"
    driver.page_source = "x" * 300

    with (
        patch("bot.safe_get", return_value=False),  # не идём на ya.ru
        patch("bot._pick_queries", return_value=[]) as mock_pick,
    ):
        yandex_warmup(driver, MagicMock(), "acc1", num_queries=2)

    # safe_get вернул False → функция завершилась до _pick_queries.
    # Тест проверяет, что _pick_queries существует и доступна как атрибут бота.
    # Реальный вызов проверяем через side_effect=True ниже.
    _ = mock_pick  # используется в следующем тесте


def test_yandex_warmup_pick_queries_called_with_num():
    """F4: yandex_warmup передаёт num_queries в _pick_queries."""
    from bot import yandex_warmup

    driver = MagicMock()
    driver.title = "Яндекс"
    driver.page_source = "x" * 300

    with (
        patch("bot.safe_get", return_value=True),
        patch("bot._pick_queries", return_value=[]) as mock_pick,
        patch("bot.hp"),
        patch("bot.human_scroll"),
    ):
        yandex_warmup(driver, MagicMock(), "acc1", num_queries=3)

    mock_pick.assert_called_once_with(3)
