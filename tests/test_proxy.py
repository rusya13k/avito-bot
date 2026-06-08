"""
A1: тесты per-account proxy (_apply_account_proxy).

Проверяем:
- account.get("proxy") используется как первый источник прокси.
- При неудаче per-account прокси — fallback на proxies.txt.
- Если ни один прокси не доступен — спрашиваем админа в TG.
- Успешный per-account proxy возвращается без обращения к proxies.txt.
- __no_proxy__ если админ разрешил без прокси.

T18: тесты для proxy health probe + auto-rotation в _apply_account_proxy
(см. секцию "Probe path" внизу). Legacy-тесты выше используют `cfg=None`
который отключает probe (back-compat).

После замены AdsPower → ChromeLauncher: _apply_account_proxy больше не
принимает adspower/user_id — только account dict, account_name и cfg.
Прокси возвращается как строка, ChromeLauncher.build_proxy_arg() конвертирует
её для --proxy-server CLI-флага.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from bot import _apply_account_proxy
from proxy_health import ProbeResult


def _make_account(proxy=None):
    return {"name": "acc1", "proxy": proxy}


# ── Per-account proxy (legacy path, cfg=None) ────────────────────────────


def test_per_account_proxy_used_first():
    """Если account["proxy"] задан — используется он (legacy path)."""
    acc = _make_account(proxy="user:pass@host:1080")
    with patch("bot.get_random_proxy") as mock_rnd:
        result = _apply_account_proxy(acc, "acc1")
    assert result == "user:pass@host:1080"
    mock_rnd.assert_not_called()


def test_no_per_account_proxy_uses_proxies_txt():
    """Нет per-account прокси → proxies.txt."""
    acc = _make_account(proxy=None)
    with patch("bot.get_random_proxy", return_value="txt:9090"):
        result = _apply_account_proxy(acc, "acc1")
    assert result == "txt:9090"


def test_no_proxy_at_all_cancel_by_admin_returns_none(caplog):
    """Нет per-account прокси и proxies.txt пуст — TG-опрос, cancel → None."""
    acc = _make_account(proxy=None)
    with (
        patch("bot.get_random_proxy", return_value=None),
        patch("bot._wait_user_resume_for_login", return_value="cancel"),
        caplog.at_level(logging.WARNING, logger="bot"),
    ):
        result = _apply_account_proxy(acc, "acc1")
    assert result is None
    assert any("Нет доступного прокси" in r.message for r in caplog.records)


def test_no_proxy_at_all_admin_allows_returns_sentinel(caplog):
    """Нет per-account прокси и proxies.txt пуст — TG-опрос, continue → __no_proxy__."""
    acc = _make_account(proxy=None)
    with (
        patch("bot.get_random_proxy", return_value=None),
        patch("bot._wait_user_resume_for_login", return_value="continue"),
        caplog.at_level(logging.WARNING, logger="bot"),
    ):
        result = _apply_account_proxy(acc, "acc1")
    assert result == "__no_proxy__"


# ── T18: probe path ──────────────────────────────────────────────────────


def _ok(ip="5.6.7.8", country=None):
    return ProbeResult(ok=True, ip=ip, country=country, latency_ms=42.0)


def _fail(error="timeout"):
    return ProbeResult(ok=False, error=error)


def test_probe_disabled_when_cfg_explicitly_off():
    """cfg.proxy_probe_enabled=False → probe не вызывается, legacy-путь."""
    acc = _make_account(proxy="acc:1080")
    cfg = {"proxy_probe_enabled": False}
    with (
        patch("proxy_health.pick_healthy_proxy") as mock_probe,
    ):
        result = _apply_account_proxy(acc, "acc1", cfg)
    assert result == "acc:1080"
    mock_probe.assert_not_called()


def test_probe_enabled_picks_healthy_per_account(tmp_path, monkeypatch):
    """Probe enabled (default) → per-account первым кандидатом."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("fb:1080\n", encoding="utf-8")
    acc = _make_account(proxy="acc:1080")
    cfg = {"proxy_probe_enabled": True}

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())) as mock_probe,
    ):
        result = _apply_account_proxy(acc, "acc1", cfg)

    assert result == "acc:1080"
    args, kwargs = mock_probe.call_args
    candidates = list(args[0])
    assert candidates[0] == "acc:1080"
    assert "fb:1080" in candidates


def test_probe_picks_healthy_from_pool_when_per_account_dead(tmp_path, monkeypatch):
    """per-account мёртвый → probe выбирает живой из pool."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("p1:1080\np2:1080\n", encoding="utf-8")
    acc = _make_account(proxy="dead:1080")
    cfg = {"proxy_probe_enabled": True}

    with (
        patch(
            "proxy_health.pick_healthy_proxy",
            return_value=("p2:1080", _ok(ip="9.9.9.9")),
        ),
    ):
        result = _apply_account_proxy(acc, "acc1", cfg)

    assert result == "p2:1080"


def test_probe_all_fail_asks_admin_tg(tmp_path, monkeypatch, caplog):
    """pick_healthy_proxy → (None, ProbeResult fail) — TG-опрос, cancel → None."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("p1:1080\np2:1080\n", encoding="utf-8")
    acc = _make_account(proxy="dead:1080")
    cfg = {"proxy_probe_enabled": True, "proxy_max_probe_attempts": 3}

    with (
        patch(
            "proxy_health.pick_healthy_proxy",
            return_value=(None, _fail("timeout")),
        ),
        patch("bot._wait_user_resume_for_login", return_value="cancel"),
        caplog.at_level(logging.ERROR, logger="bot"),
    ):
        result = _apply_account_proxy(acc, "acc1", cfg)

    assert result is None
    assert any("probe прокси провалились" in r.message for r in caplog.records)


def test_probe_no_candidates_asks_admin_tg(tmp_path, monkeypatch, caplog):
    """Нет ни per-account, ни proxies.txt — TG-опрос, cancel → None."""
    monkeypatch.chdir(tmp_path)  # без proxies.txt
    acc = _make_account(proxy=None)
    cfg = {"proxy_probe_enabled": True}

    with (
        patch("proxy_health.pick_healthy_proxy") as mock_probe,
        patch("bot._wait_user_resume_for_login", return_value="cancel"),
        caplog.at_level(logging.WARNING, logger="bot"),
    ):
        result = _apply_account_proxy(acc, "acc1", cfg)

    assert result is None
    mock_probe.assert_not_called()
    assert any("Нет доступного прокси" in r.message for r in caplog.records)


def test_probe_passes_cfg_options_through(tmp_path, monkeypatch):
    """timeout/url/country/max_attempts из cfg прокидываются в pick_healthy_proxy."""
    monkeypatch.chdir(tmp_path)
    acc = _make_account(proxy="acc:1080")
    cfg = {
        "proxy_probe_enabled": True,
        "proxy_probe_timeout_sec": 7.5,
        "proxy_probe_url": "https://my.probe/json",
        "proxy_expected_country": "RU",
        "proxy_max_probe_attempts": 2,
        "proxy_probe_scheme": "socks5",
    }

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())) as mock_probe,
    ):
        _apply_account_proxy(acc, "acc1", cfg)

    _, kwargs = mock_probe.call_args
    assert kwargs["timeout"] == 7.5
    assert kwargs["probe_url"] == "https://my.probe/json"
    assert kwargs["expected_country"] == "RU"
    assert kwargs["max_attempts"] == 2
    assert kwargs["scheme"] == "socks5"


def test_probe_dedups_per_account_in_pool(tmp_path, monkeypatch):
    """Если per-account proxy одновременно в proxies.txt — кандидат не дублируется."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("acc:1080\nfb:1080\n", encoding="utf-8")
    acc = _make_account(proxy="acc:1080")
    cfg = {"proxy_probe_enabled": True}

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())) as mock_probe,
    ):
        _apply_account_proxy(acc, "acc1", cfg)

    args, _ = mock_probe.call_args
    candidates = list(args[0])
    assert candidates.count("acc:1080") == 1
    assert "fb:1080" in candidates


def test_probe_default_enabled_when_cfg_omits_flag(tmp_path, monkeypatch):
    """cfg есть, но без proxy_probe_enabled → дефолт True (probe ON)."""
    monkeypatch.chdir(tmp_path)
    acc = _make_account(proxy="acc:1080")
    cfg = {}  # пусто, но не None — probe должен быть ON

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())) as mock_probe,
    ):
        _apply_account_proxy(acc, "acc1", cfg)

    mock_probe.assert_called_once()


def test_probe_legacy_when_cfg_is_none():
    """cfg=None (default) → legacy-путь без probe — для back-compat."""
    acc = _make_account(proxy="acc:1080")
    with (
        patch("proxy_health.pick_healthy_proxy") as mock_probe,
    ):
        result = _apply_account_proxy(acc, "acc1", None)

    assert result == "acc:1080"
    mock_probe.assert_not_called()


# ── T18: _pick_rotation_proxy (используется в _connect_with_retry) ────────


def test_rotation_proxy_legacy_when_cfg_none():
    """cfg=None → legacy путь, get_random_proxy."""
    from bot import _pick_rotation_proxy

    with patch("bot.get_random_proxy", return_value="rnd:1080") as mock_rnd:
        assert _pick_rotation_proxy(None) == "rnd:1080"
    mock_rnd.assert_called_once()


def test_rotation_proxy_legacy_when_probe_disabled():
    """cfg.proxy_probe_enabled=False → legacy путь."""
    from bot import _pick_rotation_proxy

    with patch("bot.get_random_proxy", return_value="rnd:1080") as mock_rnd:
        assert _pick_rotation_proxy({"proxy_probe_enabled": False}) == "rnd:1080"
    mock_rnd.assert_called_once()


def test_rotation_proxy_uses_pick_healthy_when_probe_on(tmp_path, monkeypatch):
    """probe_enabled=True → читает proxies.txt и пробует probe."""
    from bot import _pick_rotation_proxy

    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("a:1\nb:1\nc:1\n", encoding="utf-8")

    with patch(
        "proxy_health.pick_healthy_proxy",
        return_value=("b:1", ProbeResult(ok=True, ip="2.2.2.2")),
    ) as mock_pick:
        result = _pick_rotation_proxy({"proxy_probe_enabled": True})

    assert result == "b:1"
    args, kwargs = mock_pick.call_args
    candidates = list(args[0])
    assert set(candidates) == {"a:1", "b:1", "c:1"}


def test_rotation_proxy_excludes_already_tried(tmp_path, monkeypatch):
    """`exclude` отфильтровывает кандидатов перед probe."""
    from bot import _pick_rotation_proxy

    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("dead:1\nalive:1\n", encoding="utf-8")

    with patch(
        "proxy_health.pick_healthy_proxy",
        return_value=("alive:1", ProbeResult(ok=True, ip="1.1.1.1")),
    ) as mock_pick:
        _pick_rotation_proxy({"proxy_probe_enabled": True}, exclude={"dead:1"})

    args, _ = mock_pick.call_args
    candidates = list(args[0])
    assert candidates == ["alive:1"]
    assert "dead:1" not in candidates


def test_rotation_proxy_returns_none_when_pool_exhausted(tmp_path, monkeypatch):
    """exclude забирает весь pool → None без вызова probe."""
    from bot import _pick_rotation_proxy

    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("only:1\n", encoding="utf-8")

    with patch("proxy_health.pick_healthy_proxy") as mock_pick:
        result = _pick_rotation_proxy({"proxy_probe_enabled": True}, exclude={"only:1"})

    assert result is None
    mock_pick.assert_not_called()


def test_rotation_proxy_returns_none_when_all_probe_fail(tmp_path, monkeypatch):
    """probe всех кандидатов проваливается → None."""
    from bot import _pick_rotation_proxy

    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("a:1\nb:1\n", encoding="utf-8")

    with patch(
        "proxy_health.pick_healthy_proxy",
        return_value=(None, ProbeResult(ok=False, error="timeout")),
    ):
        result = _pick_rotation_proxy({"proxy_probe_enabled": True})

    assert result is None
