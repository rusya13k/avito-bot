"""
T9 + T10: тесты для human_scroll (inertia, reading pauses,
content-aware dwell).
"""

from __future__ import annotations

import random
import threading

import pytest
from selenium.common.exceptions import WebDriverException

import human_scroll
from human_scroll import (
    _ease_out_cubic,
    compute_reading_dwell,
    inertia_scroll,
    reading_time_for_chars,
    visible_text_chars,
)
from human_scroll import (
    human_scroll as scroll_page,
)

# ─────────────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────────────


class _FakeDriver:
    """Driver-stub: записывает каждый scrollBy в self.scrolls."""

    def __init__(
        self,
        *,
        text_chars: int = 0,
        execute_raises: Exception | None = None,
    ):
        self.scrolls: list[float] = []
        self.script_calls: list[str] = []
        self.text_chars = text_chars
        self.execute_raises = execute_raises

    def execute_script(self, script: str, *args):
        self.script_calls.append(script)
        if self.execute_raises is not None:
            raise self.execute_raises
        # scrollBy(0, X) → запись X
        if "scrollBy" in script:
            # extract number after scrollBy(0,
            import re

            m = re.search(r"scrollBy\(0,\s*(-?[\d.]+)\)", script)
            if m:
                self.scrolls.append(float(m.group(1)))
            return None
        # visible text JS
        if "innerHeight" in script and "innerText" in script:
            return self.text_chars
        return None


@pytest.fixture
def patch_sleep(monkeypatch):
    """time.sleep + human_delay → noop, чтобы тесты были быстрые."""
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(float(s))

    monkeypatch.setattr(human_scroll.time, "sleep", fake_sleep)
    monkeypatch.setattr(human_scroll, "_human_delay", lambda *a, **kw: 0.0)
    return sleeps


# ─────────────────────────────────────────────────────────────────────────────
# _ease_out_cubic — математика
# ─────────────────────────────────────────────────────────────────────────────


def test_ease_out_cubic_endpoints():
    assert _ease_out_cubic(0.0) == 0.0
    assert _ease_out_cubic(1.0) == 1.0


def test_ease_out_cubic_monotonic():
    """f(t) монотонно возрастает на [0, 1]."""
    prev = -1.0
    for i in range(101):
        t = i / 100.0
        v = _ease_out_cubic(t)
        assert v >= prev, f"non-monotonic at t={t}: {prev} → {v}"
        prev = v


def test_ease_out_cubic_decelerates():
    """Скорость в начале > скорости в конце (характерное замедление)."""
    early = _ease_out_cubic(0.2) - _ease_out_cubic(0.0)
    late = _ease_out_cubic(1.0) - _ease_out_cubic(0.8)
    assert early > late, f"ease-out должен замедляться: early={early:.3f}, late={late:.3f}"


# ─────────────────────────────────────────────────────────────────────────────
# inertia_scroll
# ─────────────────────────────────────────────────────────────────────────────


def test_inertia_scroll_total_amount(patch_sleep):
    """Сумма всех scrollBy шагов ≈ amount_px."""
    random.seed(0)
    driver = _FakeDriver()
    inertia_scroll(driver, 500, steps_range=(15, 15))
    assert len(driver.scrolls) == 15
    total = sum(driver.scrolls)
    # eased по cubic ease-out: sum(deltas) = total. Должно быть ≈500.
    assert abs(total - 500.0) < 0.01


def test_inertia_scroll_decelerates(patch_sleep):
    """Первые шаги больше последних (cubic ease-out)."""
    random.seed(0)
    driver = _FakeDriver()
    inertia_scroll(driver, 1000, steps_range=(20, 20))
    # Первая половина шагов должна быть больше второй.
    first_half = sum(driver.scrolls[:10])
    second_half = sum(driver.scrolls[10:])
    assert first_half > second_half, (
        f"ease-out: первая половина {first_half} должна быть > {second_half}"
    )


def test_inertia_scroll_negative_amount(patch_sleep):
    """Отрицательный amount → все scrollBy отрицательные."""
    random.seed(0)
    driver = _FakeDriver()
    inertia_scroll(driver, -300, steps_range=(10, 10))
    assert all(s < 0 for s in driver.scrolls)
    assert abs(sum(driver.scrolls) - (-300.0)) < 0.01


def test_inertia_scroll_zero_amount_noop(patch_sleep):
    driver = _FakeDriver()
    assert inertia_scroll(driver, 0) is True
    assert driver.scrolls == []


def test_inertia_scroll_stop_event_aborts(patch_sleep):
    driver = _FakeDriver()
    ev = threading.Event()
    ev.set()
    # stop_event сработал до первого шага → return True (как «выполнено»),
    # но scrolls остаётся пустым.
    inertia_scroll(driver, 500, stop_event=ev)
    assert driver.scrolls == []


def test_inertia_scroll_webdriver_error_returns_false(patch_sleep):
    driver = _FakeDriver(execute_raises=WebDriverException("boom"))
    assert inertia_scroll(driver, 500) is False


# ─────────────────────────────────────────────────────────────────────────────
# visible_text_chars
# ─────────────────────────────────────────────────────────────────────────────


def test_visible_text_chars_returns_int(patch_sleep):
    driver = _FakeDriver(text_chars=42)
    assert visible_text_chars(driver) == 42


def test_visible_text_chars_none_returns_zero(patch_sleep):
    """Если JS вернул None — отдаём 0, не падаем."""
    driver = _FakeDriver()  # text_chars=0 по умолчанию
    # Подменим execute_script чтобы возвращал None
    original = driver.execute_script

    def fake(script, *args):
        if "innerText" in script:
            return None
        return original(script, *args)

    driver.execute_script = fake  # type: ignore
    assert visible_text_chars(driver) == 0


def test_visible_text_chars_webdriver_error_returns_zero():
    driver = _FakeDriver(execute_raises=WebDriverException("frame detached"))
    assert visible_text_chars(driver) == 0


# ─────────────────────────────────────────────────────────────────────────────
# reading_time_for_chars
# ─────────────────────────────────────────────────────────────────────────────


def test_reading_time_zero_chars():
    lo, hi = reading_time_for_chars(0)
    assert 0.3 <= lo <= 1.5
    assert hi <= 1.5


def test_reading_time_grows_with_chars():
    lo_short, hi_short = reading_time_for_chars(100)
    lo_long, hi_long = reading_time_for_chars(2000)
    assert lo_long > lo_short
    assert hi_long > hi_short


def test_reading_time_clamped_at_30s():
    """Очень длинные тексты упираются в 30s — паттерн «глаз пробежал»."""
    lo, hi = reading_time_for_chars(1_000_000)
    assert lo <= 30.0
    assert hi <= 30.0


def test_reading_time_lo_le_hi():
    """lo ≤ hi для любых разумных входов."""
    for n in [0, 1, 50, 100, 500, 1000, 5000, 10000]:
        lo, hi = reading_time_for_chars(n)
        assert lo <= hi, f"chars={n}: lo={lo} > hi={hi}"


# ─────────────────────────────────────────────────────────────────────────────
# human_scroll (top-level)
# ─────────────────────────────────────────────────────────────────────────────


def test_human_scroll_does_swipes(patch_sleep):
    random.seed(0)
    driver = _FakeDriver()
    ok = scroll_page(driver, swipes=3, back_scroll_chance=0.0, reading_pause=False)
    assert ok is True
    # Каждый свайп = ~10-20 микро-scrollBy.
    assert len(driver.scrolls) >= 30


def test_human_scroll_direction_down_positive(patch_sleep):
    random.seed(0)
    driver = _FakeDriver()
    scroll_page(driver, "down", swipes=2, back_scroll_chance=0.0, reading_pause=False)
    # Все scroll'ы вниз → положительные.
    assert all(s > 0 for s in driver.scrolls)


def test_human_scroll_direction_up_negative(patch_sleep):
    random.seed(0)
    driver = _FakeDriver()
    scroll_page(driver, "up", swipes=2, back_scroll_chance=0.0, reading_pause=False)
    # Все scroll'ы вверх → отрицательные.
    assert all(s < 0 for s in driver.scrolls)


def test_human_scroll_back_scroll_adds_reverse(patch_sleep):
    """С back_scroll_chance=1.0 каждый свайп (кроме первого) даёт реверс."""
    random.seed(0)
    driver = _FakeDriver()
    scroll_page(driver, "down", swipes=3, back_scroll_chance=1.0, reading_pause=False)
    # Должно быть как минимум 2 «отката» (после 1-го и 2-го свайпа).
    negative_groups = sum(1 for s in driver.scrolls if s < 0)
    assert negative_groups >= 10, (
        f"ожидали несколько микро-scroll вверх, получили {negative_groups}"
    )


def test_human_scroll_zero_swipes_returns_false(patch_sleep):
    driver = _FakeDriver()
    assert scroll_page(driver, swipes=0) is False
    assert driver.scrolls == []


def test_human_scroll_stop_event_early_aborts(patch_sleep):
    driver = _FakeDriver()
    ev = threading.Event()
    ev.set()
    # stop_event сработал ДО первого свайпа → False.
    ok = scroll_page(driver, swipes=5, back_scroll_chance=0.0, reading_pause=False, stop_event=ev)
    assert ok is False
    assert driver.scrolls == []


def test_human_scroll_reading_pause_uses_visible_text(patch_sleep, monkeypatch):
    """reading_pause=True → visible_text_chars вызывается per свайп."""
    calls = []

    def fake_visible(driver):
        calls.append(driver)
        return 500

    monkeypatch.setattr(human_scroll, "visible_text_chars", fake_visible)

    random.seed(0)
    driver = _FakeDriver()
    scroll_page(driver, swipes=3, back_scroll_chance=0.0, reading_pause=True)
    assert len(calls) == 3


# ─────────────────────────────────────────────────────────────────────────────
# T10: compute_reading_dwell
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_dwell_baseline():
    """Пустой description, нет фото, средний interest → ~5-15 сек."""
    random.seed(0)
    dwell = compute_reading_dwell(description=None, image_count=0, interest=0.5)
    assert 5.0 <= dwell <= 30.0


def test_compute_dwell_grows_with_text():
    """Дольше описание → больше dwell."""
    random.seed(42)
    short = compute_reading_dwell(description="Маленькое.", image_count=0, interest=0.5)
    random.seed(42)
    long = compute_reading_dwell(
        description="Текст в тысячу символов. " * 50, image_count=0, interest=0.5
    )
    assert long > short, f"short={short:.1f}, long={long:.1f}"


def test_compute_dwell_grows_with_images():
    random.seed(42)
    no_img = compute_reading_dwell(description="Описание объекта.", image_count=0)
    random.seed(42)
    many_img = compute_reading_dwell(description="Описание объекта.", image_count=15)
    assert many_img > no_img


def test_compute_dwell_grows_with_interest():
    random.seed(42)
    low = compute_reading_dwell(
        description="Описание объекта на 100 символов про офис в центре.",
        image_count=10,
        interest=0.0,
    )
    random.seed(42)
    high = compute_reading_dwell(
        description="Описание объекта на 100 символов про офис в центре.",
        image_count=10,
        interest=1.0,
    )
    assert high > low


def test_compute_dwell_clamped_lower():
    """Минимальный dwell ≥ base_min."""
    random.seed(0)
    for _ in range(50):
        dwell = compute_reading_dwell(description=None, image_count=0, interest=0.0, base_min=5.0)
        assert dwell >= 5.0


def test_compute_dwell_clamped_upper():
    """Максимальный dwell ≤ base_max."""
    random.seed(0)
    for _ in range(50):
        dwell = compute_reading_dwell(
            description="x" * 100_000,
            image_count=100,
            interest=1.0,
            base_max=300.0,
        )
        assert dwell <= 300.0


def test_compute_dwell_interest_clamped():
    """interest > 1.0 / < 0 не ломает (clamp внутри)."""
    random.seed(0)
    high = compute_reading_dwell(description="abc", image_count=0, interest=2.5)
    random.seed(0)
    very_high = compute_reading_dwell(description="abc", image_count=0, interest=1.0)
    # interest=2.5 должен дать тот же результат, что interest=1.0.
    assert abs(high - very_high) < 0.01

    random.seed(0)
    low = compute_reading_dwell(description="abc", image_count=0, interest=-0.5)
    random.seed(0)
    very_low = compute_reading_dwell(description="abc", image_count=0, interest=0.0)
    assert abs(low - very_low) < 0.01


def test_compute_dwell_realistic_ranges():
    """Профильные кейсы: типичные значения дают разумные диапазоны."""
    random.seed(0)
    # 1. Быстро закрыли — пустой / неинтересный.
    quick_dwells = [
        compute_reading_dwell(description=None, image_count=0, interest=0.0) for _ in range(50)
    ]
    assert max(quick_dwells) < 30.0  # никогда долго

    # 2. Очень интересный, длинный — могут быть 2+ минуты.
    deep_dwells = [
        compute_reading_dwell(description="x" * 2000, image_count=15, interest=1.0)
        for _ in range(50)
    ]
    # Хотя бы один должен быть > 60 сек.
    assert max(deep_dwells) > 60.0
