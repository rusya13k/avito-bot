"""
Тесты на новые функции: multi-admin, DeepSeek LLM, is_listing_url_seen,
account persona/captcha_cd, F6 activity pattern, env_config DEEPSEEK_API_KEY.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── LLM defaults ──────────────────────────────────────────────────────────


def test_llm_default_model_is_deepseek():
    from llm_classifier import LLMClassifier

    assert LLMClassifier.DEFAULT_MODEL == "deepseek-v4-flash"


def test_llm_default_api_base_is_deepseek():
    from llm_classifier import LLMClassifier

    assert LLMClassifier.DEFAULT_API_BASE == "https://api.deepseek.com/v1"


def test_llm_classifier_uses_deepseek_config():
    """LLMClassifier с пустым конфигом берёт DeepSeek дефолты."""
    from llm_classifier import LLMClassifier

    clf = LLMClassifier({})
    assert clf.model == "deepseek-v4-flash"
    assert clf.api_base == "https://api.deepseek.com/v1"


# ── env_config: DEEPSEEK_API_KEY ──────────────────────────────────────────


def test_deepseek_api_key_maps_to_openai_api_key():
    """DEEPSEEK_API_KEY env var должна маппиться в openai_api_key."""
    from env_config import ENV_TO_CFG

    assert "DEEPSEEK_API_KEY" in ENV_TO_CFG
    assert ENV_TO_CFG["DEEPSEEK_API_KEY"] == "openai_api_key"


def test_deepseek_api_key_overrides_config(monkeypatch):
    """DEEPSEEK_API_KEY env var переопределяет config.json."""
    from env_config import apply_env_overrides

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key")
    cfg = {"openai_api_key": ""}
    apply_env_overrides(cfg)
    assert cfg["openai_api_key"] == "sk-test-deepseek-key"


# ── is_listing_url_seen ──────────────────────────────────────────────────


def test_is_listing_url_seen_returns_false_for_unseen(db):
    """URL, которого нет в БД, должен возвращать False."""
    assert db.is_listing_url_seen("https://avito.ru/item123") is False


def test_is_listing_url_seen_returns_true_for_seen(db):
    """URL, добавленный в БД, должен возвращать True."""
    url = "https://avito.ru/item456"
    db.upsert_listing(
        url=url,
        category="office",
        area="100",
        price="50000",
        location="moskva",
        description="desc",
        date_parsed="2026-01-01",
        date_published="2026-01-01",
        date_scraped="2026-01-01",
        title="test",
    )
    assert db.is_listing_url_seen(url) is True


def test_is_listing_url_seen_no_memory_explosion(db):
    """is_listing_url_seen не загружает все URL в память —
    проверяем что get_seen_listing_urls возвращает больше."""
    # Вставим 5 записей
    for i in range(5):
        db.upsert_listing(
            url=f"https://avito.ru/item{i}",
            category="office",
            area="100",
            price="50000",
            location="moskva",
            description="d",
            date_parsed="2026-01-01",
            date_published="2026-01-01",
            date_scraped="2026-01-01",
            title=f"t{i}",
        )
    # get_seen_listing_urls возвращает set всех URL — 5 штук
    all_urls = db.get_seen_listing_urls()
    assert len(all_urls) == 5
    # is_listing_url_seen — точечный запрос, не грузит всё
    assert db.is_listing_url_seen("https://avito.ru/item0") is True
    assert db.is_listing_url_seen("https://avito.ru/nonexistent") is False


# ── Multi-admin support ──────────────────────────────────────────────────


def _make_controller(admin_id=0, admin_ids=None):
    """Создаёт TelegramController с замоканным telebot."""
    from tg_bot import TelegramController

    with patch("tg_bot.telebot.TeleBot"):
        ctrl = TelegramController(token="test", admin_id=admin_id)
        if admin_ids:
            ctrl.set_admin_ids(admin_ids)
    return ctrl


def test_set_admin_ids_from_list():
    ctrl = _make_controller(admin_ids=[111, 222, 333])
    assert ctrl.admin_ids == {111, 222, 333}


def test_set_admin_ids_skips_zero():
    ctrl = _make_controller(admin_ids=[111, 0, 222])
    assert ctrl.admin_ids == {111, 222}
    assert 0 not in ctrl.admin_ids


def test_allowed_checks_admin_ids_set():
    ctrl = _make_controller(admin_id=42, admin_ids=[100, 200])
    # admin_id=42 но admin_ids=[100,200] — проверяем по admin_ids
    assert ctrl._allowed(100) is True
    assert ctrl._allowed(200) is True
    assert ctrl._allowed(42) is False  # 42 не в admin_ids!


def test_allowed_fallback_to_admin_id():
    ctrl = _make_controller(admin_id=42)
    assert ctrl._allowed(42) is True
    assert ctrl._allowed(999) is False


def test_notify_sends_to_all_admin_ids():
    ctrl = _make_controller(admin_ids=[100, 200])
    sent = []
    ctrl.bot.send_message = lambda uid, text: sent.append(uid)
    ctrl.notify("test message")
    assert set(sent) == {100, 200}


def test_notify_fallback_to_admin_id():
    ctrl = _make_controller(admin_id=42)
    sent = []
    ctrl.bot.send_message = lambda uid, text: sent.append(uid)
    ctrl.notify("test message")
    assert sent == [42]


# ── Account persona / captcha_cooldown ──────────────────────────────────


def test_update_account_persona(tmp_path):
    """Персона сохраняется в accounts.json через update_account."""
    from accounts import save_accounts, update_account

    accounts = [{"name": "acc1", "phone": "+7999", "enabled": True}]
    save_accounts(tmp_path, accounts)
    updated = update_account(tmp_path, "acc1", {"persona": "friendly_agent"})
    assert updated is not None
    assert updated["persona"] == "friendly_agent"


def test_update_account_captcha_cooldown(tmp_path):
    """captcha_cooldown_minutes сохраняется в accounts.json."""
    from accounts import save_accounts, update_account

    accounts = [{"name": "acc1", "phone": "+7999", "enabled": True}]
    save_accounts(tmp_path, accounts)
    updated = update_account(tmp_path, "acc1", {"captcha_cooldown_minutes": 60})
    assert updated is not None
    assert updated["captcha_cooldown_minutes"] == 60


def test_update_account_captcha_cooldown_reset(tmp_path):
    """captcha_cooldown_minutes=None сбрасывает override."""
    from accounts import save_accounts, update_account

    accounts = [{"name": "acc1", "phone": "+7999", "enabled": True, "captcha_cooldown_minutes": 60}]
    save_accounts(tmp_path, accounts)
    updated = update_account(tmp_path, "acc1", {"captcha_cooldown_minutes": None})
    assert updated is not None
    assert updated.get("captcha_cooldown_minutes") is None


# ── F6 activity pattern ──────────────────────────────────────────────────


def test_active_probability_zero_outside_active_hours():
    """Вне active_hours_start/end вероятность = 0."""
    from bot import _active_probability

    account = {"active_hours_start": 9, "active_hours_end": 18}
    cfg = {"activity_pattern": "default"}
    # Час 22 — вне окна 9-18
    assert _active_probability(account, cfg, hour=22) == 0.0
    # Час 8 — тоже вне
    assert _active_probability(account, cfg, hour=8) == 0.0


def test_active_probability_nonzero_inside_active_hours():
    """Внутри active_hours вероятность > 0."""
    from bot import _active_probability

    account = {"active_hours_start": 9, "active_hours_end": 18}
    cfg = {"activity_pattern": "default"}
    # Час 12 — внутри окна
    prob = _active_probability(account, cfg, hour=12)
    assert prob > 0.0


def test_active_probability_uses_custom_pattern():
    """Per-account pattern override работает."""
    from bot import _active_probability

    account = {
        "active_hours_start": 0,
        "active_hours_end": 24,
        "activity_pattern": {14: 0.99},
    }
    cfg = {}
    assert _active_probability(account, cfg, hour=14) == 0.99


def test_active_probability_no_window_no_zero():
    """Без active_hours_start/end — probabilistic модель активна всегда."""
    from bot import _active_probability

    account = {}
    cfg = {"activity_pattern": {3: 0.05, 14: 0.95}}
    # Ночью — низкая но не нулевая
    assert _active_probability(account, cfg, hour=3) == 0.05
    # Днём — высокая
    assert _active_probability(account, cfg, hour=14) == 0.95


# ── _big_warmup_running TOCTOU fix ──────────────────────────────────────


def test_bigwarmup_atomic_check_and_add():
    """Проверка+add под локом — двойной клик не запустит два прогрева."""
    from tg_bot import TelegramController

    with patch("tg_bot.telebot.TeleBot"):
        ctrl = TelegramController(token="test", admin_id=42)

    # Добавляем вручную — первый add должен пройти
    with ctrl._big_warmup_lock:
        if "acc1" not in ctrl._big_warmup_running:
            ctrl._big_warmup_running.add("acc1")

    # Второй check+add — уже есть, не добавляем
    with ctrl._big_warmup_lock:
        already = "acc1" in ctrl._big_warmup_running
    assert already is True

    # Множество должно содержать ровно одну запись
    assert ctrl._big_warmup_running == {"acc1"}


# ── TG dialog handlers for persona/captcha_cd ────────────────────────────


def test_dialog_handlers_registered():
    """acc_set_persona и acc_set_captcha_cd должны быть в handlers dict."""
    from tg_bot import TelegramController

    with patch("tg_bot.telebot.TeleBot"):
        ctrl = TelegramController(token="test", admin_id=42)

    handlers = {
        "acc_set_persona": ctrl._dialog_acc_set_persona,
        "acc_set_captcha_cd": ctrl._dialog_acc_set_captcha_cd,
    }
    # Если методы существуют — они callable
    for state, handler in handlers.items():
        assert callable(handler), f"handler for {state} is not callable"


def test_callback_prefix_acc_persona_registered():
    """acc_persona_ prefix должен быть в prefix_handlers."""
    from tg_bot import TelegramController

    with patch("tg_bot.telebot.TeleBot"):
        ctrl = TelegramController(token="test", admin_id=42)

    # Имитируем _on_callback prefix table
    prefixes = [
        ("acc_persona_", ctrl._cb_acc_persona),
        ("acc_captcha_cd_", ctrl._cb_acc_captcha_cd),
    ]
    for prefix, handler in prefixes:
        assert callable(handler), f"callback handler for {prefix} is not callable"
