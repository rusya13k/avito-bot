"""
T18: тесты proxy_health (parse, build_url, probe, pick_healthy_proxy).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from proxy_health import (  # noqa: E402
    DEFAULT_PROBE_URL,
    ProbeResult,
    build_proxy_url,
    parse_proxy,
    pick_healthy_proxy,
    probe_proxy,
    redact_proxy,
)

# ── parse_proxy ──────────────────────────────────────────────────────────


def test_parse_proxy_host_port():
    assert parse_proxy("1.2.3.4:1080") == ("1.2.3.4", 1080, None, None)


def test_parse_proxy_host_port_user_pass():
    assert parse_proxy("1.2.3.4:1080:alice:secret") == ("1.2.3.4", 1080, "alice", "secret")


def test_parse_proxy_user_pass_at_host_port():
    assert parse_proxy("alice:secret@1.2.3.4:1080") == ("1.2.3.4", 1080, "alice", "secret")


def test_parse_proxy_with_scheme_strips_it():
    assert parse_proxy("socks5://alice:secret@h.example:1080") == (
        "h.example",
        1080,
        "alice",
        "secret",
    )


def test_parse_proxy_password_with_colon():
    """Пароль может содержать ':' — всё после третьего ':' идёт в password."""
    assert parse_proxy("h:1080:user:p:a:s:s") == ("h", 1080, "user", "p:a:s:s")


def test_parse_proxy_user_only_no_password():
    """user@host:port — без пароля."""
    assert parse_proxy("alice@h:1080") == ("h", 1080, "alice", None)


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "no_port_at_all", "h:notaport", "h:99999", "h:0", ":1080", "h:1:2"],
)
def test_parse_proxy_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse_proxy(bad)


def test_parse_proxy_non_string_raises():
    with pytest.raises(ValueError):
        parse_proxy(None)  # type: ignore[arg-type]


# ── build_proxy_url ──────────────────────────────────────────────────────


def test_build_proxy_url_no_auth():
    assert build_proxy_url("1.2.3.4:1080") == "socks5h://1.2.3.4:1080"


def test_build_proxy_url_with_auth():
    assert build_proxy_url("1.2.3.4:1080:alice:secret") == ("socks5h://alice:secret@1.2.3.4:1080")


def test_build_proxy_url_custom_scheme():
    assert build_proxy_url("h:1080", scheme="http") == "http://h:1080"


def test_build_proxy_url_url_encodes_special_chars():
    """'/', '*', ':' в пароле должны быть %-encoded.

    Note: '@' в пароле не поддерживается синтаксически (конфликтует с
    форматом ``user:pass@host:port``). На практике пароли AdsPower
    из таких символов не состоят.
    """
    out = build_proxy_url("h:1080:user:p/ass*hash:colon")
    # password: "p/ass*hash:colon" → /=%2F, *=%2A, :=%3A
    assert "p%2Fass%2Ahash%3Acolon" in out
    assert "@h:1080" in out


# ── redact_proxy ──────────────────────────────────────────────────────────


def test_redact_proxy_strips_credentials():
    assert redact_proxy("h.example:1080:alice:secret") == "h.example:1080"
    assert redact_proxy("alice:secret@h.example:1080") == "h.example:1080"


def test_redact_proxy_invalid_input():
    """Невалидная строка — не падает, возвращает безопасную форму."""
    out = redact_proxy("totally_invalid")
    assert "***" in out


# ── probe_proxy: helpers ──────────────────────────────────────────────────


def _mock_response(status_code: int = 200, json_data=None, text: str | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = "{}"  # truthy, чтобы _lookup_country не споткнулся
    else:
        resp.json.side_effect = ValueError("not json")
        resp.text = text or ""
    return resp


# ── probe_proxy: success path ─────────────────────────────────────────────


def test_probe_proxy_success_basic():
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"ip": "5.6.7.8"})

    result = probe_proxy("1.2.3.4:1080", session=sess)

    assert result.ok is True
    assert result.ip == "5.6.7.8"
    assert result.error is None
    assert result.country is None  # без expected_country geo не запрашивается
    sess.get.assert_called_once()
    # Проверяем что ушло через прокси
    _, kwargs = sess.get.call_args
    assert kwargs["proxies"]["http"].startswith("socks5h://")
    assert "1.2.3.4:1080" in kwargs["proxies"]["http"]


def test_probe_proxy_success_with_creds_in_url():
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"ip": "5.6.7.8"})

    result = probe_proxy("h:1080:alice:secret", session=sess)

    assert result.ok is True
    _, kwargs = sess.get.call_args
    assert kwargs["proxies"]["http"] == "socks5h://alice:secret@h:1080"


def test_probe_proxy_text_response_fallback():
    """Если probe-сервис ответил plain text (e.g. api.ipify.org без ?format=json)."""
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, json_data=None, text="9.8.7.6\n")

    result = probe_proxy("h:1080", session=sess)

    assert result.ok is True
    assert result.ip == "9.8.7.6"


def test_probe_proxy_httpbin_origin_field():
    """httpbin.org возвращает 'origin', не 'ip'."""
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"origin": "11.22.33.44"})

    result = probe_proxy("h:1080", session=sess)

    assert result.ok is True
    assert result.ip == "11.22.33.44"


def test_probe_proxy_uses_default_url_when_not_specified():
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"ip": "1.1.1.1"})

    probe_proxy("h:1080", session=sess)

    args, _ = sess.get.call_args
    assert args[0] == DEFAULT_PROBE_URL


# ── probe_proxy: failure paths ────────────────────────────────────────────


def test_probe_proxy_invalid_proxy_string():
    result = probe_proxy("not_a_proxy", session=MagicMock())
    assert result.ok is False
    assert result.error and result.error.startswith("parse_error")
    assert result.ip is None


def test_probe_proxy_timeout():
    sess = MagicMock()
    sess.get.side_effect = requests.Timeout("connect timeout")

    result = probe_proxy("h:1080", session=sess, timeout=2.0)

    assert result.ok is False
    assert result.error == "timeout"
    assert result.ip is None


def test_probe_proxy_connection_error():
    sess = MagicMock()
    sess.get.side_effect = requests.ConnectionError("refused")

    result = probe_proxy("h:1080", session=sess)

    assert result.ok is False
    assert result.error and result.error.startswith("connection_error")


def test_probe_proxy_generic_request_exception():
    """RequestException не из подсемейства ConnectionError/Timeout
    попадает в ветку ``request_error: <Type>``."""
    sess = MagicMock()
    # ChunkedEncodingError — прямой наследник RequestException, не
    # ConnectionError, поэтому ловится в общем except.
    sess.get.side_effect = requests.exceptions.ChunkedEncodingError("bad chunks")

    result = probe_proxy("h:1080", session=sess)

    assert result.ok is False
    assert result.error and result.error.startswith("request_error")
    assert "ChunkedEncodingError" in result.error


def test_probe_proxy_non_200_status():
    sess = MagicMock()
    sess.get.return_value = _mock_response(503, {"ip": "x"})

    result = probe_proxy("h:1080", session=sess)

    assert result.ok is False
    assert result.error == "status_503"


def test_probe_proxy_no_ip_in_response():
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"foo": "bar"})

    result = probe_proxy("h:1080", session=sess)

    assert result.ok is False
    assert result.error == "no_ip_in_response"


def test_probe_proxy_ip_banned():
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"ip": "5.6.7.8"})

    result = probe_proxy("h:1080", session=sess, banned_ips={"5.6.7.8", "9.9.9.9"})

    assert result.ok is False
    assert result.ip == "5.6.7.8"
    assert result.error == "ip_banned:5.6.7.8"


# ── probe_proxy: country check ───────────────────────────────────────────


def test_probe_proxy_country_match():
    """Probe IP=RU, expected RU → ok=True."""
    sess = MagicMock()
    # 1-й вызов — ipify, 2-й — geo.
    sess.get.side_effect = [
        _mock_response(200, {"ip": "5.6.7.8"}),
        _mock_response(200, {"countryCode": "RU", "country": "Russia"}),
    ]

    result = probe_proxy("h:1080", session=sess, expected_country="RU")

    assert result.ok is True
    assert result.ip == "5.6.7.8"
    assert result.country == "RU"


def test_probe_proxy_country_mismatch():
    sess = MagicMock()
    sess.get.side_effect = [
        _mock_response(200, {"ip": "5.6.7.8"}),
        _mock_response(200, {"countryCode": "DE"}),
    ]

    result = probe_proxy("h:1080", session=sess, expected_country="RU")

    assert result.ok is False
    assert result.ip == "5.6.7.8"
    assert result.country == "DE"
    assert result.error and result.error.startswith("country_mismatch")


def test_probe_proxy_country_case_insensitive():
    """countryCode 'ru' и expected_country 'RU' → match."""
    sess = MagicMock()
    sess.get.side_effect = [
        _mock_response(200, {"ip": "5.6.7.8"}),
        _mock_response(200, {"countryCode": "ru"}),
    ]

    result = probe_proxy("h:1080", session=sess, expected_country="ru")

    assert result.ok is True
    assert result.country == "RU"


def test_probe_proxy_country_softpass_when_geo_fails():
    """Geo сервис недоступен → country=None, probe считается успешным
    (мы не валим всё из-за external geo-сервиса)."""
    sess = MagicMock()
    sess.get.side_effect = [
        _mock_response(200, {"ip": "5.6.7.8"}),
        requests.Timeout("geo timeout"),
    ]

    result = probe_proxy("h:1080", session=sess, expected_country="RU")

    assert result.ok is True
    assert result.country is None


def test_probe_proxy_country_uses_country_code_field():
    """ipapi.co возвращает country_code (snake_case)."""
    sess = MagicMock()
    sess.get.side_effect = [
        _mock_response(200, {"ip": "1.1.1.1"}),
        _mock_response(200, {"country_code": "RU"}),
    ]
    result = probe_proxy("h:1080", session=sess, expected_country="RU")
    assert result.ok is True
    assert result.country == "RU"


# ── pick_healthy_proxy ────────────────────────────────────────────────────


def test_pick_healthy_proxy_first_works():
    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"ip": "1.1.1.1"})

    proxy, result = pick_healthy_proxy(
        ["good:1080", "another:1080"],
        session=sess,
    )

    assert proxy == "good:1080"
    assert result.ok is True
    # Только один вызов (вторым кандидатом не пользуемся).
    assert sess.get.call_count == 1


def test_pick_healthy_proxy_skips_dead_ones():
    sess = MagicMock()
    sess.get.side_effect = [
        requests.Timeout("dead"),
        _mock_response(200, {"ip": "2.2.2.2"}),
    ]

    proxy, result = pick_healthy_proxy(
        ["dead:1080", "live:1080"],
        session=sess,
    )

    assert proxy == "live:1080"
    assert result.ok is True
    assert result.ip == "2.2.2.2"


def test_pick_healthy_proxy_all_fail_returns_none():
    sess = MagicMock()
    sess.get.side_effect = [
        requests.Timeout("dead1"),
        requests.ConnectionError("dead2"),
    ]

    proxy, result = pick_healthy_proxy(
        ["d1:1080", "d2:1080"],
        session=sess,
    )

    assert proxy is None
    assert result is not None
    assert result.ok is False
    # last result — от последней попытки
    assert result.error and result.error.startswith("connection_error")


def test_pick_healthy_proxy_respects_max_attempts():
    sess = MagicMock()
    sess.get.side_effect = requests.Timeout("dead")

    proxy, result = pick_healthy_proxy(
        ["a:1", "b:1", "c:1", "d:1", "e:1"],
        session=sess,
        max_attempts=2,
    )

    assert proxy is None
    assert sess.get.call_count == 2  # только 2, не 5


def test_pick_healthy_proxy_empty_candidates():
    proxy, result = pick_healthy_proxy([], session=MagicMock())
    assert proxy is None
    assert result is None


def test_pick_healthy_proxy_logs_redacted(caplog):
    """В логе должен быть только host:port, без пароля."""
    import logging

    sess = MagicMock()
    sess.get.return_value = _mock_response(200, {"ip": "1.1.1.1"})

    with caplog.at_level(logging.INFO, logger="proxy_health"):
        pick_healthy_proxy(["h:1080:alice:supersecret"], session=sess)

    full_log = " ".join(r.message for r in caplog.records)
    assert "supersecret" not in full_log
    assert "alice" not in full_log
    assert "h:1080" in full_log


# ── ProbeResult dataclass ────────────────────────────────────────────────


def test_probe_result_defaults():
    r = ProbeResult(ok=True)
    assert r.ip is None
    assert r.country is None
    assert r.latency_ms == 0.0
    assert r.error is None
