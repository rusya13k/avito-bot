"""
T18: Proxy health probe + auto-rotation.

Перед запуском AdsPower-профиля делаем HTTP-probe через прокси к
публичному IP-сервису (`api.ipify.org` по умолчанию). Цели:

* Убедиться что прокси вообще жив (не timeout / connection refused).
* Получить реальный публичный IP — для опционального банлиста и
  проверки страны через geo-сервис.
* (опционально) Сравнить ISO-код страны с `proxy_expected_country` —
  отлавливаем случаи когда «российский» прокси внезапно отвечает с DE/NL.

Если probe фейлится — вызывающий код пробует следующий прокси из списка
кандидатов (rotation). Это спасает от длинных timeout'ов внутри AdsPower
и от запуска профиля через мёртвый/забаненный/чужой прокси.

Public API:
    parse_proxy(proxy_str)      → (host, port, user, password)
    build_proxy_url(...)        → "scheme://[user:pass@]host:port"
    probe_proxy(...)            → ProbeResult
    pick_healthy_proxy(...)     → (proxy_str | None, ProbeResult | None)

Формат `proxy_str` поддерживается такой же, как у `AdsPowerAPI.update_proxy`:
* ``host:port``
* ``host:port:user:pass``
* ``user:pass@host:port`` (тоже распознаётся)
* ``scheme://...`` (схема игнорируется — мы сами назначаем socks5h/http)

По умолчанию используется схема ``socks5h://`` (DNS резолвится на стороне
прокси — реалистичнее, не палит DNS клиента). Это совпадает с тем, что
AdsPower выставляет в `proxy_type=socks5`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)

# ── Defaults ───────────────────────────────────────────────────────────────
# api.ipify.org — простой сервис: GET → JSON {"ip": "1.2.3.4"}.
# Альтернативы: https://api.myip.com, https://ifconfig.co/json.
DEFAULT_PROBE_URL = "https://api.ipify.org?format=json"

# ip-api.com — бесплатный geo-lookup без ключа (45 req/min с одного IP).
# Возвращает поле `countryCode` (ISO 3166-1 alpha-2, e.g. "RU").
DEFAULT_GEO_URL_TEMPLATE = "http://ip-api.com/json/{ip}?fields=status,country,countryCode"

DEFAULT_TIMEOUT = 10.0
DEFAULT_PROXY_SCHEME = "socks5h"  # DNS резолвится на стороне прокси
DEFAULT_MAX_PROBE_ATTEMPTS = 5


@dataclass
class ProbeResult:
    """Результат одного probe-вызова.

    `ok=True` — прокси живой, успешно ответил, страна (если проверялась)
    совпадает. `error=None` в этом случае.

    `ok=False` — fail; `error` содержит короткий код причины
    (`timeout`, `connection_error: ...`, `status_502`, `parse_error: ...`,
    `no_ip_in_response`, `ip_banned:<ip>`, `country_mismatch:XX!=YY`).
    """

    ok: bool
    ip: str | None = None
    country: str | None = None
    latency_ms: float = 0.0
    error: str | None = None


# ── Parsing / URL building ────────────────────────────────────────────────


def parse_proxy(proxy_str: str) -> tuple[str, int, str | None, str | None]:
    """Парсит строку прокси в (host, port, user, password).

    Поддерживаемые форматы:
        ``host:port``
        ``host:port:user:pass``
        ``user:pass@host:port``
        ``scheme://[user:pass@]host:port`` (scheme отбрасывается)

    Бросает ValueError если формат не распознан или порт не int.
    """
    if not isinstance(proxy_str, str):
        raise ValueError(f"proxy must be str, got {type(proxy_str).__name__}")
    s = proxy_str.strip()
    if not s:
        raise ValueError("empty proxy string")

    # Strip scheme if present (ignored — we build our own).
    if "://" in s:
        s = s.split("://", 1)[1]

    user: str | None = None
    password: str | None = None

    if "@" in s:
        # user[:pass]@host:port
        creds, hostport = s.rsplit("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        else:
            user = creds
        if ":" not in hostport:
            raise ValueError(f"missing port: {proxy_str!r}")
        host, port_str = hostport.rsplit(":", 1)
    else:
        # host:port[:user:pass]
        parts = s.split(":")
        if len(parts) == 2:
            host, port_str = parts
        elif len(parts) >= 4:
            host = parts[0]
            port_str = parts[1]
            user = parts[2]
            # password может содержать ':'
            password = ":".join(parts[3:])
        else:
            raise ValueError(f"unrecognized proxy format: {proxy_str!r}")

    try:
        port = int(port_str)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid port {port_str!r}") from e

    if not host:
        raise ValueError(f"empty host in {proxy_str!r}")
    if port <= 0 or port > 65535:
        raise ValueError(f"port out of range: {port}")

    return host, port, user, password


def build_proxy_url(proxy_str: str, scheme: str = DEFAULT_PROXY_SCHEME) -> str:
    """Собирает URL для requests proxies dict.

    Пример: ``socks5h://alice:p%40ss@1.2.3.4:1080``.
    Логин/пароль url-encoded (на случай '@', ':', '/').
    """
    host, port, user, password = parse_proxy(proxy_str)
    if user and password is not None:
        return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if user:
        return f"{scheme}://{quote(user, safe='')}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def redact_proxy(proxy_str: str) -> str:
    """Маскирует логин/пароль для логов: ``host:port`` (без креденшелов)."""
    try:
        host, port, _user, _pass = parse_proxy(proxy_str)
        return f"{host}:{port}"
    except ValueError:
        # Невалидная строка — отдадим только первый сегмент.
        return proxy_str.split(":", 1)[0] + ":***"


# ── Probe ────────────────────────────────────────────────────────────────


def probe_proxy(
    proxy_str: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    probe_url: str = DEFAULT_PROBE_URL,
    expected_country: str | None = None,
    geo_url_template: str | None = DEFAULT_GEO_URL_TEMPLATE,
    banned_ips: Iterable[str] | None = None,
    scheme: str = DEFAULT_PROXY_SCHEME,
    session: requests.Session | None = None,
) -> ProbeResult:
    """Делает HTTP-probe через прокси.

    1. Парсит прокси, собирает proxy URL.
    2. GET ``probe_url`` через прокси с timeout.
    3. Non-200 / timeout / connection error → ok=False.
    4. Парсит JSON, забирает 'ip'. Если IP не нашли — ok=False.
    5. Если ip в `banned_ips` → ok=False.
    6. Если задан `expected_country`: дополнительный GET geo-сервиса.
       Несовпадение ISO-кода → ok=False. Если geo не отдал страну
       (timeout/non-200) — НЕ валим probe, считаем soft-pass.

    Returns:
        ProbeResult.
    """
    try:
        proxy_url = build_proxy_url(proxy_str, scheme)
    except ValueError as e:
        return ProbeResult(ok=False, error=f"parse_error: {e}")

    proxies = {"http": proxy_url, "https": proxy_url}
    sess = session or requests
    banned_set: set[str] = set(banned_ips) if banned_ips else set()

    started = time.monotonic()
    try:
        resp = sess.get(probe_url, proxies=proxies, timeout=timeout)
    except requests.Timeout:
        return ProbeResult(
            ok=False,
            error="timeout",
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except requests.ConnectionError as e:
        return ProbeResult(
            ok=False,
            error=f"connection_error: {str(e)[:100]}",
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except requests.RequestException as e:
        return ProbeResult(
            ok=False,
            error=f"request_error: {type(e).__name__}",
            latency_ms=(time.monotonic() - started) * 1000.0,
        )

    latency_ms = (time.monotonic() - started) * 1000.0

    status = getattr(resp, "status_code", None)
    if status != 200:
        return ProbeResult(ok=False, error=f"status_{status}", latency_ms=latency_ms)

    # Parse IP from response.
    ip = _extract_ip(resp)
    if not ip:
        return ProbeResult(ok=False, error="no_ip_in_response", latency_ms=latency_ms)

    if ip in banned_set:
        return ProbeResult(ok=False, ip=ip, error=f"ip_banned:{ip}", latency_ms=latency_ms)

    # Optional country check.
    country: str | None = None
    if expected_country and geo_url_template:
        country = _lookup_country(sess, geo_url_template, ip, timeout)
        # Если geo не ответил — country=None, soft-pass.
        if country and country.upper() != expected_country.upper():
            return ProbeResult(
                ok=False,
                ip=ip,
                country=country,
                error=f"country_mismatch:{country}!={expected_country.upper()}",
                latency_ms=latency_ms,
            )

    return ProbeResult(ok=True, ip=ip, country=country, latency_ms=latency_ms)


def _extract_ip(resp) -> str | None:
    """Достать IP из ответа probe-сервиса (JSON или plain text)."""
    # JSON: {"ip": "1.2.3.4"}
    try:
        data = resp.json()
        if isinstance(data, dict):
            value = data.get("ip") or data.get("origin")  # httpbin → "origin"
            if isinstance(value, str) and value.strip():
                return value.strip()
    except (ValueError, AttributeError):
        pass
    # Plain text: "1.2.3.4\n"
    text = (getattr(resp, "text", "") or "").strip()
    if text and len(text) <= 64 and all(c.isalnum() or c in ".:" for c in text):
        return text
    return None


def _lookup_country(sess, geo_url_template: str, ip: str, timeout: float) -> str | None:
    """Дополнительный GET к geo-сервису для определения страны.

    Возвращает ISO-код (e.g. "RU") или None если geo не ответил.
    Не использует прокси — geo сам по IP определяет страну.
    """
    try:
        geo_url = geo_url_template.format(ip=ip)
        geo_resp = sess.get(geo_url, timeout=timeout)
        if getattr(geo_resp, "status_code", None) != 200:
            _logger.debug("Geo probe non-200: %s", getattr(geo_resp, "status_code", None))
            return None
        data = geo_resp.json() if getattr(geo_resp, "text", None) else {}
        if not isinstance(data, dict):
            return None
        # ip-api.com: countryCode; ipapi.co: country_code; некоторые: country.
        code = data.get("countryCode") or data.get("country_code") or data.get("country")
        if isinstance(code, str) and code.strip():
            return code.strip().upper()
    except (requests.RequestException, ValueError) as e:
        _logger.debug("Geo probe failed: %s", e)
    return None


# ── Rotation helper ───────────────────────────────────────────────────────


def pick_healthy_proxy(
    candidates: Iterable[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    probe_url: str = DEFAULT_PROBE_URL,
    expected_country: str | None = None,
    banned_ips: Iterable[str] | None = None,
    max_attempts: int = DEFAULT_MAX_PROBE_ATTEMPTS,
    scheme: str = DEFAULT_PROXY_SCHEME,
    log_prefix: str = "",
    session: requests.Session | None = None,
) -> tuple[str | None, ProbeResult | None]:
    """Из списка `candidates` находит первый прокси, прошедший probe.

    Перебираем кандидатов в порядке (caller обычно уже зашаффлил
    pool, чтобы аккаунты не толпились на одном IP). Лимит
    `max_attempts` общий — даже если кандидатов много, не делаем
    больше N HTTP-запросов.

    Returns:
        (proxy_str, ProbeResult) если нашли живой,
        (None, last_result) если ни один не прошёл (last_result —
        ProbeResult последней попытки, None если candidates пустой).
    """
    last: ProbeResult | None = None
    tried = 0
    for proxy in candidates:
        if tried >= max_attempts:
            break
        tried += 1
        result = probe_proxy(
            proxy,
            timeout=timeout,
            probe_url=probe_url,
            expected_country=expected_country,
            banned_ips=banned_ips,
            scheme=scheme,
            session=session,
        )
        last = result
        if result.ok:
            _logger.info(
                "%sproxy probe OK: %s ip=%s country=%s latency=%.0fms",
                log_prefix,
                redact_proxy(proxy),
                result.ip,
                result.country or "?",
                result.latency_ms,
            )
            return proxy, result
        _logger.info(
            "%sproxy probe FAIL: %s reason=%s latency=%.0fms",
            log_prefix,
            redact_proxy(proxy),
            result.error,
            result.latency_ms,
        )
    return None, last
