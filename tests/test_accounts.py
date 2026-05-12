"""
G2: тесты загрузчика accounts.json.

Покрытие:
- Чтение из accounts.json (приоритет)
- Fallback на cfg["accounts"] при отсутствии файла + deprecation warning
- Disabled-аккаунты (enabled=false) фильтруются
- alias adspower_id ↔ user_id
- Пропуск некорректных записей (без 'name', не dict, дубль имени)
- Невалидный JSON / не-список не валит загрузку
"""

import json
import logging

import pytest

from accounts import (
    ACCOUNTS_JSON_FILENAME,
    _normalize_account,
    get_account_overrides,
    load_accounts,
)


@pytest.fixture
def repo(tmp_path):
    """Изолированный repo_dir на тест."""
    return tmp_path


def _write_accounts(repo, data):
    """Помощник: записать accounts.json в репо."""
    (repo / ACCOUNTS_JSON_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Чтение из accounts.json ────────────────────────────────────────────────


def test_loads_from_accounts_json(repo):
    _write_accounts(
        repo,
        [
            {"name": "acc1", "adspower_id": "abc", "phone": "+7..."},
            {"name": "acc2", "user_id": "xyz"},  # старый ключ
        ],
    )
    accounts = load_accounts(repo)
    assert len(accounts) == 2
    assert accounts[0]["name"] == "acc1"
    assert accounts[0]["enabled"] is True
    # alias: adspower_id -> user_id (заполнили оба)
    assert accounts[0]["adspower_id"] == "abc"
    assert accounts[0]["user_id"] == "abc"
    # обратный alias: user_id -> adspower_id
    assert accounts[1]["user_id"] == "xyz"
    assert accounts[1]["adspower_id"] == "xyz"


def test_disabled_accounts_are_filtered(repo):
    _write_accounts(
        repo,
        [
            {"name": "active", "enabled": True},
            {"name": "off", "enabled": False},
        ],
    )
    accounts = load_accounts(repo)
    names = [a["name"] for a in accounts]
    assert names == ["active"]


def test_skips_entries_without_name(repo, caplog):
    _write_accounts(
        repo,
        [
            {"name": "ok"},
            {"phone": "+7..."},  # без name
            {"name": ""},  # пустое name
            "not a dict",  # вообще не dict
        ],
    )
    with caplog.at_level(logging.WARNING):
        accounts = load_accounts(repo)
    names = [a["name"] for a in accounts]
    assert names == ["ok"]
    # как минимум одно warning о пропущенной записи
    assert any("пропускаю" in r.message for r in caplog.records)


def test_duplicate_names_keeps_first(repo, caplog):
    _write_accounts(
        repo,
        [
            {"name": "dup", "phone": "1"},
            {"name": "dup", "phone": "2"},
        ],
    )
    with caplog.at_level(logging.WARNING):
        accounts = load_accounts(repo)
    assert len(accounts) == 1
    assert accounts[0]["phone"] == "1"
    assert any("дубль" in r.message for r in caplog.records)


def test_does_not_mutate_input(repo):
    """G2: load_accounts не должен мутировать исходные данные."""
    original = [{"name": "acc1", "user_id": "x"}]
    _write_accounts(repo, original)
    loaded_a = load_accounts(repo)
    loaded_b = load_accounts(repo)
    # Каждый вызов возвращает свою копию
    assert loaded_a is not loaded_b
    # И мутация результата не задевает дальнейшие вызовы
    loaded_a[0]["name"] = "MUTATED"
    fresh = load_accounts(repo)
    assert fresh[0]["name"] == "acc1"


def test_invalid_json_returns_empty(repo, caplog):
    (repo / ACCOUNTS_JSON_FILENAME).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        accounts = load_accounts(repo)
    assert accounts == []
    assert any("невалидный JSON" in r.message for r in caplog.records)


def test_non_list_root_returns_empty(repo, caplog):
    (repo / ACCOUNTS_JSON_FILENAME).write_text(
        '{"accounts": []}',
        encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR):
        accounts = load_accounts(repo)
    assert accounts == []
    assert any("список" in r.message for r in caplog.records)


# ── Fallback на cfg["accounts"] ────────────────────────────────────────────


def test_falls_back_to_cfg_when_file_missing(repo, caplog):
    cfg = {"accounts": [{"name": "legacy", "user_id": "u1"}]}
    with caplog.at_level(logging.WARNING):
        accounts = load_accounts(repo, cfg)
    assert len(accounts) == 1
    assert accounts[0]["name"] == "legacy"
    # должен быть deprecation warning
    assert any(
        "устаревший формат" in r.message or "config.json" in r.message for r in caplog.records
    )


def test_accounts_json_has_priority_over_cfg(repo):
    _write_accounts(repo, [{"name": "from_file"}])
    cfg = {"accounts": [{"name": "from_cfg"}]}
    accounts = load_accounts(repo, cfg)
    assert [a["name"] for a in accounts] == ["from_file"]


def test_no_source_returns_empty(repo, caplog):
    with caplog.at_level(logging.WARNING):
        accounts = load_accounts(repo, cfg=None)
    assert accounts == []


def test_cfg_without_accounts_key(repo, caplog):
    with caplog.at_level(logging.WARNING):
        accounts = load_accounts(repo, cfg={"unrelated": 1})
    assert accounts == []


# ── Хелперы ────────────────────────────────────────────


def test_normalize_account_returns_none_for_invalid():
    assert _normalize_account(None, index=0, source="x") is None
    assert _normalize_account(42, index=0, source="x") is None
    assert _normalize_account({}, index=0, source="x") is None
    assert _normalize_account({"name": "  "}, index=0, source="x") is None


def test_get_account_overrides():
    acc = {"name": "a", "captcha_cooldown_minutes": 60}
    assert get_account_overrides(acc, "captcha_cooldown_minutes") == 60
    assert get_account_overrides(acc, "missing", default=42) == 42


# ── Per-account captcha_cooldown override (account_state) ──────────────────


def test_account_state_per_account_cooldown_override():
    """G2: set_account_cooldown_minutes влияет на mark_captcha без явного arg'а."""
    from account_state import AccountState

    s = AccountState()
    s.set_account_cooldown_minutes("acc1", 60)  # 60 min
    until = s.mark_captcha("acc1")
    # within ~+60 минут от now (даём ±5 секунд погрешности)
    import time

    expected = time.time() + 60 * 60
    assert abs(until - expected) < 5

    # acc2 — без override, использует глобальный DEFAULT
    until2 = s.mark_captcha("acc2")
    # default — 30 мин (или то, что в module-уровневой константе)
    from account_state import DEFAULT_CAPTCHA_COOLDOWN_MINUTES

    expected2 = time.time() + DEFAULT_CAPTCHA_COOLDOWN_MINUTES * 60
    assert abs(until2 - expected2) < 5


def test_account_state_invalid_override_is_ignored():
    """G2: невалидное значение (строка / отрицательное) не ломает state."""
    from account_state import DEFAULT_CAPTCHA_COOLDOWN_MINUTES, AccountState

    s = AccountState()
    s.set_account_cooldown_minutes("acc1", "not a number")
    s.set_account_cooldown_minutes("acc1", -5)
    # override остался None -> используется default
    import time

    until = s.mark_captcha("acc1")
    expected = time.time() + DEFAULT_CAPTCHA_COOLDOWN_MINUTES * 60
    assert abs(until - expected) < 5
