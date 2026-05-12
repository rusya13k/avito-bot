"""
Human-like delays (B2).

Раньше повсюду был `time.sleep(random.uniform(lo, hi))` — это **равномерное**
распределение, которое легко детектится антифродом по гистограмме.

Здесь реализуем более естественные паттерны:

    - distribution='normal' (default) — нормальное распределение с mean=(lo+hi)/2
      и std=(hi-lo)/6 (≈99.7% значений попадают в [lo, hi] по правилу 3σ),
      затем clamp в [lo, hi]. Так получается «колокол» с пиком в середине.
    - distribution='uniform' — старое поведение (для совместимости).
    - distribution='lognormal' — длинный правый хвост, удобно для редких
      «задумался на 10s» пауз.

Также поддерживается ранний выход при `stop_event.is_set()`: длинные sleep'ы
разбиваются на чанки по `STOP_CHECK_INTERVAL`, чтобы команда «стоп» из TG
не висела в очереди до конца паузы.

API:
    human_delay(0.5, 1.5)                 # normal
    human_delay(2, 5, distribution='lognormal')
    human_delay(10, 30, stop_event=ev)    # прерывается, если ev.is_set()
"""

from __future__ import annotations

import random
import threading
import time

# Чанк, на который дробим длинные sleep'ы для проверки stop_event.
# Не делаем слишком мелким — иначе тратим CPU на context switches.
STOP_CHECK_INTERVAL = 0.5


def _draw_seconds(lo: float, hi: float, distribution: str) -> float:
    if hi <= lo:
        return max(0.0, lo)

    if distribution == "uniform":
        return random.uniform(lo, hi)

    if distribution == "lognormal":
        # Сдвинутое логнормальное распределение: pos-skew, длинный правый хвост.
        # mu=ln(mean) приблизительно ставим на (lo+hi)/2; sigma=0.5 — умеренная.
        mid = (lo + hi) / 2.0
        # Чтобы попадать в +/- разумный диапазон.
        sample = random.lognormvariate(0.0, 0.4) * mid * 0.7
        return max(lo, min(hi * 2.0, sample))

    # default: normal — пик в середине, ±3σ ≈ [lo, hi]
    mean = (lo + hi) / 2.0
    std = (hi - lo) / 6.0
    sample = random.gauss(mean, std)
    return max(lo, min(hi, sample))


def human_delay(
    lo: float,
    hi: float,
    *,
    distribution: str = "normal",
    stop_event: threading.Event | None = None,
) -> float:
    """
    Спит «по-человечески» от ~lo до ~hi секунд.

    Args:
        lo, hi: границы (lo <= hi). Для normal — это ±3σ.
        distribution: "normal" | "uniform" | "lognormal"
        stop_event: если задан — sleep дробится на чанки и прерывается,
                    как только event сработает.

    Returns:
        Фактически потраченное время, в секундах (для логов / тестов).
    """
    seconds = _draw_seconds(lo, hi, distribution)
    if seconds <= 0:
        return 0.0

    if stop_event is None:
        time.sleep(seconds)
        return seconds

    # Дробим, чтобы можно было выйти раньше.
    elapsed = 0.0
    start = time.time()
    while elapsed < seconds:
        if stop_event.is_set():
            break
        chunk = min(STOP_CHECK_INTERVAL, seconds - elapsed)
        time.sleep(chunk)
        elapsed = time.time() - start
    return elapsed
