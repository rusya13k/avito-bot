"""
A1: тесты per-account proxy (_apply_account_proxy).

Проверяем:
- account.get("proxy") используется как первый источник прокси.
- При неудаче per-account прокси — fallback на proxies.txt.
- Если ни один прокси не доступен — логируем ERROR и возвращаем None.
- Успешный per-account proxy возвращается без обращения к proxies.txt.

L11: тесты для AdsPowerAPI.update_proxy (метод-инкапсуляция URL-сборки).

T18: тесты для proxy health probe + auto-rotation в _apply_account_proxy
(см. секцию "Probe path" внизу). Legacy-тесты выше используют `cfg=None`
который отключает probe (back-compat).
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from bot import AdsPowerAPI, _apply_account_proxy
from proxy_health import ProbeResult


@pytest.fixture
def adspower():
    return MagicMock(name="adspower")


def _make_account(proxy=None):
    return {"name": "acc1", "user_id": "u1", "proxy": proxy}


# ── Per-account proxy ──────────────────────────────────────────────────────


def test_per_account_proxy_used_first(adspower):
    """Если account["proxy"] задан и update_profile_proxy успешен — используется он."""
    acc = _make_account(proxy="user:pass@host:1080")
    with (
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
        patch("bot.get_random_proxy") as mock_rnd,
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result == "user:pass@host:1080"
    mock_upd.assert_called_once_with(adspower, "u1", "user:pass@host:1080")
    mock_rnd.assert_not_called()


def test_per_account_proxy_fallback_on_failure(adspower):
    """Per-account proxy не ставится → fallback на proxies.txt."""
    acc = _make_account(proxy="bad:proxy")
    with (
        patch("bot.update_profile_proxy", side_effect=[False, True]) as mock_upd,
        patch("bot.get_random_proxy", return_value="fallback:1234"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result == "fallback:1234"
    assert mock_upd.call_count == 2


def test_no_proxy_at_all_returns_none_and_logs_error(adspower, caplog):
    """Нет per-account прокси и proxies.txt пуст — возвращаем None, ERROR в лог."""
    acc = _make_account(proxy=None)
    with (
        patch("bot.update_profile_proxy") as mock_upd,
        patch("bot.get_random_proxy", return_value=None),
        caplog.at_level(logging.ERROR, logger="bot"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result is None
    mock_upd.assert_not_called()
    assert any("Нет доступного прокси" in r.message for r in caplog.records)


def test_no_per_account_proxy_uses_proxies_txt(adspower):
    """Нет per-account прокси → proxies.txt без обращения к account["proxy"]."""
    acc = _make_account(proxy=None)
    with (
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
        patch("bot.get_random_proxy", return_value="txt:9090"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result == "txt:9090"
    mock_upd.assert_called_once_with(adspower, "u1", "txt:9090")


def test_both_proxies_fail_returns_none(adspower, caplog):
    """Per-account и proxies.txt оба не ставятся → None + ERROR."""
    acc = _make_account(proxy="p:1")
    with (
        patch("bot.update_profile_proxy", return_value=False),
        patch("bot.get_random_proxy", return_value="p:2"),
        caplog.at_level(logging.ERROR, logger="bot"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1")
    assert result is None
    assert any("Нет доступного прокси" in r.message for r in caplog.records)


# ── L11: AdsPowerAPI.update_proxy() ────────────────────────────────────────


def test_update_proxy_success_with_credentials():
    """L11: host:port:user:pass → POST с правильным URL/payload, code=0 → True."""
    api = AdsPowerAPI("http://127.0.0.1:50325/", api_key="secret")
    response = MagicMock()
    response.json.return_value = {"code": 0}

    with patch("bot.requests.post", return_value=response) as mock_post:
        ok = api.update_proxy("u1", "1.2.3.4:1080:alice:pwd")

    assert ok is True
    # base должен быть нормализован (без trailing slash) и URL — собран из _url
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args.args[0] == "http://127.0.0.1:50325/api/v1/user/update"
    payload = call_args.kwargs["json"]
    assert payload["user_id"] == "u1"
    cfg = payload["user_proxy_config"]
    assert cfg["proxy_host"] == "1.2.3.4"
    assert cfg["proxy_port"] == "1080"
    assert cfg["proxy_user"] == "alice"
    assert cfg["proxy_password"] == "pwd"
    # При наличии api_key — Authorization header.
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


def test_update_proxy_returns_false_on_non_zero_code():
    """L11: code != 0 в JSON-ответе → False."""
    api = AdsPowerAPI("http://127.0.0.1:50325")
    response = MagicMock()
    response.json.return_value = {"code": 42, "msg": "boom"}

    with patch("bot.requests.post", return_value=response):
        assert api.update_proxy("u1", "host:1080") is False


def test_update_proxy_returns_false_on_request_exception():
    """L11: сетевая ошибка → False, не пробрасываем наружу."""
    api = AdsPowerAPI("http://127.0.0.1:50325")
    with patch("bot.requests.post", side_effect=requests.ConnectionError("boom")):
        assert api.update_proxy("u1", "host:1080") is False


def test_update_proxy_invalid_format_skips_http():
    """L11: один токен (без порта) → False, без HTTP-вызова."""
    api = AdsPowerAPI("http://127.0.0.1:50325")
    with patch("bot.requests.post") as mock_post:
        assert api.update_proxy("u1", "no_port_here") is False
    mock_post.assert_not_called()


def test_update_profile_proxy_wrapper_delegates_to_method():
    """L11: top-level update_profile_proxy — тонкая обёртка над методом
    (back-compat для tests/test_proxy.py, которые мокают этот символ)."""
    from bot import update_profile_proxy

    api = MagicMock(spec=AdsPowerAPI)
    api.update_proxy.return_value = True

    assert update_profile_proxy(api, "u1", "host:1080") is True
    api.update_proxy.assert_called_once_with("u1", "host:1080")


# ── T18: probe path ──────────────────────────────────────────────────────


def _ok(ip="5.6.7.8", country=None):
    return ProbeResult(ok=True, ip=ip, country=country, latency_ms=42.0)


def _fail(error="timeout"):
    return ProbeResult(ok=False, error=error)


def test_probe_disabled_when_cfg_explicitly_off(adspower):
    """cfg.proxy_probe_enabled=False → probe не вызывается, legacy-путь."""
    acc = _make_account(proxy="acc:1080")
    cfg = {"proxy_probe_enabled": False}
    with (
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
        patch("proxy_health.pick_healthy_proxy") as mock_probe,
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)
    assert result == "acc:1080"
    mock_probe.assert_not_called()
    mock_upd.assert_called_once_with(adspower, "u1", "acc:1080")


def test_probe_enabled_picks_healthy_per_account(adspower, tmp_path, monkeypatch):
    """Probe enabled (default) → per-account первым кандидатом, applied при ok."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("fb:1080\n", encoding="utf-8")
    acc = _make_account(proxy="acc:1080")
    cfg = {"proxy_probe_enabled": True}

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())) as mock_probe,
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    assert result == "acc:1080"
    # Кандидаты: per-account первый, потом fb из proxies.txt.
    args, kwargs = mock_probe.call_args
    candidates = list(args[0])
    assert candidates[0] == "acc:1080"
    assert "fb:1080" in candidates
    mock_upd.assert_called_once_with(adspower, "u1", "acc:1080")


def test_probe_picks_healthy_from_pool_when_per_account_dead(adspower, tmp_path, monkeypatch):
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
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    assert result == "p2:1080"
    mock_upd.assert_called_once_with(adspower, "u1", "p2:1080")


def test_probe_all_fail_returns_none_with_error_log(adspower, tmp_path, monkeypatch, caplog):
    """pick_healthy_proxy → (None, ProbeResult fail) → ERROR + None.
    AdsPower update_proxy НЕ дёргается."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("p1:1080\np2:1080\n", encoding="utf-8")
    acc = _make_account(proxy="dead:1080")
    cfg = {"proxy_probe_enabled": True, "proxy_max_probe_attempts": 3}

    with (
        patch(
            "proxy_health.pick_healthy_proxy",
            return_value=(None, _fail("timeout")),
        ),
        patch("bot.update_profile_proxy") as mock_upd,
        caplog.at_level(logging.ERROR, logger="bot"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    assert result is None
    mock_upd.assert_not_called()
    assert any("probe прокси провалились" in r.message for r in caplog.records)


def test_probe_no_candidates_at_all_returns_none(adspower, tmp_path, monkeypatch, caplog):
    """Нет ни per-account, ни proxies.txt → ERROR (как в legacy)."""
    monkeypatch.chdir(tmp_path)  # без proxies.txt
    acc = _make_account(proxy=None)
    cfg = {"proxy_probe_enabled": True}

    with (
        patch("proxy_health.pick_healthy_proxy") as mock_probe,
        patch("bot.update_profile_proxy") as mock_upd,
        caplog.at_level(logging.ERROR, logger="bot"),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    assert result is None
    mock_probe.assert_not_called()
    mock_upd.assert_not_called()
    assert any("Нет доступного прокси" in r.message for r in caplog.records)


def test_probe_passes_cfg_options_through(adspower, tmp_path, monkeypatch):
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
        patch("bot.update_profile_proxy", return_value=True),
    ):
        _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    _, kwargs = mock_probe.call_args
    assert kwargs["timeout"] == 7.5
    assert kwargs["probe_url"] == "https://my.probe/json"
    assert kwargs["expected_country"] == "RU"
    assert kwargs["max_attempts"] == 2
    assert kwargs["scheme"] == "socks5"


def test_probe_dedups_per_account_in_pool(adspower, tmp_path, monkeypatch):
    """Если per-account proxy одновременно в proxies.txt — кандидат не дублируется."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxies.txt").write_text("acc:1080\nfb:1080\n", encoding="utf-8")
    acc = _make_account(proxy="acc:1080")
    cfg = {"proxy_probe_enabled": True}

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())) as mock_probe,
        patch("bot.update_profile_proxy", return_value=True),
    ):
        _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    args, _ = mock_probe.call_args
    candidates = list(args[0])
    assert candidates.count("acc:1080") == 1
    assert "fb:1080" in candidates


def test_probe_passed_but_adspower_update_fails(adspower, tmp_path, monkeypatch):
    """Probe прошёл, но AdsPower API отказал → return None (без второго probe)."""
    monkeypatch.chdir(tmp_path)
    acc = _make_account(proxy="acc:1080")
    cfg = {"proxy_probe_enabled": True}

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())),
        patch("bot.update_profile_proxy", return_value=False),
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    assert result is None


def test_probe_default_enabled_when_cfg_omits_flag(adspower, tmp_path, monkeypatch):
    """cfg есть, но без proxy_probe_enabled → дефолт True (probe ON)."""
    monkeypatch.chdir(tmp_path)
    acc = _make_account(proxy="acc:1080")
    cfg = {}  # пусто, но не None — probe должен быть ON

    with (
        patch("proxy_health.pick_healthy_proxy", return_value=("acc:1080", _ok())) as mock_probe,
        patch("bot.update_profile_proxy", return_value=True),
    ):
        _apply_account_proxy(adspower, "u1", acc, "acc1", cfg)

    mock_probe.assert_called_once()


def test_probe_legacy_when_cfg_is_none(adspower):
    """cfg=None (default) → legacy-путь без probe — для back-compat существующих
    тестов / вызовов которые не передают cfg."""
    acc = _make_account(proxy="acc:1080")
    with (
        patch("proxy_health.pick_healthy_proxy") as mock_probe,
        patch("bot.update_profile_proxy", return_value=True) as mock_upd,
    ):
        result = _apply_account_proxy(adspower, "u1", acc, "acc1", None)

    # Эта проверка важна: если кто-то меняет default, тест поймает.
    # При cfg=None исходные тесты выше остаются актуальными.
    assert result == "acc:1080"
    mock_probe.assert_not_called()
    mock_upd.assert_called_once_with(adspower, "u1", "acc:1080")


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
