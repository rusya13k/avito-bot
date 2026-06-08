"""
Тесты для ChromeLauncher — замена AdsPower для запуска Chrome.

Покрываем:
- build_proxy_arg: конвертация proxy-строк для --proxy-server
- cleanup_lock_files: удаление SingletonLock/Socket/Cookie
- _find_free_port: порт из диапазона
- _build_cmd: сборка CLI-команды Chrome
- start/stop/is_running lifecycle (мок subprocess + requests)
"""

import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chrome_launcher import (
    ChromeLauncher,
)

# ── build_proxy_arg ──────────────────────────────────────────────────────


def test_build_proxy_arg_none_returns_none():
    """None → None (без прокси)."""
    assert ChromeLauncher.build_proxy_arg(None) is None


def test_build_proxy_arg_empty_returns_none():
    """Пустая строка → None."""
    assert ChromeLauncher.build_proxy_arg("") is None


def test_build_proxy_arg_socks5_with_scheme():
    """socks5://host:port → как есть."""
    assert ChromeLauncher.build_proxy_arg("socks5://127.0.0.1:10800") == "socks5://127.0.0.1:10800"


def test_build_proxy_arg_host_port_adds_socks5():
    """host:port → socks5://host:port."""
    assert ChromeLauncher.build_proxy_arg("127.0.0.1:10800") == "socks5://127.0.0.1:10800"


def test_build_proxy_arg_with_credentials_strips_them():
    """user:pass@host:port → socks5://host:port (credentials отброшены)."""
    result = ChromeLauncher.build_proxy_arg("alice:pwd@1.2.3.4:1080")
    assert result == "socks5://1.2.3.4:1080"


def test_build_proxy_arg_host_port_user_pass_strips_credentials():
    """host:port:user:pass → socks5://host:port."""
    result = ChromeLauncher.build_proxy_arg("1.2.3.4:1080:alice:pwd")
    assert result == "socks5://1.2.3.4:1080"


def test_build_proxy_arg_socks5_with_credentials_strips():
    """socks5://user:pass@host:port → socks5://host:port."""
    result = ChromeLauncher.build_proxy_arg("socks5://alice:pwd@1.2.3.4:1080")
    assert result == "socks5://1.2.3.4:1080"


def test_build_proxy_arg_invalid_returns_none():
    """Невалидный формат → None."""
    assert ChromeLauncher.build_proxy_arg("no_port_here") is None


# ── cleanup_lock_files ───────────────────────────────────────────────────


def test_cleanup_lock_files_removes_singleton_files(tmp_path):
    """Удаляет SingletonLock, SingletonSocket, SingletonCookie."""
    for name in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
        (tmp_path / name).write_text("test", encoding="utf-8")

    ChromeLauncher.cleanup_lock_files(tmp_path)

    for name in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
        assert not (tmp_path / name).exists()


def test_cleanup_lock_files_no_dir_no_error(tmp_path):
    """Несуществующая директория — не падает."""
    ChromeLauncher.cleanup_lock_files(tmp_path / "nonexistent")


def test_cleanup_lock_files_missing_files_no_error(tmp_path):
    """Нет lock-файлов — не падает."""
    ChromeLauncher.cleanup_lock_files(tmp_path)


# ── _build_cmd ───────────────────────────────────────────────────────────


def test_build_cmd_minimal():
    """Минимальный набор флагов — user-data-dir, user-agent, без --remote-debugging-port."""
    cmd = ChromeLauncher._build_cmd(
        chrome_bin="/usr/bin/google-chrome",
        user_data_dir=Path("/tmp/profile"),
        proxy=None,
        user_agent="TestAgent",
        window_size="1920,1080",
    )
    # --remote-debugging-port НЕ должен быть в списке — добавляется в _start_chrome
    assert not any(f.startswith("--remote-debugging-port") for f in cmd)
    # Path() нормализует separators — на Windows будет \tmp\profile
    udd_flag = f"--user-data-dir={Path('/tmp/profile')}"
    assert udd_flag in cmd
    assert "--user-agent=TestAgent" in cmd
    # about:blank — последний аргумент
    assert cmd[-1] == "about:blank"


def test_build_cmd_with_proxy():
    """Proxy добавляется как --proxy-server=..."""
    cmd = ChromeLauncher._build_cmd(
        chrome_bin="/usr/bin/google-chrome",
        user_data_dir=Path("/tmp/profile"),
        proxy="socks5://127.0.0.1:10800",
        user_agent="TestAgent",
        window_size="1920,1080",
    )
    assert "--proxy-server=socks5://127.0.0.1:10800" in cmd


def test_build_cmd_with_credentials_proxy_strips():
    """Proxy с credentials — credentials отбрасываются."""
    cmd = ChromeLauncher._build_cmd(
        chrome_bin="/usr/bin/google-chrome",
        user_data_dir=Path("/tmp/profile"),
        proxy="user:pass@1.2.3.4:1080",
        user_agent="TestAgent",
        window_size="1920,1080",
    )
    assert "--proxy-server=socks5://1.2.3.4:1080" in cmd


def test_build_cmd_extra_flags():
    """Дополнительные флаги прокидываются."""
    cmd = ChromeLauncher._build_cmd(
        chrome_bin="/usr/bin/google-chrome",
        user_data_dir=Path("/tmp/profile"),
        proxy=None,
        user_agent="TestAgent",
        window_size="1920,1080",
        extra_flags=["--headless=new", "--disable-dev-shm-usage"],
    )
    assert "--headless=new" in cmd
    assert "--disable-dev-shm-usage" in cmd


# ── ChromeLauncher lifecycle ─────────────────────────────────────────────


@pytest.fixture
def launcher(tmp_path):
    return ChromeLauncher(repo_dir=tmp_path)


def _mock_chrome_start(monkeypatch, return_port=9222):
    """Helper: мокает _start_chrome + _find_chrome_binary."""
    monkeypatch.setattr("chrome_launcher._find_chrome_binary", lambda: "/usr/bin/chrome")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    monkeypatch.setattr(
        "chrome_launcher._start_chrome",
        lambda cmd_template, **kw: (mock_proc, return_port),
    )
    return mock_proc


def test_start_creates_user_data_dir(launcher, tmp_path, monkeypatch):
    """start() создаёт chrome_profile_dir если его нет."""
    _mock_chrome_start(monkeypatch)

    udd = tmp_path / "accounts" / "acc1" / "chrome_profile"
    assert not udd.exists()

    launcher.start("acc1", user_data_dir=udd)

    assert udd.exists()
    assert launcher.is_running("acc1")


def test_stop_kills_process(launcher, monkeypatch):
    """stop() убивает процесс и убирает из трекера."""
    _mock_chrome_start(monkeypatch)

    launcher.start("acc1")
    assert launcher.is_running("acc1")

    # Мокаем _kill_proc чтобы не вызывать os.kill
    with patch.object(ChromeLauncher, "_kill_proc"):
        # Мокаем socket.connect чтобы port check не вис
        with patch("socket.socket"):
            launcher.stop("acc1")

    assert not launcher.is_running("acc1")


def test_stop_nonexistent_account_no_error(launcher):
    """stop() для незарегистрированного аккаунта — не падает."""
    launcher.stop("nonexistent")


def test_get_debug_port_returns_port(launcher, monkeypatch):
    """get_debug_port() возвращает порт после start()."""
    _mock_chrome_start(monkeypatch)

    port = launcher.start("acc1")
    assert launcher.get_debug_port("acc1") == port


def test_get_debug_port_none_for_unknown(launcher):
    """get_debug_port() для незарегистрированного аккаунта → None."""
    assert launcher.get_debug_port("unknown") is None


def test_start_raises_on_chrome_not_ready(monkeypatch, launcher):
    """Если Chrome не отвечает — RuntimeError."""
    monkeypatch.setattr("chrome_launcher._find_chrome_binary", lambda: "/usr/bin/chrome")
    monkeypatch.setattr(
        "chrome_launcher._start_chrome",
        lambda cmd_template, **kw: (_ for _ in ()).throw(RuntimeError("Chrome не ответил")),
    )

    with pytest.raises(RuntimeError, match="не ответил"):
        launcher.start("acc1")


def test_start_raises_on_chrome_not_found(monkeypatch, launcher):
    """Если chrome бинарник не найден — FileNotFoundError."""
    monkeypatch.setattr(
        "chrome_launcher._find_chrome_binary",
        lambda: (_ for _ in ()).throw(FileNotFoundError("no chrome")),
    )

    with pytest.raises(FileNotFoundError):
        launcher.start("acc1")


def test_default_user_data_dir(launcher, tmp_path):
    """_default_user_data_dir = repo_dir/accounts/name/chrome_profile."""
    result = launcher._default_user_data_dir("myaccount")
    assert result == tmp_path / "accounts" / "myaccount" / "chrome_profile"


def test_stop_all(launcher, monkeypatch):
    """stop_all() останавливает все процессы."""
    _mock_chrome_start(monkeypatch)

    launcher.start("acc1")
    launcher.start("acc2")

    with patch.object(ChromeLauncher, "_kill_proc"):
        with patch("socket.socket"):
            launcher.stop_all()

    assert not launcher.is_running("acc1")
    assert not launcher.is_running("acc2")


def test_start_kills_existing_before_restart(launcher, monkeypatch):
    """Если аккаунт уже запущен — kill + restart."""
    _mock_chrome_start(monkeypatch)

    launcher.start("acc1")
    assert launcher.is_running("acc1")

    # Restart — старый процесс должен быть убит
    with patch.object(ChromeLauncher, "_kill_proc") as mock_kill:
        launcher.start("acc1")
        mock_kill.assert_called()


# ── kill_orphaned_chrome ─────────────────────────────────────────────────


def test_kill_orphaned_cleanup_lock_files(launcher, tmp_path):
    """kill_orphaned_chrome очищает lock-файлы."""
    udd = tmp_path / "accounts" / "acc1" / "chrome_profile"
    udd.mkdir(parents=True, exist_ok=True)
    (udd / "SingletonLock").write_text("test", encoding="utf-8")

    # Мокаем OS-specific kill (чтобы не зависеть от платформы)
    with patch.object(
        ChromeLauncher, f"_kill_orphaned_{_get_os_key()}", staticmethod(lambda x: None)
    ):
        launcher.kill_orphaned_chrome("acc1", user_data_dir=udd)

    assert not (udd / "SingletonLock").exists()


def _get_os_key():
    import platform

    s = platform.system()
    if s == "Linux":
        return "linux"
    elif s == "Windows":
        return "windows"
    return "macos"
