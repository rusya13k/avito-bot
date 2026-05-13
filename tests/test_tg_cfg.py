"""
L5/L8 tests: TelegramController._cfg / _save_cfg.

L5: mtime-cache (no disk read on repeated _cfg() call), invalidates on
    external mtime change.
L8: _save_cfg() — atomic via tempfile + os.replace.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tg_ctrl(tmp_path, monkeypatch):
    """TelegramController with BASE pointing to tmp_path and mocked telebot."""
    import telebot

    monkeypatch.setattr(telebot, "TeleBot", lambda *a, **kw: MagicMock())

    from tg_bot import TelegramController

    monkeypatch.setattr(TelegramController, "BASE", tmp_path)
    # _setup() registers handlers on the (now-mocked) bot — it's safe.
    ctrl = TelegramController(token="test", admin_id=0)
    return ctrl, tmp_path


def _write_cfg(path: Path, cfg: dict):
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def test_cfg_reads_initial_value(tg_ctrl):
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1"})
    assert ctrl._cfg()["key"] == "v1"


def test_cfg_returns_deepcopy(tg_ctrl):
    """L5: мутация результата _cfg() НЕ влияет на кэш."""
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1", "list": [1, 2, 3]})
    cfg = ctrl._cfg()
    cfg["key"] = "MUTATED"
    cfg["list"].append(99)
    cfg2 = ctrl._cfg()
    assert cfg2["key"] == "v1"
    assert cfg2["list"] == [1, 2, 3]


def test_cfg_uses_cache_on_repeated_calls(tg_ctrl, monkeypatch):
    """L5: повторные _cfg() при том же mtime НЕ читают файл."""
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1"})
    ctrl._cfg()  # первое чтение — наполняет кэш

    import builtins

    real_open = builtins.open
    open_calls = []

    def tracked_open(*args, **kwargs):
        # отслеживаем только открытия config.json
        if args and "config.json" in str(args[0]):
            open_calls.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)
    # три повторных вызова — должны попадать в кэш.
    for _ in range(3):
        ctrl._cfg()
    assert open_calls == []


def test_cfg_invalidates_on_external_mtime_change(tg_ctrl):
    """L5: если кто-то извне изменил config.json — _cfg() это видит."""
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1"})
    assert ctrl._cfg()["key"] == "v1"

    # имитируем внешнюю запись (например, ручное редактирование).
    # mtime гарантированно изменится — даём фс время продвинуть таймер.
    time.sleep(0.01)
    _write_cfg(base / "config.json", {"key": "v2"})
    # форсируем разный mtime даже на ФС с грубой точностью (FAT32 etc).
    new_mtime = (base / "config.json").stat().st_mtime + 1
    os.utime(base / "config.json", (new_mtime, new_mtime))

    assert ctrl._cfg()["key"] == "v2"


def test_save_cfg_atomic_write(tg_ctrl):
    """L8: _save_cfg() пишет через tempfile + os.replace."""
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1"})

    ctrl._save_cfg({"key": "v2", "extra": [1, 2, 3]})

    on_disk = json.loads((base / "config.json").read_text(encoding="utf-8"))
    assert on_disk == {"key": "v2", "extra": [1, 2, 3]}


def test_save_cfg_no_temp_files_left(tg_ctrl):
    """L8: после успешной записи временных .config-*.tmp не остаётся."""
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1"})

    ctrl._save_cfg({"key": "v2"})

    leftover = list(base.glob(".config-*.tmp"))
    assert leftover == []


def test_save_cfg_updates_cache(tg_ctrl, monkeypatch):
    """L5+L8: после _save_cfg() кэш обновлён, повторный _cfg() не читает диск."""
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1"})
    ctrl._cfg()  # наполняем кэш

    ctrl._save_cfg({"key": "v2"})

    # ловим открытия файла после save_cfg
    import builtins

    real_open = builtins.open
    open_calls = []

    def tracked_open(*args, **kwargs):
        if args and "config.json" in str(args[0]):
            open_calls.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)
    cfg = ctrl._cfg()
    assert cfg["key"] == "v2"
    assert open_calls == []  # cache hit


def test_save_cfg_returns_copy(tg_ctrl):
    """L5: _save_cfg() сохраняет копию — мутация снаружи не портит кэш."""
    ctrl, base = tg_ctrl
    _write_cfg(base / "config.json", {"key": "v1"})

    incoming = {"key": "v2", "list": [1, 2]}
    ctrl._save_cfg(incoming)
    incoming["key"] = "MUTATED_AFTER_SAVE"
    incoming["list"].append(99)

    cfg = ctrl._cfg()
    assert cfg["key"] == "v2"
    assert cfg["list"] == [1, 2]
