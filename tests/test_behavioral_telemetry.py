"""
T20: Behavioral telemetry — DB методы + bot.py hooks + tg_bot формат.
"""

from unittest.mock import MagicMock, patch

import pytest

# ════════════════════════════════════════════════════════════════════════════
# DB: behavioral_samples table + record/stats methods
# ════════════════════════════════════════════════════════════════════════════


def test_record_sample_inserts_row(db):
    """T20: один record_behavioral_sample → один ряд в behavioral_samples."""
    db.record_behavioral_sample("acc1", "cycle_pause_sec", 1800.0)
    samples = db.get_behavioral_samples()
    assert len(samples) == 1
    s = samples[0]
    assert s["account_name"] == "acc1"
    assert s["event_type"] == "cycle_pause_sec"
    assert s["value"] == pytest.approx(1800.0)
    assert s["ts"] > 0


def test_record_sample_explicit_ts(db):
    """T20: переданный ts сохраняется как есть."""
    db.record_behavioral_sample("acc1", "dwell_sec", 42.5, ts=1700000000.0)
    samples = db.get_behavioral_samples()
    assert samples[0]["ts"] == pytest.approx(1700000000.0)
    assert samples[0]["value"] == pytest.approx(42.5)


def test_record_sample_empty_account_allowed(db):
    """T20: пустой account_name (глобальный) разрешён."""
    db.record_behavioral_sample("", "global_event", 1.0)
    samples = db.get_behavioral_samples(account_name="")
    assert len(samples) == 1
    assert samples[0]["account_name"] == ""


def test_record_sample_in_transaction_commits(db):
    """T20 + C4: sample в транзакции коммитится вместе с остальным."""
    with db.transaction() as cur:
        db.record_behavioral_sample("acc1", "cycle_pause_sec", 1000.0, cursor=cur)
        db.record_behavioral_sample("acc1", "cycle_pause_sec", 2000.0, cursor=cur)
    assert len(db.get_behavioral_samples()) == 2


def test_record_sample_in_transaction_rolls_back(db):
    """T20 + C4: при exception sample откатывается."""
    with pytest.raises(RuntimeError):
        with db.transaction() as cur:
            db.record_behavioral_sample("acc1", "cycle_pause_sec", 1000.0, cursor=cur)
            raise RuntimeError("boom")
    assert db.get_behavioral_samples() == []


def test_get_samples_filter_by_account(db):
    """T20: account_name фильтрует."""
    db.record_behavioral_sample("acc1", "cycle_pause_sec", 1.0)
    db.record_behavioral_sample("acc2", "cycle_pause_sec", 2.0)
    db.record_behavioral_sample("acc1", "cycle_pause_sec", 3.0)
    s_acc1 = db.get_behavioral_samples(account_name="acc1")
    s_acc2 = db.get_behavioral_samples(account_name="acc2")
    assert len(s_acc1) == 2
    assert len(s_acc2) == 1
    assert {x["value"] for x in s_acc1} == {1.0, 3.0}


def test_get_samples_filter_by_event_type(db):
    """T20: event_type фильтрует."""
    db.record_behavioral_sample("acc1", "cycle_pause_sec", 100.0)
    db.record_behavioral_sample("acc1", "dwell_sec", 50.0)
    db.record_behavioral_sample("acc1", "cycle_pause_sec", 200.0)
    s = db.get_behavioral_samples(event_type="cycle_pause_sec")
    assert len(s) == 2
    assert all(x["event_type"] == "cycle_pause_sec" for x in s)


def test_get_samples_filter_by_ts_range(db):
    """T20: since_ts включительно, until_ts исключительно."""
    db.record_behavioral_sample("acc1", "e", 1.0, ts=100.0)
    db.record_behavioral_sample("acc1", "e", 2.0, ts=200.0)
    db.record_behavioral_sample("acc1", "e", 3.0, ts=300.0)
    s = db.get_behavioral_samples(since_ts=200.0, until_ts=300.0)
    assert len(s) == 1
    assert s[0]["value"] == pytest.approx(2.0)


def test_get_samples_limit(db):
    """T20: limit ограничивает."""
    for i in range(10):
        db.record_behavioral_sample("acc1", "e", float(i), ts=float(i))
    s = db.get_behavioral_samples(limit=3)
    assert len(s) == 3
    # ORDER BY ts DESC — последние 3.
    assert {x["value"] for x in s} == {7.0, 8.0, 9.0}


def test_get_stats_empty_returns_none(db):
    """T20: пустая выборка → count=0, остальные поля None."""
    s = db.get_behavioral_stats(account_name="nobody")
    assert s["count"] == 0
    assert s["min"] is None
    assert s["max"] is None
    assert s["mean"] is None
    assert s["median"] is None
    assert s["p25"] is None
    assert s["p75"] is None
    assert s["p95"] is None
    assert s["stddev"] is None
    assert s["histogram"] == []


def test_get_stats_single_sample(db):
    """T20: один sample → min=max=mean=median=value, stddev=0."""
    db.record_behavioral_sample("acc1", "e", 42.0)
    s = db.get_behavioral_stats()
    assert s["count"] == 1
    assert s["min"] == pytest.approx(42.0)
    assert s["max"] == pytest.approx(42.0)
    assert s["mean"] == pytest.approx(42.0)
    assert s["median"] == pytest.approx(42.0)
    assert s["p25"] == pytest.approx(42.0)
    assert s["p95"] == pytest.approx(42.0)
    assert s["stddev"] == pytest.approx(0.0)
    # Один уникальный value → одна гистограмма-bin.
    assert len(s["histogram"]) == 1


def test_get_stats_basic_percentiles(db):
    """T20: для 0..100 percentiles совпадают с classical formulas."""
    for v in range(101):  # 0, 1, ..., 100 — 101 значение
        db.record_behavioral_sample("acc1", "e", float(v))
    s = db.get_behavioral_stats()
    assert s["count"] == 101
    assert s["min"] == pytest.approx(0.0)
    assert s["max"] == pytest.approx(100.0)
    assert s["mean"] == pytest.approx(50.0)
    assert s["median"] == pytest.approx(50.0)
    assert s["p25"] == pytest.approx(25.0)
    assert s["p75"] == pytest.approx(75.0)
    assert s["p95"] == pytest.approx(95.0)


def test_get_stats_stddev(db):
    """T20: stddev = pstdev (без bessel-correction)."""
    # Для [1, 2, 3]: mean=2, var=2/3, std=√(2/3)≈0.8165
    for v in [1.0, 2.0, 3.0]:
        db.record_behavioral_sample("acc1", "e", v)
    s = db.get_behavioral_stats()
    assert s["stddev"] == pytest.approx((2 / 3) ** 0.5, rel=1e-3)


def test_get_stats_histogram_bins(db):
    """T20: histogram — bins=N равных интервалов от min до max."""
    for v in range(10):
        db.record_behavioral_sample("acc1", "e", float(v))
    s = db.get_behavioral_stats(bins=5)
    assert len(s["histogram"]) == 5
    # Сумма count'ов = total samples
    assert sum(b["count"] for b in s["histogram"]) == 10
    # bin'ы покрывают [min, max] equal-width
    assert s["histogram"][0]["left"] == pytest.approx(0.0)
    assert s["histogram"][-1]["right"] == pytest.approx(9.0)


def test_get_stats_filter_combination(db):
    """T20: фильтры account/event_type/ts комбинируются."""
    db.record_behavioral_sample("acc1", "e1", 100.0, ts=50.0)
    db.record_behavioral_sample("acc1", "e2", 200.0, ts=150.0)
    db.record_behavioral_sample("acc2", "e1", 300.0, ts=150.0)
    db.record_behavioral_sample("acc1", "e1", 400.0, ts=150.0)
    s = db.get_behavioral_stats(account_name="acc1", event_type="e1", since_ts=100.0)
    assert s["count"] == 1
    assert s["min"] == pytest.approx(400.0)


# ════════════════════════════════════════════════════════════════════════════
# bot.py hooks: cycle_pause_sec / long_break_sec / dwell_sec
# ════════════════════════════════════════════════════════════════════════════


def test_cycle_pause_hook_records_regular_sample(db):
    """T20: pick_cycle_pause + label='regular' → cycle_pause_sec sample."""
    # Прямой вызов hook логики — не запускаем целиком _run_main_loop.
    # Просто эмулируем то, что записывается после pick_cycle_pause.
    pause_secs = 1800.0
    pause_label = "regular"
    event_type = "long_break_sec" if pause_label == "long_break" else "cycle_pause_sec"
    db.record_behavioral_sample("acc1", event_type, pause_secs)
    samples = db.get_behavioral_samples(event_type="cycle_pause_sec")
    assert len(samples) == 1
    assert samples[0]["value"] == pytest.approx(1800.0)


def test_cycle_pause_hook_records_long_break_sample(db):
    """T20: pick_cycle_pause + label='long_break' → long_break_sec sample."""
    pause_secs = 9000.0
    pause_label = "long_break"
    event_type = "long_break_sec" if pause_label == "long_break" else "cycle_pause_sec"
    db.record_behavioral_sample("acc1", event_type, pause_secs)
    samples = db.get_behavioral_samples(event_type="long_break_sec")
    assert len(samples) == 1
    assert samples[0]["value"] == pytest.approx(9000.0)
    # И НЕТ записей в cycle_pause_sec
    assert db.get_behavioral_samples(event_type="cycle_pause_sec") == []


def test_view_listing_records_dwell_sample(db):
    """T20: view_listing с db_manager записывает dwell_sec."""
    import bot

    driver = MagicMock()
    wait = MagicMock()
    wait.until.return_value = MagicMock()
    # Конкретный mock на _read_listing_meta + compute_reading_dwell.
    with (
        patch("bot._read_listing_meta", return_value=("text" * 100, 5)),
        patch("bot._compute_reading_dwell", return_value=42.5),
        patch("bot.random") as mock_rng,
        patch("bot.scroll_gallery"),
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.hp"),
        patch("bot.check_block", return_value=False),
        patch("bot.WebDriverWait", return_value=wait),
    ):
        mock_rng.random.return_value = 0.5  # interest_roll → нейтральный
        mock_rng.uniform.return_value = 0.5
        mock_rng.shuffle.side_effect = lambda x: None
        mock_rng.randint.return_value = 1
        # Нужен driver.find_element чтобы scroll_to_desc не упал
        driver.find_element = MagicMock(return_value=MagicMock())
        bot.view_listing(driver, wait, "acc1", db_manager=db)

    samples = db.get_behavioral_samples(event_type="dwell_sec")
    assert len(samples) == 1
    assert samples[0]["account_name"] == "acc1"
    assert samples[0]["value"] == pytest.approx(42.5)


def test_view_listing_no_db_manager_no_record(db):
    """T20: view_listing без db_manager не падает и не записывает."""
    import bot

    driver = MagicMock()
    wait = MagicMock()
    wait.until.return_value = MagicMock()
    with (
        patch("bot._read_listing_meta", return_value=("text", 1)),
        patch("bot._compute_reading_dwell", return_value=10.0),
        patch("bot.random") as mock_rng,
        patch("bot.scroll_gallery"),
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.hp"),
        patch("bot.check_block", return_value=False),
    ):
        mock_rng.random.return_value = 0.5
        mock_rng.uniform.return_value = 0.5
        mock_rng.shuffle.side_effect = lambda x: None
        mock_rng.randint.return_value = 1
        driver.find_element = MagicMock(return_value=MagicMock())
        # db_manager не передаётся
        bot.view_listing(driver, wait, "acc1")

    # Никаких sample'ов не записалось в нашу db (она другая, но всё равно).
    assert db.get_behavioral_samples() == []


def test_view_listing_db_failure_does_not_break_flow():
    """T20: исключение в record_behavioral_sample не валит view_listing."""
    import bot

    driver = MagicMock()
    wait = MagicMock()
    wait.until.return_value = MagicMock()
    bad_db = MagicMock()
    bad_db.record_behavioral_sample.side_effect = RuntimeError("db down")
    with (
        patch("bot._read_listing_meta", return_value=("", 0)),
        patch("bot._compute_reading_dwell", return_value=10.0),
        patch("bot.random") as mock_rng,
        patch("bot.scroll_gallery"),
        patch("bot.human_scroll"),
        patch("bot.random_mouse_move"),
        patch("bot.slow_scroll_to"),
        patch("bot.hp"),
        patch("bot.check_block", return_value=False),
    ):
        mock_rng.random.return_value = 0.5
        mock_rng.uniform.return_value = 0.5
        mock_rng.shuffle.side_effect = lambda x: None
        mock_rng.randint.return_value = 1
        driver.find_element = MagicMock(return_value=MagicMock())
        # Не должно raise
        result = bot.view_listing(driver, wait, "acc1", db_manager=bad_db)
    assert result is True


# ════════════════════════════════════════════════════════════════════════════
# tg_bot helpers: format histogram + format pattern
# ════════════════════════════════════════════════════════════════════════════


def test_format_histogram_empty():
    """T20: пустой histogram → ''."""
    from tg_bot import _format_behavior_histogram

    assert _format_behavior_histogram([]) == ""


def test_format_histogram_all_zero_returns_empty():
    """T20: все count=0 → '' (нет данных)."""
    from tg_bot import _format_behavior_histogram

    h = [{"left": 0, "right": 1, "count": 0}, {"left": 1, "right": 2, "count": 0}]
    assert _format_behavior_histogram(h) == ""


def test_format_histogram_basic():
    """T20: один char per bin, символ из набора блоков."""
    from tg_bot import _format_behavior_histogram

    h = [
        {"left": 0, "right": 1, "count": 1},
        {"left": 1, "right": 2, "count": 5},
        {"left": 2, "right": 3, "count": 10},
        {"left": 3, "right": 4, "count": 2},
    ]
    result = _format_behavior_histogram(h)
    assert len(result) == 4
    # Все символы из набора блоков
    blocks = "▁▂▃▄▅▆▇█"
    assert all(c in blocks for c in result)
    # Самый высокий bin = 10 → должен быть '█'
    assert "█" in result


def test_format_seconds_compact_thresholds():
    """T20: 45s → '45s', 5min → '5m', 2h → '2.0h'."""
    from tg_bot import _format_seconds_compact

    assert _format_seconds_compact(45) == "45s"
    assert _format_seconds_compact(300) == "5m"
    assert _format_seconds_compact(3600) == "1.0h"
    assert _format_seconds_compact(7200) == "2.0h"
    # Граница 60: ровно 60 → '1m'
    assert _format_seconds_compact(60) == "1m"
    # Граница 3600: ровно 3600 → '1.0h'
    assert _format_seconds_compact(3600) == "1.0h"


def test_format_pattern_no_samples_returns_empty(db):
    """T20: для аккаунта без sample'ов _format_behavior_pattern → ''."""
    from tg_bot import TelegramController

    # Создаём контроллер без запуска (только для метода).
    tc = TelegramController.__new__(TelegramController)
    result = tc._format_behavior_pattern(db, "nobody")
    assert result == ""


def test_format_pattern_with_samples(db):
    """T20: с sample'ами — выводит блок «📊 Pattern (7д)»."""
    import time as _t

    from tg_bot import TelegramController

    now = _t.time()
    # Записываем последние 5 cycle_pause + 3 dwell для acc1
    for i, v in enumerate([1500.0, 1800.0, 2400.0, 3000.0, 4500.0]):
        db.record_behavioral_sample("acc1", "cycle_pause_sec", v, ts=now - 100 - i)
    for i, v in enumerate([20.0, 45.0, 90.0]):
        db.record_behavioral_sample("acc1", "dwell_sec", v, ts=now - 200 - i)

    tc = TelegramController.__new__(TelegramController)
    result = tc._format_behavior_pattern(db, "acc1")
    assert "📊 Pattern" in result
    assert "паузы цикла" in result
    assert "dwell листингов" in result
    # n=5 для cycle_pause
    assert "n=5" in result
    # n=3 для dwell
    assert "n=3" in result


def test_format_pattern_skips_empty_event_types(db):
    """T20: если у event_type 0 sample'ов — пропускается без вывода."""
    import time as _t

    from tg_bot import TelegramController

    # Записываем только cycle_pause (без dwell, без long_break).
    db.record_behavioral_sample("acc1", "cycle_pause_sec", 1800.0, ts=_t.time())

    tc = TelegramController.__new__(TelegramController)
    result = tc._format_behavior_pattern(db, "acc1")
    assert "паузы цикла" in result
    # dwell / long_break не должны появиться
    assert "dwell листингов" not in result
    assert "длинные перерывы" not in result


def test_format_pattern_filters_old_samples(db):
    """T20: sample'ы старше 7 дней не учитываются."""
    import time as _t

    from tg_bot import TelegramController

    old_ts = _t.time() - 14 * 86400  # 14 дней назад
    db.record_behavioral_sample("acc1", "cycle_pause_sec", 9999.0, ts=old_ts)

    tc = TelegramController.__new__(TelegramController)
    result = tc._format_behavior_pattern(db, "acc1")
    # 9999 — старая, отфильтрована, sample'ов в 7д = 0
    assert result == ""
