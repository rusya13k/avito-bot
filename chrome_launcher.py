"""
ChromeLauncher — замена AdsPower для запуска Chrome.

Запускает Chrome напрямую через subprocess.Popen с CLI-флагами:
  --proxy-server, --user-data-dir, --remote-debugging-port, --user-agent, и т.д.
Selenium-код (avito_client, bot) НЕ трогаем — только подключаемся через
debuggerAddress к уже запущенному Chrome.

Public API:
    ChromeLauncher.start(account_name, user_data_dir, proxy, user_agent) → debug_port
    ChromeLauncher.stop(account_name) → None
    ChromeLauncher.build_proxy_arg(proxy_str) → str | None
    ChromeLauncher.is_running(account_name) → bool
    ChromeLauncher.cleanup_lock_files(user_data_dir) → None
"""

from __future__ import annotations

import logging
import os
import platform
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from proxy_health import parse_proxy

_logger = logging.getLogger(__name__)

# Диапазон debug-портов — по одному на аккаунт, не пересекаются.
# Таймаут ожидания готовности Chrome.

# Дефолтный User-Agent (Chrome 149 на Windows 10 x64).
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.7827.53 Safari/537.36"
)

# Таймаут ожидания готовности Chrome (poll /json/version).
_CHROME_READY_TIMEOUT = 30.0

# Таймаут graceful shutdown (SIGTERM → wait → SIGKILL).
_GRACEFUL_SHUTDOWN_TIMEOUT = 10.0

# Максимум попыток запуска Chrome с разными портами.
_MAX_START_RETRIES = 3


def _wait_chrome_ready(port: int, timeout: float = _CHROME_READY_TIMEOUT) -> None:
    """Ждёт пока Chrome начнёт отвечать на debug-порту.

    Raises:
        RuntimeError: если Chrome не ответил за timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"http://127.0.0.1:{port}/json/version",
                timeout=5,
                proxies={"http": None, "https": None},
            )
            if resp.ok:
                return
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1.0)
    raise RuntimeError(f"Chrome не ответил на debug-порту {port} за {timeout}s")


def _start_chrome(
    cmd_template: list[str], max_retries: int = _MAX_START_RETRIES
) -> tuple[subprocess.Popen, int]:
    """Запускает Chrome с retry'ми на разных портах.

    Args:
        cmd_template: список аргументов Chrome БЕЗ --remote-debugging-port.
        max_retries: сколько портов перепробовать.

    Returns:
        (process, debug_port)

    Raises:
        RuntimeError: если все попытки провалились.
    """
    for attempt in range(max_retries):
        # Выбираем порт через bind
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        # --remote-debugging-port вставляем ДО about:blank (Chrome игнорирует
        # флаги после URL на Windows). cmd_template[0]=chrome_bin, [-1]=URL.
        cmd = cmd_template[:-1] + [f"--remote-debugging-port={port}", cmd_template[-1]]

        # Stderr пишем во временный файл — при падении сможем прочитать причину.
        # На Windows stdout=nul в open глючит с UnicodeDecodeError (см. код),
        # поэтому stdout тоже в файл.
        stderr_tmp = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_tmp,
        )

        try:
            _wait_chrome_ready(port)
            stderr_tmp.close()
            return proc, port
        except RuntimeError:
            # Chrome не стартанул — читаем stderr чтобы понять почему
            stderr_tmp.seek(0)
            stderr_data = stderr_tmp.read().decode("utf-8", errors="replace")[:500]
            stderr_tmp.close()
            _logger.warning(
                "Chrome start attempt %d failed on port %d. Stderr: %s",
                attempt + 1, port, stderr_data or "(empty)",
            )
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            time.sleep(1.0)  # пауза между попытками
            continue

    raise RuntimeError(
        f"Chrome не стартанул за {max_retries} попытки (каждая по {_CHROME_READY_TIMEOUT}s)"
    )


def _find_chrome_binary() -> str:
    """Возвращает путь к Chrome/Chromium бинарнику.

    Ищет в порядке:
      1. ENV CHROME_PATH (явный override).
      2. Стандартные пути по ОС.
    Бросает FileNotFoundError если не найден.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    system = platform.system()
    candidates: list[str] = []

    if system == "Windows":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            f"{pf}\\Google\\Chrome\\Application\\chrome.exe",
            f"{pf86}\\Google\\Chrome\\Application\\chrome.exe",
            f"{local}\\Google\\Chrome\\Application\\chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]

    for c in candidates:
        if Path(c).is_file():
            return c

    raise FileNotFoundError(
        f"Chrome/Chromium не найден. Установи или задай CHROME_PATH env. Искал в: {candidates}"
    )


class ChromeLauncher:
    """Управляет жизненным циклом Chrome-процессов: start / stop / cleanup.

    Каждый аккаунт получает свой Chrome-процесс с уникальным debug-портом
    и user-data-dir. Процессы отслеживаются по account_name → process info.
    """

    def __init__(self, repo_dir: str | Path | None = None):
        """Args:
        repo_dir: корень репозитория (для поиска chrome_profile_dir
                  относительно accounts/<name>/chrome_profile).
                  Если None — используются абсолютные пути.
        """
        self.repo_dir = Path(repo_dir) if repo_dir else None
        # account_name → {"process": Popen, "debug_port": int}
        self._processes: dict[str, dict[str, Any]] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def start(
        self,
        account_name: str,
        user_data_dir: str | Path | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
        *,
        window_size: str = "1920,1080",
        extra_flags: list[str] | None = None,
    ) -> int:
        """Запускает Chrome для аккаунта. Возвращает debug-порт.

        Args:
            account_name: уникальное имя аккаунта (для трекинга процесса).
            user_data_dir: путь к директории профиля Chrome
                           (accounts/<name>/chrome_profile по умолчанию).
            proxy: адрес для --proxy-server (socks5://127.0.0.1:10800).
                   None = прямой коннект (без флага).
            user_agent: User-Agent строка. None = дефолтный.
            window_size: WxH для --window-size.
            extra_flags: дополнительные CLI-флаги Chrome.

        Raises:
            RuntimeError: если Chrome не стартовал или не ответил за таймаут.
            FileNotFoundError: если chrome бинарник не найден.
        """
        # Убить зависший процесс если он есть от предыдущего запуска
        if self.is_running(account_name):
            _logger.warning("[%s] Chrome уже запущен — убиваю перед рестартом", account_name)
            self.stop(account_name)

        # Определяем user-data-dir и резолвим в абсолют (Chrome на Windows
        # резолвит относительные пути относительно своего install directory)
        if user_data_dir is None:
            udd = self._default_user_data_dir(account_name).resolve()
        else:
            udd = Path(user_data_dir).resolve()

        # Создаём директорию если нет
        udd.mkdir(parents=True, exist_ok=True)

        # Очищаем lock-файлы от предыдущей сессии
        self.cleanup_lock_files(udd)

        # Собираем CLI-команду (без --remote-debugging-port — добавляется в _start_chrome)
        chrome_bin = _find_chrome_binary()
        cmd_template = self._build_cmd(
            chrome_bin=chrome_bin,
            user_data_dir=udd,
            proxy=proxy,
            user_agent=user_agent or _DEFAULT_USER_AGENT,
            window_size=window_size,
            extra_flags=extra_flags,
        )

        _logger.info(
            "[%s] Starting Chrome: udd=%s, proxy=%s",
            account_name,
            udd,
            proxy or "<direct>",
        )

        # Запускаем Chrome (с retry на разных портах)
        proc, debug_port = _start_chrome(cmd_template)

        _logger.info("[%s] Chrome готов (port=%d)", account_name, debug_port)

        # Сохраняем в трекер
        self._processes[account_name] = {
            "process": proc,
            "debug_port": debug_port,
            "user_data_dir": udd,
        }

        return debug_port

    def stop(self, account_name: str) -> None:
        """Останавливает Chrome-процесс для аккаунта.

        Порядок: SIGTERM → wait → SIGKILL (или TerminateProcess на Windows).
        """
        info = self._processes.pop(account_name, None)
        if info is None:
            _logger.debug("[%s] Нет зарегистрированного Chrome-процесса", account_name)
            return

        proc: subprocess.Popen = info["process"]
        debug_port: int = info["debug_port"]

        _logger.info(
            "[%s] Останавливаю Chrome (port=%d, pid=%d)", account_name, debug_port, proc.pid
        )
        self._kill_proc(proc)

        # Проверяем что debug-порт свободен
        for _ in range(5):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(("127.0.0.1", debug_port))
                    # Порт ещё занят — ждём
                    time.sleep(1)
            except OSError:
                break

        _logger.info("[%s] Chrome остановлен", account_name)

    def is_running(self, account_name: str) -> bool:
        """True если Chrome-процесс для аккаунта жив."""
        info = self._processes.get(account_name)
        if info is None:
            return False
        proc: subprocess.Popen = info["process"]
        return proc.poll() is None

    def get_debug_port(self, account_name: str) -> int | None:
        """Возвращает debug-порт для аккаунта или None."""
        info = self._processes.get(account_name)
        return info["debug_port"] if info else None

    def stop_all(self) -> None:
        """Останавливает все зарегистрированные Chrome-процессы."""
        for name in list(self._processes.keys()):
            self.stop(name)

    # ── Static helpers ────────────────────────────────────────────────────

    @staticmethod
    def build_proxy_arg(proxy_str: str | None) -> str | None:
        """Превращает proxy-строку из accounts.json в значение для --proxy-server.

        Поддерживаемые форматы входа:
          - "socks5://127.0.0.1:10800"  → "socks5://127.0.0.1:10800"
          - "127.0.0.1:10800"            → "socks5://127.0.0.1:10800"
          - "host:port:user:pass"        → "socks5://host:port"
            (credentials не поддерживаются Chrome CLI — нужен форвардер)
          - "user:pass@host:port"        → "socks5://host:port"
          - None                         → None (без прокси)

        Chrome --proxy-server НЕ поддерживает credentials.
        Если proxy содержит user:pass — они отбрасываются с warning,
        потому что нужен локальный форвардер (proxy-forwarder.service).
        """
        if not proxy_str:
            return None

        # Если уже содержит схему — проверяем на credentials
        if "://" in proxy_str:
            scheme, rest = proxy_str.split("://", 1)
            # user:pass@host:port — отбрасываем credentials
            if "@" in rest:
                _, hostport = rest.rsplit("@", 1)
                _logger.warning(
                    "build_proxy_arg: Chrome --proxy-server не поддерживает "
                    "credentials. Используйте форвардер. Отброшено: ***@%s",
                    hostport,
                )
                return f"{scheme}://{hostport}"
            return proxy_str

        # Без схемы — парсим и собираем с socks5://
        try:
            host, port, user, password = parse_proxy(proxy_str)
        except ValueError:
            _logger.warning("build_proxy_arg: невалидный прокси %r — скип", proxy_str)
            return None

        if user or password:
            _logger.warning(
                "build_proxy_arg: Chrome --proxy-server не поддерживает "
                "credentials. Отброшены user/password для %s:%d. "
                "Используйте форвардер.",
                host,
                port,
            )

        return f"socks5://{host}:{port}"

    @staticmethod
    def cleanup_lock_files(user_data_dir: Path) -> None:
        """Удаляет lock-файлы Chrome из user-data-dir.

        Chrome оставляет SingletonLock, SingletonSocket, SingletonCookie
        при нештатном завершении. Без очистки новый инстанс не запустится.
        """
        if not user_data_dir.exists():
            return

        lock_names = ["SingletonLock", "SingletonSocket", "SingletonCookie"]
        for name in lock_names:
            path = user_data_dir / name
            try:
                if path.is_socket() or path.is_symlink():
                    path.unlink()
                elif path.exists():
                    path.unlink()
            except OSError as e:
                _logger.debug("cleanup_lock_files: не удалось удалить %s: %s", path, e)

    # ── Internal ──────────────────────────────────────────────────────────

    def _default_user_data_dir(self, account_name: str) -> Path:
        """accounts/<name>/chrome_profile относительно repo_dir."""
        if self.repo_dir:
            return self.repo_dir / "accounts" / account_name / "chrome_profile"
        return Path("accounts") / account_name / "chrome_profile"

    @staticmethod
    def _build_cmd(
        *,
        chrome_bin: str,
        user_data_dir: Path,
        proxy: str | None,
        user_agent: str,
        window_size: str,
        extra_flags: list[str] | None = None,
    ) -> list[str]:
        """Собирает список аргументов для subprocess.Popen."""
        cmd = [
            chrome_bin,
            f"--user-data-dir={user_data_dir}",
            f"--user-agent={user_agent}",
            f"--window-size={window_size}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--disable-features=TranslateUI",
            "--metrics-recording-only",
            "--disable-default-apps",
            "--disable-popup-blocking",
        ]

        # Прокси (только если задан и без credentials)
        if proxy:
            proxy_arg = ChromeLauncher.build_proxy_arg(proxy)
            if proxy_arg:
                cmd.append(f"--proxy-server={proxy_arg}")

        # --no-sandbox нужен если запускаем под root (Linux сервер)
        if platform.system() != "Windows" and os.getuid() == 0:
            cmd.append("--no-sandbox")

        # --disable-gpu для headless-серверов без видеокарты
        cmd.append("--disable-gpu")

        # Headless-режим на Linux-серверах без X-сервера.
        # Включается если:
        #   1. Явно задан CHROME_HEADLESS=1 (env)
        #   2. Или Linux + нет DISPLAY (нет X-сервера)
        if os.environ.get("CHROME_HEADLESS") == "1" or (
            platform.system() == "Linux" and not os.environ.get("DISPLAY")
        ):
            cmd.append("--headless=new")

        # Дополнительные флаги
        if extra_flags:
            cmd.extend(extra_flags)

        # URL-аргумент не передаём — Chrome откроет новую вкладку
        # (или восстановит предыдущую сессию из user-data-dir)
        cmd.append("about:blank")

        return cmd

    @staticmethod
    def _kill_proc(proc: subprocess.Popen) -> None:
        """Graceful shutdown: SIGTERM → wait → SIGKILL (или TerminateProcess)."""
        if proc.poll() is not None:
            return  # уже мёртв

        try:
            proc.terminate()  # SIGTERM на Linux, TerminateProcess на Windows
        except OSError:
            pass

        try:
            proc.wait(timeout=_GRACEFUL_SHUTDOWN_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            pass

        # Жёсткое убийство
        try:
            proc.kill()  # SIGKILL
            proc.wait(timeout=5)
        except Exception:
            pass

    # ── Pre-launch cleanup (зависшие процессы) ───────────────────────────

    def kill_orphaned_chrome(self, account_name: str, user_data_dir: Path | None = None) -> None:
        """Ищет и убивает зависший Chrome-процесс для аккаунта.

        Используется перед start() чтобы очистить зомби от предыдущего
        запуска. Поиск по debug-порту из диапазона или по user-data-dir
        в командной строке процесса.
        """
        if user_data_dir is None:
            udd = self._default_user_data_dir(account_name)
        else:
            udd = Path(user_data_dir)

        udd_str = str(udd.resolve())

        # Метод 1: найти по /proc/cmdline (Linux) или tasklist (Windows)
        system = platform.system()
        if system == "Linux":
            self._kill_orphaned_linux(udd_str)
        elif system == "Windows":
            self._kill_orphaned_windows(udd_str)
        else:
            self._kill_orphaned_macos(udd_str)

        # Очистить lock-файлы
        self.cleanup_lock_files(udd)

    @staticmethod
    def _kill_orphaned_linux(udd_str: str) -> None:
        """Ищет chrome-процессы с --user-data-dir=udd и убивает."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"chrome.*--user-data-dir={udd_str}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for pid_str in result.stdout.strip().split("\n"):
                    pid = int(pid_str.strip())
                    _logger.info("Убиваю orphaned chrome pid=%d (udd=%s)", pid, udd_str)
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(1)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except Exception as e:
            _logger.debug("_kill_orphaned_linux: %s", e)

    @staticmethod
    def _kill_orphaned_windows(udd_str: str) -> None:
        """Ищет chrome-процессы с --user-data-dir и убивает через taskkill."""
        try:
            result = subprocess.run(
                [
                    "wmic",
                    "process",
                    "where",
                    f"commandline like '%--user-data-dir={udd_str}%'",
                    "get",
                    "processid",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line.isdigit():
                        pid = int(line)
                        _logger.info("Убиваю orphaned chrome pid=%d (udd=%s)", pid, udd_str)
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True,
                            timeout=10,
                        )
        except Exception as e:
            _logger.debug("_kill_orphaned_windows: %s", e)

    @staticmethod
    def _kill_orphaned_macos(udd_str: str) -> None:
        """Ищет chrome-процессы через pgrep (macOS)."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"Google Chrome.*--user-data-dir={udd_str}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for pid_str in result.stdout.strip().split("\n"):
                    pid = int(pid_str.strip())
                    _logger.info("Убиваю orphaned chrome pid=%d (udd=%s)", pid, udd_str)
                    os.kill(pid, signal.SIGTERM)
        except Exception as e:
            _logger.debug("_kill_orphaned_macos: %s", e)
