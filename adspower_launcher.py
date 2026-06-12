"""
AdsPower Launcher — запуск профиля AdsPower через Local API (headful via Xvfb).

AdsPower управляет прокси, fingerprint'ом, WebRTC, canvas на уровне C++.
Запуск браузера в headful-режиме через Xvfb (DISPLAY=:99) — единственный
рабочий способ, т.к. в --headless AdsPower не может инициализировать
своё расширение-проксификатор, что ведёт к крашу Chrome с
net::ERR_SOCKS_CONNECTION_FAILED.

Gemini (2026-06-11) подтвердил:
- Chromium не поддерживает передачу учётных данных SOCKS5 через флаги CLI
- --headless ломает прокси-расширение AdsPower
- --single-process в headless + SOCKS5 → зависание сетевого стека → crash
- Xvfb headful решает все три проблемы
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any

import requests

logger = logging.getLogger("bot.adspower")

ADS_BASE_URL = os.environ.get("ADSPOWER_API_URL", "http://127.0.0.1:50325")
ADSPOWER_CLI = "adspower"


class AdsPowerLauncher:
    """Управление профилями AdsPower через Local API.

    Browser запускается в headful-режиме (headless=0) — рендеринг
    через Xvfb на DISPLAY=:99. Без этого прокси-расширение не работает.

    API-only: CLI для запуска стабильно не работает при вызове из
    subprocess без shell (JSON-аргументы ломают парсинг). CLI используется
    только для stop_browser и kill_orphaned_browsers.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ADSPOWER_API_KEY", "")
        self.daily_limit_exceeded: bool = False

    def _ensure_runtime(self) -> bool:
        """Проверить что AdsPower runtime запущен, иначе запустить."""
        for _ in range(3):
            try:
                resp = requests.get(f"{ADS_BASE_URL}", timeout=3)
                if resp.status_code in (200, 404):
                    return True
            except Exception:
                pass
            time.sleep(1)

        logger.info("AdsPower runtime не отвечает — запускаю...")
        for attempt in range(3):
            try:
                subprocess.run(
                    ["adspower", "start", "-k", self.api_key],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                for _ in range(10):
                    time.sleep(1.5)
                    try:
                        resp = requests.get(f"{ADS_BASE_URL}", timeout=2)
                        if resp.status_code in (200, 404):
                            logger.info("AdsPower runtime запущен")
                            return True
                    except Exception:
                        continue
            except Exception as exc:
                logger.warning("Попытка %d запуска runtime: %s", attempt + 1, exc)
                time.sleep(2)

        logger.error("Не удалось запустить AdsPower runtime после 3 попыток")
        return False

    def start_browser(self, profile_id: str) -> dict[str, Any] | None:
        """Запустить профиль AdsPower через REST API, headful (headless=0).

        Использует headless=0 — браузер рендерится через Xvfb (:99).
        Это критично для работы прокси-расширения AdsPower.
        """
        self._ensure_runtime()

        try:
            logger.info(
                "Запуск профиля AdsPower %s в headful-режиме (Xvfb :99)...",
                profile_id,
            )
            resp = requests.get(
                f"{ADS_BASE_URL}/api/v1/browser/start",
                params={"user_id": profile_id, "headless": "0"},
                timeout=30,
            )
            data = resp.json()

            if data.get("code") == 0:
                return self._parse_response(data)

            msg = data.get("msg", "unknown error")
            if "daily limit" in msg.lower():
                self.daily_limit_exceeded = True
                logger.error(
                    "AdsPower дневной лимит исчерпан для %s: %s (восстановление ~21ч).",
                    profile_id,
                    msg,
                )
            else:
                logger.error("AdsPower API ошибка: %s — %s", msg, data)

        except requests.Timeout:
            logger.error("AdsPower API timeout 30s для профиля %s", profile_id)
        except Exception as exc:
            logger.error("AdsPower API start failed: %s", exc)

        return None

    def stop_browser(self, profile_id: str) -> bool:
        """Остановить профиль AdsPower."""
        try:
            resp = requests.get(
                f"{ADS_BASE_URL}/api/v1/browser/stop",
                params={"user_id": profile_id},
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("code") == 0:
                return True
        except Exception:
            pass

        # fallback: прямой kill через CLI
        try:
            subprocess.run(
                [ADSPOWER_CLI, "close-browser", json.dumps({"profile_id": profile_id})],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass

        return False

    def kill_orphaned_browsers(self) -> None:
        """Остановить все запущенные профили AdsPower."""
        try:
            subprocess.run(
                [ADSPOWER_CLI, "close-all-profiles"],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass

    def _parse_response(self, data: dict) -> dict[str, Any] | None:
        """Парсинг ответа REST API."""
        try:
            d = data["data"]
            selenium_addr = d["ws"]["selenium"]
            host, port_str = selenium_addr.split(":")
            webdriver = d.get("webdriver", "")
            return {
                "debug_port": int(port_str),
                "webdriver": webdriver,
                "selenium_address": selenium_addr,
            }
        except (KeyError, ValueError, TypeError) as exc:
            logger.error("AdsPower API: неожиданный ответ: %s — %s", data, exc)
            return None
