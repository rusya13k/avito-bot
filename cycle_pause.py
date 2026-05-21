"""
T19: Stagger cycle pauses — lognormal + долгие перерывы «обед/ужин».

Раньше пауза между циклами в `_run_main_loop` была
`random.uniform(pause_min*60, pause_max*60)` (uniform), без длинных
перерывов. Гистограмма пауз — равномерная коробка [30, 90] мин,
fingerprinting видит «как из генератора».

T19 даёт два изменения:

1. **Lognormal** для обычных пауз вместо uniform — реальные паузы
   человека имеют длинный правый хвост: чаще короткие, реже длинные.
2. **Long breaks «обед / ужин»**: 1-2 раза в день — длинный перерыв
   120-300 мин в окне обеда (12-14) или ужина (18-21).

Public API (pure-функции, удобные для тестирования):

    pick_cycle_pause(account, cfg, *, account_state, account_name,
                     now=None, rng=None) -> tuple[float, str]

Возвращает (seconds, label) где label ∈ {"regular", "long_break"}.

Side effects:
    Если выпал long break, вызывает `account_state.record_long_break(name)`
    ДО возврата — чтобы счётчик инкрементировался даже при крэше / рестарте
    во время самого `sleep`.
"""

from __future__ import annotations

import datetime as _dt
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


# Окна «обед» и «ужин» — часы локального времени. В эти окна с
# повышенной вероятностью выпадает long break.
LUNCH_WINDOW = (12, 14)  # включительно 12, 13 — берёт паузу до ~16
DINNER_WINDOW = (18, 21)  # 18, 19, 20 — пауза до ~22

# Дефолты конфигурации (override через cfg / account).
DEFAULT_LONG_BREAK_MIN_MIN = 120.0  # 2 часа
DEFAULT_LONG_BREAK_MAX_MIN = 300.0  # 5 часов
DEFAULT_LONG_BREAKS_PER_DAY = 2  # максимум long break'ов в сутки
DEFAULT_LONG_BREAK_CHANCE_IN_WINDOW = 0.30  # в окне обеда/ужина
DEFAULT_LONG_BREAK_CHANCE_OUT_WINDOW = 0.05  # вне окна (всё равно бывает)


def _in_window(hour: int, window: tuple[int, int]) -> bool:
    """True если час входит в [window[0], window[1])."""
    lo, hi = window
    return lo <= hour < hi


def _is_meal_window(hour: int) -> bool:
    """True если сейчас «обед» или «ужин»."""
    return _in_window(hour, LUNCH_WINDOW) or _in_window(hour, DINNER_WINDOW)


def _resolve(account: dict | None, cfg: dict | None, key: str, default: Any) -> Any:
    """Per-account override > cfg > default."""
    if account is not None and key in account and account[key] is not None:
        return account[key]
    if cfg is not None and key in cfg and cfg[key] is not None:
        return cfg[key]
    return default


def _lognormal_seconds(
    lo_min: float,
    hi_min: float,
    *,
    rng: random.Random | None = None,
) -> float:
    """Lognormal sample, скейлится под [lo, hi] минуты, возвращает секунды.

    Алгоритм такой же, как в `human_delay._draw_seconds(distribution="lognormal")`:
    `sample = lognormvariate(0, 0.4) * mid * 0.7`, clamp в [lo, hi*2].
    Это даёт пик ~mid, длинный правый хвост, минимум lo.

    Args:
        lo_min, hi_min: границы в минутах (mid = (lo+hi)/2).
        rng: для тестирования (детерминированный random.Random).
    """
    if rng is None:
        rng = random
    if hi_min <= lo_min:
        return max(0.0, lo_min * 60.0)
    mid = (lo_min + hi_min) / 2.0
    sample = rng.lognormvariate(0.0, 0.4) * mid * 0.7
    minutes = max(lo_min, min(hi_min * 2.0, sample))
    return minutes * 60.0


def _uniform_seconds(
    lo_min: float,
    hi_min: float,
    *,
    rng: random.Random | None = None,
) -> float:
    """Uniform sample в минутах, возвращает секунды (для long-break)."""
    if rng is None:
        rng = random
    if hi_min <= lo_min:
        return max(0.0, lo_min * 60.0)
    return rng.uniform(lo_min, hi_min) * 60.0


def pick_cycle_pause(
    account: dict | None,
    cfg: dict | None,
    *,
    account_state: Any,
    account_name: str,
    now: _dt.datetime | None = None,
    rng: random.Random | None = None,
) -> tuple[float, str]:
    """T19: выбрать длительность паузы после очередного цикла.

    Принимает решение:
    - lognormal regular pause в [pause_min, pause_max] минут (норма)
      ИЛИ
    - long break в [long_break_min, long_break_max] минут (~2-5 часов),
      если ещё не достигнут лимит long_breaks_per_day и выпал бросок.

    Вероятность long break повышена в окнах обеда (12-14) и ужина (18-21).

    Args:
        account: dict из accounts.json (per-account overrides).
        cfg: глобальный config.json (fallback).
        account_state: AccountState инстанс — для count/record_long_break.
        account_name: имя аккаунта (ключ для AccountState).
        now: datetime.datetime — для тестов. Default = datetime.now().
        rng: random.Random — для тестов. Default = random module.

    Returns:
        (seconds, label): seconds для sleep'а, label — "regular" или
        "long_break".

    Side effect:
        При выборе long break вызывает `account_state.record_long_break`.
    """
    if rng is None:
        rng = random
    if now is None:
        now = _dt.datetime.now()

    # Resolve config values (account override > cfg > defaults).
    pause_min = float(_resolve(account, cfg, "session_pause_min", 30.0))
    pause_max = float(_resolve(account, cfg, "session_pause_max", 90.0))
    long_break_min = float(_resolve(account, cfg, "long_break_min_min", DEFAULT_LONG_BREAK_MIN_MIN))
    long_break_max = float(_resolve(account, cfg, "long_break_max_min", DEFAULT_LONG_BREAK_MAX_MIN))
    long_breaks_per_day = int(
        _resolve(account, cfg, "long_breaks_per_day", DEFAULT_LONG_BREAKS_PER_DAY)
    )
    chance_in_window = float(
        _resolve(
            account,
            cfg,
            "long_break_chance_in_window",
            DEFAULT_LONG_BREAK_CHANCE_IN_WINDOW,
        )
    )
    chance_out_window = float(
        _resolve(
            account,
            cfg,
            "long_break_chance_out_window",
            DEFAULT_LONG_BREAK_CHANCE_OUT_WINDOW,
        )
    )

    # Сколько long break'ов уже было сегодня?
    taken = account_state.count_long_breaks_today(account_name)
    can_long_break = taken < long_breaks_per_day and long_breaks_per_day > 0

    if can_long_break:
        hour = now.hour
        chance = chance_in_window if _is_meal_window(hour) else chance_out_window
        if rng.random() < chance:
            seconds = _uniform_seconds(long_break_min, long_break_max, rng=rng)
            account_state.record_long_break(account_name)
            return seconds, "long_break"

    # Regular lognormal pause.
    seconds = _lognormal_seconds(pause_min, pause_max, rng=rng)
    return seconds, "regular"
