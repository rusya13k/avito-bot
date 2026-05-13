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


# ──────────────────────────────────────────────────────────────────────
# K1: load_all_accounts + CRUD (save/add/update/remove)
# ──────────────────────────────────────────────────────────────────────


def test_load_all_accounts_includes_disabled(repo):
    """K1: load_all_accounts возвращает И disabled-аккаунты — для UI TG-бота."""
    from accounts import load_all_accounts

    _write_accounts(
        repo,
        [
            {"name": "acc1"},
            {"name": "acc2", "enabled": False},
            {"name": "acc3", "enabled": True},
        ],
    )
    all_accs = load_all_accounts(repo)
    names = [a["name"] for a in all_accs]
    assert names == ["acc1", "acc2", "acc3"]
    assert all_accs[0]["enabled"] is True
    assert all_accs[1]["enabled"] is False  # сохранён как есть
    assert all_accs[2]["enabled"] is True

    # А load_accounts (без _all_) — отфильтрует disabled
    visible = load_accounts(repo)
    assert [a["name"] for a in visible] == ["acc1", "acc3"]


def test_load_all_accounts_falls_back_to_cfg(repo):
    """K1: если accounts.json нет, читаем cfg['accounts'] — но БЕЗ фильтра."""
    from accounts import load_all_accounts

    cfg = {"accounts": [{"name": "a", "enabled": False}, {"name": "b"}]}
    accs = load_all_accounts(repo, cfg)
    names = [a["name"] for a in accs]
    assert names == ["a", "b"]


def test_save_accounts_writes_and_load_roundtrips(repo):
    from accounts import load_all_accounts, save_accounts

    data = [
        {"name": "alpha", "adspower_id": "a1", "enabled": True},
        {"name": "beta", "adspower_id": "b1", "enabled": False, "phone": "+7..."},
    ]
    target = save_accounts(repo, data)
    assert target.exists()
    assert target.name == ACCOUNTS_JSON_FILENAME

    # Файл читаемый JSON
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == data

    # load_all_accounts возвращает то же содержимое (с alias-нормализацией)
    loaded = load_all_accounts(repo)
    assert [a["name"] for a in loaded] == ["alpha", "beta"]
    # alias adspower_id → user_id
    assert loaded[0]["user_id"] == "a1"


def test_save_accounts_validates_each_item(repo):
    from accounts import save_accounts

    with pytest.raises(TypeError):
        save_accounts(repo, ["not a dict"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        save_accounts(repo, [{"name": ""}])
    with pytest.raises(ValueError):
        save_accounts(repo, [{"adspower_id": "x"}])  # нет name


def test_add_account_appends_and_persists(repo):
    from accounts import add_account, load_all_accounts

    _write_accounts(repo, [{"name": "existing"}])
    add_account(repo, {"name": "new_acc", "phone": "+71112223344"})

    accs = load_all_accounts(repo)
    names = [a["name"] for a in accs]
    assert names == ["existing", "new_acc"]


def test_add_account_rejects_duplicate(repo):
    from accounts import add_account

    _write_accounts(repo, [{"name": "existing"}])
    with pytest.raises(ValueError, match="уже существует"):
        add_account(repo, {"name": "existing"})


def test_add_account_rejects_empty_name(repo):
    from accounts import add_account

    with pytest.raises(ValueError, match="обязателен"):
        add_account(repo, {"name": "   "})


def test_add_account_creates_file_if_missing(repo):
    """K1: первый add_account при отсутствии accounts.json — создаёт файл."""
    from accounts import add_account

    assert not (repo / ACCOUNTS_JSON_FILENAME).exists()
    add_account(repo, {"name": "first"})
    assert (repo / ACCOUNTS_JSON_FILENAME).exists()


def test_add_account_migrates_from_cfg_on_first_write(repo):
    """
    K1: если accounts.json нет, но в cfg["accounts"] что-то есть, первый
    add_account ДОЛЖЕН перенести существующие аккаунты в accounts.json
    (иначе они потеряются при следующем чтении).
    """
    from accounts import add_account, load_all_accounts

    cfg = {"accounts": [{"name": "legacy_acc"}]}
    add_account(repo, {"name": "new_acc"}, cfg=cfg)

    accs = load_all_accounts(repo)
    assert [a["name"] for a in accs] == ["legacy_acc", "new_acc"]


def test_update_account_patches_fields(repo):
    from accounts import load_all_accounts, update_account

    _write_accounts(repo, [{"name": "a", "phone": "old"}])
    updated = update_account(repo, "a", {"phone": "new", "user_id": "uid42"})
    assert updated is not None
    assert updated["phone"] == "new"
    assert updated["user_id"] == "uid42"

    on_disk = load_all_accounts(repo)
    assert on_disk[0]["phone"] == "new"
    assert on_disk[0]["user_id"] == "uid42"


def test_update_account_returns_none_for_unknown(repo):
    from accounts import update_account

    _write_accounts(repo, [{"name": "exists"}])
    assert update_account(repo, "nope", {"phone": "x"}) is None


def test_remove_account_deletes_and_persists(repo):
    from accounts import load_all_accounts, remove_account

    _write_accounts(repo, [{"name": "a"}, {"name": "b"}, {"name": "c"}])
    assert remove_account(repo, "b") is True

    accs = load_all_accounts(repo)
    assert [a["name"] for a in accs] == ["a", "c"]


def test_remove_account_returns_false_for_unknown(repo):
    from accounts import remove_account

    _write_accounts(repo, [{"name": "a"}])
    assert remove_account(repo, "nope") is False
    # содержимое не изменилось
    assert (repo / ACCOUNTS_JSON_FILENAME).read_text(encoding="utf-8") != ""


# ── L9: автоудаление cookies-файла при remove_account ────────────────────


def test_remove_account_deletes_cookies_file(repo):
    """L9: при удалении аккаунта связанный cookies.json тоже удаляется."""
    from accounts import remove_account

    cookies_rel = "accounts/acc_x/cookies.json"
    cookies_abs = repo / cookies_rel
    cookies_abs.parent.mkdir(parents=True)
    cookies_abs.write_text("[]", encoding="utf-8")

    _write_accounts(
        repo,
        [
            {"name": "acc_x", "cookies_path": cookies_rel},
            {"name": "acc_y"},  # без cookies_path — соседний аккаунт
        ],
    )

    assert remove_account(repo, "acc_x") is True
    assert not cookies_abs.exists(), "cookies-файл должен быть удалён вместе с аккаунтом"


def test_remove_account_no_cookies_path_does_not_crash(repo):
    """L9: аккаунт без cookies_path удаляется тихо, без обращения к ФС."""
    from accounts import remove_account

    _write_accounts(repo, [{"name": "no_cookies"}])
    # Не должно поднять никаких исключений.
    assert remove_account(repo, "no_cookies") is True


def test_remove_account_missing_cookies_file_does_not_crash(repo):
    """L9: cookies_path указан, но файла нет — remove_account не падает."""
    from accounts import remove_account

    _write_accounts(
        repo,
        [{"name": "ghost", "cookies_path": "accounts/ghost/cookies.json"}],
    )
    # Файла на диске нет — это нормальная ситуация (например, аккаунт
    # был добавлен, но куки не успели загрузить).
    assert remove_account(repo, "ghost") is True


def test_atomic_write_does_not_leave_temp_files(repo):
    """
    K1: при штатной записи временные .accounts_*.json.tmp не остаются.
    Это проверка правильного использования tempfile + os.replace.
    """
    from accounts import save_accounts

    save_accounts(repo, [{"name": "a"}])
    save_accounts(repo, [{"name": "a"}, {"name": "b"}])

    leftovers = list(repo.glob(".accounts_*.json.tmp"))
    assert leftovers == [], f"Остались temp-файлы: {leftovers}"


def test_concurrent_add_account_is_safe(repo, tmp_path):
    """
    K1: 10 параллельных потоков add_account не теряют изменения и не
    падают на UNIQUE-нарушении (все имена разные).
    """
    import threading

    from accounts import add_account, load_all_accounts

    save_path = repo / ACCOUNTS_JSON_FILENAME
    save_path.write_text("[]", encoding="utf-8")

    errors: list[Exception] = []

    def worker(i):
        try:
            add_account(repo, {"name": f"acc{i}"})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Ошибки в параллельных add_account: {errors}"
    final = load_all_accounts(repo)
    names = sorted(a["name"] for a in final)
    assert names == sorted(f"acc{i}" for i in range(10))
