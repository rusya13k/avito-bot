"""
F1: тесты ключевых инвариантов DatabaseManager.

Покрытие:
- C4: транзакции (commit/rollback атомарность)
- A2: upsert_listing с COALESCE (не затирать ранее сохранённые поля)
- A3: mark_listing_parse_status (включая создание минимальной записи)
- E3: get_daily_summary
- E2: incr_metric / get_metrics / get_metric_value
- C3: is_new_listing
"""

import pytest

# ── Базовые операции ────────────────────────────────────────────────


def test_upsert_listing_returns_id(db):
    lid = db.upsert_listing(
        url="https://t/1",
        category="c",
        area=10,
        price=100,
        location="loc",
        description="d",
        date_parsed="2024-01-01",
        date_published="2024-01-01",
        date_scraped="2024-01-01",
    )
    assert isinstance(lid, int)
    fetched = db.get_listing_by_url("https://t/1")
    assert fetched is not None
    assert fetched["price"] == 100


def test_is_new_listing(db):
    assert db.is_new_listing("https://t/1") is True
    db.upsert_listing(
        url="https://t/1",
        category="c",
        area=10,
        price=100,
        location="l",
        description="d",
        date_parsed="2024-01-01",
        date_published="2024-01-01",
        date_scraped="2024-01-01",
    )
    assert db.is_new_listing("https://t/1") is False
    assert db.is_new_listing("https://t/2") is True


def test_upsert_listing_coalesce_does_not_clobber(db):
    """A2: повторный парсинг с None-полями НЕ затирает уже спарсенные данные."""
    db.upsert_listing(
        url="https://t/1",
        category="c",
        area=10,
        price=100,
        location="MSK",
        description="full",
        date_parsed="2024-01-01",
        date_published="2024-01-01",
        date_scraped="2024-01-01",
        seller_name="Иван",
        phone="+79991234567",
    )
    # Повторный парсинг, который не достал seller_name и phone
    db.upsert_listing(
        url="https://t/1",
        category="c",
        area=10,
        price=200,
        location=None,
        description=None,
        date_parsed="2024-02-01",
        date_published="2024-02-01",
        date_scraped="2024-02-01",
        seller_name=None,
        phone=None,
    )
    listing = db.get_listing_by_url("https://t/1")
    # цена обновилась
    assert listing["price"] == 200
    # старые поля сохранились (COALESCE)
    assert listing["seller_name"] == "Иван"
    assert listing["phone"] == "+79991234567"
    assert listing["location"] == "MSK"
    assert listing["description"] == "full"


# ── C4: транзакции ────────────────────────────────────────────────


def test_transaction_commits_on_success(db):
    with db.transaction() as cur:
        lid = db.upsert_listing(
            url="https://t/ok",
            category="c",
            area=10,
            price=100,
            location="l",
            description="d",
            date_parsed="2024-01-01",
            date_published="2024-01-01",
            date_scraped="2024-01-01",
            phone="+79991111111",
            cursor=cur,
        )
        db.upsert_phone(phone_normalized="+79991111111", cursor=cur)
    assert db.get_listing_by_url("https://t/ok") is not None
    assert db.get_phone_count("+79991111111") == 1
    assert isinstance(lid, int)


def test_transaction_rolls_back_on_exception(db):
    """C4: если падаем посреди транзакции, ничего не должно быть закоммичено."""
    with pytest.raises(RuntimeError):
        with db.transaction() as cur:
            db.upsert_listing(
                url="https://t/rb",
                category="c",
                area=10,
                price=100,
                location="l",
                description="d",
                date_parsed="2024-01-01",
                date_published="2024-01-01",
                date_scraped="2024-01-01",
                phone="+79992222222",
                cursor=cur,
            )
            db.upsert_phone(phone_normalized="+79992222222", cursor=cur)
            raise RuntimeError("simulated crash")

    assert db.get_listing_by_url("https://t/rb") is None
    assert db.get_phone_count("+79992222222") == 0


def test_methods_work_without_cursor_arg(db):
    """Backward-compat: cursor=None — стандартный путь, своё соединение."""
    db.upsert_listing(
        url="https://t/auto",
        category="c",
        area=10,
        price=100,
        location="l",
        description="d",
        date_parsed="2024-01-01",
        date_published="2024-01-01",
        date_scraped="2024-01-01",
    )
    assert db.get_listing_by_url("https://t/auto") is not None


# ── A3: parse_status ────────────────────────────────────────────────


def test_mark_listing_parse_status_existing(db):
    lid = db.upsert_listing(
        url="https://t/1",
        category="c",
        area=10,
        price=100,
        location="l",
        description="d",
        date_parsed="2024-01-01",
        date_published="2024-01-01",
        date_scraped="2024-01-01",
    )
    db.mark_listing_parse_status(url="https://t/1", status="ok", listing_id=lid)
    assert db.get_listing_by_id(lid)["parse_status"] == "ok"


def test_mark_listing_parse_status_creates_stub(db):
    """A3: если листинга нет — создаётся минимальная запись для трекинга."""
    new_id = db.mark_listing_parse_status(url="https://t/captcha", status="captcha")
    assert isinstance(new_id, int)
    listing = db.get_listing_by_url("https://t/captcha")
    assert listing is not None
    assert listing["parse_status"] == "captcha"


# ── E3: get_daily_summary ────────────────────────────────────────────────


def test_get_daily_summary(db):
    import time

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    today = time.strftime("%Y-%m-%d 00:00:00")

    with db.transaction() as cur:
        l1 = db.upsert_listing(
            url="https://t/1",
            category="c",
            area=10,
            price=100,
            location="l",
            description="d",
            date_parsed=now,
            date_published=now,
            date_scraped=now,
            cursor=cur,
        )
        db.mark_listing_parse_status(url="https://t/1", status="ok", listing_id=l1, cursor=cur)
        l2 = db.upsert_listing(
            url="https://t/2",
            category="c",
            area=10,
            price=100,
            location="l",
            description="d",
            date_parsed=now,
            date_published=now,
            date_scraped=now,
            cursor=cur,
        )
        db.mark_listing_parse_status(url="https://t/2", status="captcha", listing_id=l2, cursor=cur)

    db.update_listing_classification(l1, "owner", 0.9, "heuristic", now)

    summary = db.get_daily_summary(today)
    assert summary["listings_parsed"] == 2
    assert summary["listings_ok"] == 1
    assert summary["listings_captcha"] == 1
    assert summary["classified_owner"] == 1
    assert summary["classified_agent"] == 0


# ── B3: dialog upsert + idempotent message ───────────────────────────────


def test_add_message_is_idempotent(db):
    """add_message — защита от дубля по (dialog_id, direction, text, timestamp)."""
    did = db.upsert_dialog(
        our_account="acc",
        visitor_id="v1",
        listing_id=None,
        status="active",
        last_message_text="hi",
        last_message_time="2024-01-01 12:00:00",
    )
    db.add_message(did, "in", "hello", "2024-01-01 12:00:00")
    db.add_message(did, "in", "hello", "2024-01-01 12:00:00")  # дубль
    db.add_message(did, "in", "hello", "2024-01-01 12:00:01")  # ts другой
    msgs = db.get_messages(did)
    assert len(msgs) == 2


# ── F5: get_first_in_message_age_seconds ──────────────────────────────────


def test_get_first_in_message_age_returns_none_without_message(db):
    """F5: если в диалоге нет in-сообщения с таким текстом → None."""
    did = db.upsert_dialog(
        our_account="acc",
        visitor_id="v1",
        listing_id=None,
        status="active",
        last_message_text="x",
        last_message_time="2024-01-01 12:00:00",
    )
    assert db.get_first_in_message_age_seconds(did, "missing") is None


def test_get_first_in_message_age_uses_min_timestamp(db):
    """F5: при дубликатах одного сообщения с разными timestamp возвращаем
    возраст самого раннего (когда мы ВПЕРВЫЕ его увидели)."""
    import datetime

    did = db.upsert_dialog(
        our_account="acc",
        visitor_id="v1",
        listing_id=None,
        status="active",
        last_message_text="hi",
        last_message_time="2024-01-01 12:00:00",
    )
    # Считаем возраст относительно «сейчас» — поэтому используем
    # детерминированные относительные timestamp'ы.
    now = datetime.datetime.now()
    long_ago = (now - datetime.timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")
    recent = (now - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

    db.add_message(did, "in", "hello", long_ago)
    db.add_message(did, "in", "hello", recent)  # дубль с другим ts

    age_seconds = db.get_first_in_message_age_seconds(did, "hello")
    assert age_seconds is not None
    # Возраст ≈ 120 мин = 7200 секунд (с погрешностью на test latency).
    assert 7100 < age_seconds < 7300


def test_get_first_in_message_age_ignores_out_messages(db):
    """F5: считаем возраст только in-сообщений; out не учитываем."""
    import datetime

    did = db.upsert_dialog(
        our_account="acc",
        visitor_id="v1",
        listing_id=None,
        status="active",
        last_message_text="x",
        last_message_time="2024-01-01 12:00:00",
    )
    long_ago = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.add_message(did, "out", "hello", long_ago)  # это OUT — игнорируем
    assert db.get_first_in_message_age_seconds(did, "hello") is None


def test_get_first_in_message_age_handles_corrupt_timestamp(db):
    """F5: невалидный timestamp в БД (например, миграционная грязь) —
    возвращаем None, не падаем."""
    import sqlite3

    did = db.upsert_dialog(
        our_account="acc",
        visitor_id="v1",
        listing_id=None,
        status="active",
        last_message_text="x",
        last_message_time="2024-01-01 12:00:00",
    )
    # add_message парсит/валидирует только при чтении, поэтому пишем напрямую.
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO messages (dialog_id, direction, text, timestamp) VALUES (?, 'in', 'hello', 'NOT-A-DATE')",
            (did,),
        )
        conn.commit()
    assert db.get_first_in_message_age_seconds(did, "hello") is None


# ── E2: metrics ────────────────────────────────────────────────


def test_incr_metric_creates_and_increments(db):
    """incr_metric: первый вызов INSERT, второй — UPDATE += by."""
    assert db.get_metric_value("acc1", "listings_parsed") == 0
    db.incr_metric("acc1", "listings_parsed")
    db.incr_metric("acc1", "listings_parsed")
    db.incr_metric("acc1", "listings_parsed", by=3)
    assert db.get_metric_value("acc1", "listings_parsed") == 5


def test_incr_metric_per_account_isolation(db):
    """Разные аккаунты — независимые счётчики; "" — глобальная метрика."""
    db.incr_metric("acc1", "listings_parsed")
    db.incr_metric("acc2", "listings_parsed", by=2)
    db.incr_metric("", "llm_errors", by=7)
    assert db.get_metric_value("acc1", "listings_parsed") == 1
    assert db.get_metric_value("acc2", "listings_parsed") == 2
    assert db.get_metric_value("", "llm_errors") == 7
    # Перекрёстная изоляция: acc1 не видит llm_errors аккаунта ""
    assert db.get_metric_value("acc1", "llm_errors") == 0


def test_incr_metric_empty_name_is_noop(db):
    """metric="" — мусорный вызов, не должен ничего записывать."""
    db.incr_metric("acc1", "", by=5)
    rows = db.get_metrics()
    assert rows == []


def test_incr_metric_in_transaction_rolls_back(db):
    """E2 + C4: метрика в транзакции откатывается вместе с остальным."""
    with pytest.raises(RuntimeError):
        with db.transaction() as cur:
            db.incr_metric("acc1", "listings_parsed", cursor=cur)
            raise RuntimeError("boom")
    assert db.get_metric_value("acc1", "listings_parsed") == 0


def test_incr_metric_in_transaction_commits(db):
    """E2 + C4: метрика в транзакции коммитится вместе с остальным."""
    with db.transaction() as cur:
        db.upsert_listing(
            url="https://t/m",
            category="c",
            area=10,
            price=100,
            location="l",
            description="d",
            date_parsed="2024-01-01",
            date_published="2024-01-01",
            date_scraped="2024-01-01",
            cursor=cur,
        )
        db.incr_metric("acc1", "listings_parsed", cursor=cur)
    assert db.get_listing_by_url("https://t/m") is not None
    assert db.get_metric_value("acc1", "listings_parsed") == 1


def test_get_metrics_filters_and_aggregations(db):
    """get_metrics с фильтрами и group_by=hour|day|metric."""
    import time as _t

    # Засеваем разные часы и дни через ts
    base = _t.mktime(_t.strptime("2024-01-01 10:30:00", "%Y-%m-%d %H:%M:%S"))
    db.incr_metric("acc1", "listings_ok", by=2, ts=base)
    db.incr_metric("acc1", "listings_ok", by=1, ts=base + 3600)  # 11:00
    db.incr_metric("acc2", "listings_ok", by=5, ts=base)
    db.incr_metric("acc1", "listings_captcha", by=1, ts=base + 86400)  # +1d

    # group_by=hour без фильтров — все четыре строки.
    hourly = db.get_metrics(group_by="hour")
    assert len(hourly) == 4

    # фильтр по аккаунту
    acc1_only = db.get_metrics(account_name="acc1")
    assert {r["metric"] for r in acc1_only} == {"listings_ok", "listings_captcha"}
    assert sum(r["value"] for r in acc1_only) == 4

    # фильтр по метрике + group_by=metric (агрегация per-account)
    by_metric = db.get_metrics(metric="listings_ok", group_by="metric")
    by_metric_dict = {r["account_name"]: r["value"] for r in by_metric}
    assert by_metric_dict == {"acc1": 3, "acc2": 5}

    # group_by=day
    daily = db.get_metrics(group_by="day", metric="listings_ok", account_name="acc1")
    # У acc1 listings_ok оба раза в один день -> один ряд value=3
    assert len(daily) == 1
    assert daily[0]["bucket"] == "2024-01-01"
    assert daily[0]["value"] == 3


def test_get_metrics_invalid_group_by_raises(db):
    db.incr_metric("acc1", "listings_ok")
    with pytest.raises(ValueError):
        db.get_metrics(group_by="week")


def test_get_daily_summary_includes_metric_counters(db):
    """get_daily_summary подмешивает llm_errors / captcha_hits / ... из metrics."""
    import time as _t

    today = _t.strftime("%Y-%m-%d 00:00:00")

    db.incr_metric("acc1", "llm_errors", by=3)
    db.incr_metric("acc1", "captcha_hits", by=1)
    db.incr_metric("acc1", "dialogs_handled", by=2)
    db.incr_metric("acc1", "messages_sent", by=4)

    s = db.get_daily_summary(today)
    assert s["llm_errors"] == 3
    assert s["captcha_hits"] == 1
    assert s["dialogs_handled"] == 2
    assert s["messages_sent"] == 4


# ── K3: get_classification_stats ────────────────────────────────────


def _add_classified(db, url, classification):
    """Helper: создаёт листинг и проставляет ему классификацию."""
    lid = db.upsert_listing(
        url=url,
        category="c",
        area=10,
        price=100,
        location="loc",
        description="d",
        date_parsed="2024-01-01",
        date_published="2024-01-01",
        date_scraped="2024-01-01",
    )
    if classification is not None:
        db.update_listing_classification(
            lid, classification, 0.9, "heuristic", "2024-01-01T00:00:00"
        )
    return lid


def test_get_classification_stats_empty(db):
    """Пустая БД → total=0, by_label={}."""
    stats = db.get_classification_stats()
    assert stats == {"by_label": {}, "total": 0}


def test_get_classification_stats_excludes_unclassified(db):
    """NULL и пустая classification не попадают в статистику."""
    _add_classified(db, "https://t/1", "owner")
    _add_classified(db, "https://t/2", None)  # unclassified
    _add_classified(db, "https://t/3", "")  # пустая (на случай legacy)

    stats = db.get_classification_stats()
    assert stats["by_label"] == {"owner": 1}
    assert stats["total"] == 1


def test_get_classification_stats_groups_by_label(db):
    _add_classified(db, "https://t/1", "owner")
    _add_classified(db, "https://t/2", "owner")
    _add_classified(db, "https://t/3", "agent")
    _add_classified(db, "https://t/4", "agent")
    _add_classified(db, "https://t/5", "agent")
    _add_classified(db, "https://t/6", "uncertain")

    stats = db.get_classification_stats()
    assert stats["by_label"] == {"owner": 2, "agent": 3, "uncertain": 1}
    assert stats["total"] == 6
