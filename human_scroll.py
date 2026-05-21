"""
T9 + T10: реалистичный scroll-флоу.

Старый `bot.human_scroll`:

    for _ in range(3..7):
        scrollBy(0, 150..500)
        sleep(0.3..1.1)

— это выглядит как «дёрг-дёрг-дёрг» равными порциями, БЕЗ инерции
(jumpy scroll), без зависимости от контента, без длинных reading-пауз.
Лёгкий сигнал бота.

Новая модель:

1. **Inertia-scroll**: один «свайп» — это 10-20 микро-скроллов с
   замедлением по cubic ease-out (быстро в начале, медленно в конце).
   Между микро-шагами 10-30ms. Это даёт настоящее «инерционное»
   движение, какое получается от touchpad'а или mouse-wheel.

2. **Reading pauses**: после каждого свайпа — пауза, длина которой
   зависит от объёма видимого текста (`scan_visible_text_length`).
   На «густой» странице (5-10 абзацев) — 5-15 сек, на пустой — 0.5-2.

3. **Micro-stops** между свайпами: 200-400ms (моторика не идеальна,
   палец возвращается на колесо).

4. **Back-scroll** ~15% — перечитываем кусок выше.

5. **content-aware dwell** (T10): `compute_reading_dwell(description,
   images, interest_score)` для view_listing.

Public API:

    human_scroll(driver, direction='down', *, swipes=None, ...)
        Полный «прокрутить страницу» — несколько свайпов с inertia и
        reading-паузами. Заменяет bot.human_scroll.

    inertia_scroll(driver, amount_px, *, stop_event=None)
        Один свайп (низкоуровневый — для view_listing scrollIntoView и т.п.).

    compute_reading_dwell(*, description, image_count, interest=0.5) -> float
        Сколько секунд читать листинг (T10).

    visible_text_chars(driver) -> int
        Грубая оценка объёма текста на текущей видимой странице (для
        adaptive reading pauses).
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Any

from selenium.common.exceptions import WebDriverException

from human_delay import human_delay as _human_delay

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Inertia
# ─────────────────────────────────────────────────────────────────────────────


def _ease_out_cubic(t: float) -> float:
    """t ∈ [0, 1] → eased ∈ [0, 1] с замедлением к концу."""
    return 1.0 - (1.0 - t) ** 3


def inertia_scroll(
    driver: Any,
    amount_px: int,
    *,
    steps_range: tuple[int, int] = (10, 20),
    pause_range: tuple[float, float] = (0.010, 0.030),
    stop_event: threading.Event | None = None,
) -> bool:
    """T9: один свайп с замедлением по cubic ease-out.

    Положительное `amount_px` — вниз, отрицательное — вверх.

    Returns:
        True — свайп выполнен (или почти выполнен; раннее прерывание
            по stop_event тоже True, мы что-то проскроллили).
        False — драйвер упал, свайп не выполнен.
    """
    if amount_px == 0:
        return True

    steps = random.randint(*steps_range)
    pause_lo, pause_hi = pause_range

    # Нарезаем amount по cubic ease-out: на каждом шаге считаем,
    # СКОЛЬКО мы должны были бы проскроллить от старта (eased * total),
    # и берём дельту от предыдущего шага.
    prev_eased = 0.0
    for i in range(1, steps + 1):
        if stop_event is not None and stop_event.is_set():
            return True
        t = i / steps
        eased = _ease_out_cubic(t)
        delta = (eased - prev_eased) * amount_px
        prev_eased = eased
        try:
            driver.execute_script(f"window.scrollBy(0, {delta:.2f});")
        except WebDriverException as exc:
            logger.debug("inertia_scroll: scrollBy failed — %s", exc)
            return False
        # Микро-пауза между шагами — 10-30ms.
        time.sleep(random.uniform(pause_lo, pause_hi))
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Visible text estimate
# ─────────────────────────────────────────────────────────────────────────────


# JS, который возвращает приблизительный размер видимого текста на странице.
# Берём только элементы внутри viewport (по getBoundingClientRect) и
# суммируем длины их innerText. Не идеально (не учитывает overlap), но
# даёт стабильную оценку «густо или пусто».
_VISIBLE_TEXT_JS = r"""
(() => {
  try {
    const vh = window.innerHeight || document.documentElement.clientHeight || 0;
    const vw = window.innerWidth || document.documentElement.clientWidth || 0;
    let total = 0;
    // Берём только смысловые контейнеры — иначе считаем текст много раз.
    const tags = ['p', 'li', 'h1', 'h2', 'h3', 'span', 'div'];
    const seen = new WeakSet();
    for (const tag of tags) {
      const els = document.getElementsByTagName(tag);
      for (let i = 0; i < els.length && i < 1000; i++) {
        const el = els[i];
        if (seen.has(el)) continue;
        seen.add(el);
        try {
          const r = el.getBoundingClientRect();
          if (r.bottom < 0 || r.top > vh) continue;
          if (r.right < 0 || r.left > vw) continue;
          // Берём только «листовые» текстовые узлы:
          // если у элемента есть дочерние с текстом, родитель пропускаем.
          if (el.children && el.children.length > 0 &&
              tag !== 'p' && tag !== 'li' && tag !== 'h1' &&
              tag !== 'h2' && tag !== 'h3') continue;
          const t = (el.innerText || '').trim();
          if (t.length > 0) total += t.length;
        } catch (_) { /* detached элемент — пропускаем */ }
      }
    }
    return total;
  } catch (e) { return 0; }
})()
"""


def visible_text_chars(driver: Any) -> int:
    """T9: грубая оценка объёма видимого текста (chars).

    Работает на любой странице (JS поверх getBoundingClientRect).
    На ошибку → 0 (consumer падёт в default reading-time).
    """
    try:
        result = driver.execute_script(_VISIBLE_TEXT_JS)
        return int(result) if result is not None else 0
    except WebDriverException as exc:
        logger.debug("visible_text_chars: failed — %s", exc)
        return 0


def reading_time_for_chars(chars: int) -> tuple[float, float]:
    """T9: на сколько (lo, hi) секунд читать N chars.

    Базируется на средней скорости чтения: 200 слов/мин ≈ 1000 chars/мин
    ≈ 16 chars/сек. Но это «вдумчивое» чтение; в скан-режиме (что
    обычно при просмотре листинга) — 30-60 chars/сек.

    Минимум: 0.5 сек (даже на пустой странице глаз пробегает).
    Максимум: 30 сек (длинный абзац с описанием).
    """
    if chars <= 0:
        return (0.3, 1.5)
    # Скан: 30-60 chars/сек → диапазон (chars/60, chars/30).
    lo = max(0.5, chars / 60.0)
    hi = max(lo + 0.5, chars / 30.0)
    # Clamp.
    lo = min(lo, 30.0)
    hi = min(hi, 30.0)
    return (lo, hi)


# ─────────────────────────────────────────────────────────────────────────────
# Public scroll API
# ─────────────────────────────────────────────────────────────────────────────


def human_scroll(
    driver: Any,
    direction: str = "down",
    *,
    swipes: int | None = None,
    swipe_range: tuple[int, int] = (300, 900),
    back_scroll_chance: float = 0.15,
    reading_pause: bool = True,
    stop_event: threading.Event | None = None,
) -> bool:
    """T9: «прокрутить страницу» — несколько inertia-свайпов с reading-паузами.

    Заменяет bot.human_scroll. Поведение:

    1. Делаем `swipes` свайпов (default 3-6) в указанном направлении.
    2. Каждый свайп — 300-900px по cubic ease-out (inertia_scroll).
    3. После КАЖДОГО свайпа — reading-пауза, длина адаптивная по
       объёму видимого текста (если reading_pause=True).
    4. Между свайпами — micro-stop 200-400ms (моторика).
    5. С вероятностью back_scroll_chance (default 15%) делаем «откат»
       вверх на 100-300px (перечитать).

    Args:
        direction: "down" или "up".
        swipes: количество свайпов; None → random 3-6.
        swipe_range: (min, max) px одного свайпа.
        back_scroll_chance: 0..1 — шанс отката за КАЖДЫЙ свайп.
        reading_pause: True — паузы зависят от объёма текста на
            экране; False — короткие фиксированные паузы (для тестов
            или там где adaptive не нужен).
        stop_event: прерываемся, если сработал.

    Returns:
        True если выполнили хотя бы один свайп.
    """
    if swipes is None:
        swipes = random.randint(3, 6)
    if swipes <= 0:
        return False

    sign = 1 if direction == "down" else -1
    swipe_lo, swipe_hi = swipe_range

    did_anything = False
    for swipe_idx in range(swipes):
        if stop_event is not None and stop_event.is_set():
            return did_anything

        # T9: иногда «откат» — листаем чуть назад прежде чем продолжить.
        if did_anything and random.random() < back_scroll_chance:
            back_amount = -sign * random.randint(100, 300)
            inertia_scroll(driver, back_amount, stop_event=stop_event)
            # Короткая reading-пауза на возврате.
            _human_delay(0.5, 1.5, stop_event=stop_event)

        amount = sign * random.randint(swipe_lo, swipe_hi)
        if not inertia_scroll(driver, amount, stop_event=stop_event):
            # Драйвер сломался — выходим.
            return did_anything
        did_anything = True

        # Reading-пауза.
        if reading_pause:
            chars = visible_text_chars(driver)
            lo, hi = reading_time_for_chars(chars)
            _human_delay(lo, hi, distribution="lognormal", stop_event=stop_event)
        else:
            _human_delay(0.3, 0.9, stop_event=stop_event)

        # Micro-stop между свайпами (если ещё не последний).
        if swipe_idx < swipes - 1:
            _human_delay(0.2, 0.4, stop_event=stop_event)

    return did_anything


# ─────────────────────────────────────────────────────────────────────────────
# T10: content-aware dwell
# ─────────────────────────────────────────────────────────────────────────────


def compute_reading_dwell(
    *,
    description: str | None = None,
    image_count: int = 0,
    interest: float = 0.5,
    base_min: float = 5.0,
    base_max: float = 300.0,
) -> float:
    """T10: рассчитать realistic dwell time для одного листинга.

    Формула: dwell = base × (1 + α·log(text_chars+1)) × (1 + β·image_count)
                  × (0.5 + interest)

    где:
      • text_chars — длина description (proxy для «сколько читать»).
      • image_count — сколько фото в галерее (proxy для «есть что
        полистать»).
      • interest ∈ [0, 1] — насколько листинг релевантен пользователю.
        0 = «совсем не моё», 1 = «прям то что надо».

    Reasonable values:
      • Empty description, 0 фото, interest=0 → ~5 сек (быстро закрыл).
      • Длинное описание, 10 фото, interest=0.5 → ~40 сек (типичный).
      • Длинное описание, 15 фото, interest=1.0 → ~150 сек (зацепило).

    Args:
        description: текст описания (None / "" → 0 chars).
        image_count: число фото в галерее.
        interest: 0..1 — interest score (0 = неинтересно, 1 = очень).
        base_min, base_max: hard clamp на результат.

    Returns:
        Dwell time в секундах в диапазоне [base_min, base_max].
    """
    text_chars = len(description.strip()) if description else 0
    interest = max(0.0, min(1.0, interest))

    base = 8.0  # baseline: «открыл и сразу понятно»
    text_factor = 1.0 + 0.6 * math.log(text_chars + 1)  # 1.0 → 4.5 для 1000 chars
    image_factor = 1.0 + 0.04 * min(image_count, 20)  # 1.0 → 1.8 для 20 фото
    interest_factor = 0.5 + interest  # 0.5 → 1.5 (×3 при максимальном интересе)

    dwell = base * text_factor * image_factor * interest_factor

    # Add small noise (±10%), чтобы при равных входах разные dwell'ы.
    dwell *= random.uniform(0.9, 1.1)

    return max(base_min, min(base_max, dwell))
