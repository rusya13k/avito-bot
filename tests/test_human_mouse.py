"""
T6: тесты для human_mouse (Bezier + jitter + click).

Полный Selenium не запускаем — используем DummyDriver/DummyElement,
которые имитируют только нужные методы (execute_script + ActionChains
hooks). За счёт DummyActionChains патчим
selenium.webdriver.common.action_chains.ActionChains в фикстуре.
"""

from __future__ import annotations

import math
import random
import threading

import pytest
from selenium.common.exceptions import WebDriverException

import human_mouse
from human_mouse import (
    bezier_path,
    human_click,
    human_move_to,
    reset_last_pos,
)

# ─────────────────────────────────────────────────────────────────────────────
# Dummy Selenium harness
# ─────────────────────────────────────────────────────────────────────────────


class _DummyElement:
    """Минимальный WebElement-stub. Возвращает rect через JS-stub в driver."""

    def __init__(
        self,
        rect: tuple[float, float, float, float] = (200.0, 300.0, 100.0, 40.0),
    ):
        # left, top, width, height
        self.rect = rect
        # Сколько раз вызвали native click().
        self.native_click_calls = 0
        # Если выставить True — native click бросит WebDriverException.
        self.native_click_raises = False

    def click(self):
        self.native_click_calls += 1
        if self.native_click_raises:
            raise WebDriverException("dummy native click failure")


class _DummyPointerAction:
    """Stub для actions.w3c_actions.pointer_action."""

    def __init__(self, recorder: list):
        self._rec = recorder

    def move_to_location(self, x, y):
        self._rec.append(("move", int(x), int(y)))
        return self

    def pause(self, duration):
        self._rec.append(("pause", float(duration)))
        return self

    def click(self):
        self._rec.append(("click",))
        return self


class _DummyW3CActions:
    def __init__(self, recorder: list):
        self.pointer_action = _DummyPointerAction(recorder)


class _DummyActionChains:
    """Stub ActionChains. Запоминает последовательность операций."""

    raise_on_perform = False

    def __init__(self, driver, *args, **kwargs):
        self.driver = driver
        # Каждая ActionChains начинает свою «сессию», но мы хотим увидеть
        # все операции из всех вызовов в одном списке.
        if not hasattr(driver, "_action_log"):
            driver._action_log = []
        self.w3c_actions = _DummyW3CActions(driver._action_log)

    def perform(self):
        if _DummyActionChains.raise_on_perform:
            raise WebDriverException("dummy perform failure")
        # Маркер «сессия завершена» — полезно в тестах считать perform().
        self.driver._action_log.append(("perform",))


class _DummyDriver:
    """Stub WebDriver. execute_script отвечает на 2 паттерна:
    1) getBoundingClientRect → возвращает element.rect
    2) window.innerWidth / innerHeight → константа 1280×720
    Любой третий script — no-op (например, scrollIntoView).
    """

    def __init__(self, *, viewport: tuple[int, int] = (1280, 720)):
        self.viewport = viewport
        self.js_calls: list[str] = []
        self._action_log: list = []
        self.execute_script_raises = False

    def execute_script(self, script, *args):
        self.js_calls.append(script)
        if self.execute_script_raises:
            raise WebDriverException("dummy js failure")
        if "getBoundingClientRect" in script:
            elem = args[0] if args else None
            if elem is None or not hasattr(elem, "rect"):
                return None
            return list(elem.rect)
        if "innerWidth" in script:
            return list(self.viewport)
        if "scrollIntoView" in script:
            return None
        if "click()" in script or "arguments[0].click" in script:
            # JS-fallback click — отметим в логе.
            elem = args[0] if args else None
            if elem is not None and hasattr(elem, "native_click_calls"):
                # Считаем как ещё один click — для проверки fallback.
                elem.native_click_calls += 1
            return None
        return None


@pytest.fixture
def patch_actionchains(monkeypatch):
    """Подменяем ActionChains внутри human_mouse на Dummy."""
    monkeypatch.setattr(human_mouse, "ActionChains", _DummyActionChains)
    _DummyActionChains.raise_on_perform = False
    yield
    _DummyActionChains.raise_on_perform = False


@pytest.fixture
def patch_sleep(monkeypatch):
    """Подменяем time.sleep внутри human_mouse, чтобы тесты были быстрые."""
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(float(s))

    monkeypatch.setattr(human_mouse.time, "sleep", fake_sleep)
    return sleeps


@pytest.fixture(autouse=True)
def clean_last_pos():
    """Перед каждым тестом — чистый _LAST_POS, чтобы не было утечек."""
    human_mouse._LAST_POS.clear()
    yield
    human_mouse._LAST_POS.clear()


# ─────────────────────────────────────────────────────────────────────────────
# bezier_path: математика
# ─────────────────────────────────────────────────────────────────────────────


def test_bezier_path_endpoints_match():
    """points[0] == start, points[-1] == end (с точностью FP)."""
    start = (10.0, 20.0)
    end = (300.0, 400.0)
    points = bezier_path(start, end, num_points=20, curvature=0.3)
    assert math.isclose(points[0][0], start[0])
    assert math.isclose(points[0][1], start[1])
    assert math.isclose(points[-1][0], end[0])
    assert math.isclose(points[-1][1], end[1])


def test_bezier_path_num_points_correct():
    """num_points точно равно длине списка."""
    points = bezier_path((0, 0), (100, 100), num_points=15, curvature=0.2)
    assert len(points) == 15
    points = bezier_path((0, 0), (100, 100), num_points=2, curvature=0.0)
    assert len(points) == 2


def test_bezier_path_min_2_points():
    """num_points < 2 безопасно повышается до 2."""
    points = bezier_path((0, 0), (100, 100), num_points=1, curvature=0.2)
    assert len(points) == 2


def test_bezier_path_zero_curvature_is_straight():
    """curvature=0 → точки лежат на прямой start-end."""
    start = (0.0, 0.0)
    end = (100.0, 100.0)
    points = bezier_path(start, end, num_points=10, curvature=0.0)
    # При curvature=0 control = midpoint. Bezier вырождается в прямую.
    for x, y in points:
        # x == y (диагональ y=x)
        assert math.isclose(x, y, abs_tol=1e-9)


def test_bezier_path_nonzero_curvature_deviates():
    """curvature>0 → точки в середине НЕ лежат на прямой start-end."""
    random.seed(0)  # стабильный offset
    start = (0.0, 0.0)
    end = (100.0, 0.0)  # горизонтальная прямая
    points = bezier_path(start, end, num_points=10, curvature=0.5)
    mid = points[len(points) // 2]
    # Прямая y=0; mid.y должна быть != 0 (не лежит на прямой).
    assert abs(mid[1]) > 5.0, f"Expected curve deviation, got mid.y={mid[1]}"


def test_bezier_path_same_start_end():
    """Граничный случай: start == end → все точки совпадают."""
    points = bezier_path((50.0, 50.0), (50.0, 50.0), num_points=5, curvature=0.5)
    for x, y in points:
        assert math.isclose(x, 50.0)
        assert math.isclose(y, 50.0)


# ─────────────────────────────────────────────────────────────────────────────
# human_move_to: Bezier-путь генерирует mousemove'ы
# ─────────────────────────────────────────────────────────────────────────────


def test_human_move_to_sends_move_events(patch_actionchains):
    """human_move_to генерирует ≥ steps_min mousemove на pointer_action."""
    random.seed(0)
    driver = _DummyDriver()
    elem = _DummyElement(rect=(200.0, 300.0, 100.0, 40.0))

    ok = human_move_to(driver, elem, steps_range=(15, 30), overshoot_chance=0.0)
    assert ok is True

    moves = [op for op in driver._action_log if op[0] == "move"]
    pauses = [op for op in driver._action_log if op[0] == "pause"]
    # Должно быть steps в диапазоне [15, 30] (без overshoot).
    assert 15 <= len(moves) <= 30
    # Каждое движение сопровождается паузой.
    assert len(pauses) == len(moves)


def test_human_move_to_last_point_inside_element(patch_actionchains):
    """Последний move попадает внутрь bounding rect элемента."""
    random.seed(0)
    driver = _DummyDriver()
    rect = (500.0, 200.0, 80.0, 30.0)
    elem = _DummyElement(rect=rect)
    human_move_to(driver, elem, jitter_px=3, overshoot_chance=0.0)

    moves = [op for op in driver._action_log if op[0] == "move"]
    last_x, last_y = moves[-1][1], moves[-1][2]
    left, top, w, h = rect
    # ±2 из-за int() округления и jitter_px=3.
    assert left - 2 <= last_x <= left + w + 2
    assert top - 2 <= last_y <= top + h + 2


def test_human_move_to_no_visible_rect_returns_false(patch_actionchains):
    """Элемент с w=0 или h=0 → False, без mousemove."""
    driver = _DummyDriver()
    elem = _DummyElement(rect=(100.0, 100.0, 0.0, 0.0))
    ok = human_move_to(driver, elem)
    assert ok is False
    moves = [op for op in driver._action_log if op[0] == "move"]
    assert moves == []


def test_human_move_to_remembers_last_pos(patch_actionchains):
    """После успешного move_to позиция запоминается → следующий путь
    стартует с неё (а не со случайной точки в верхней половине)."""
    random.seed(42)
    driver = _DummyDriver()
    elem1 = _DummyElement(rect=(800.0, 600.0, 50.0, 50.0))
    elem2 = _DummyElement(rect=(100.0, 100.0, 50.0, 50.0))

    human_move_to(driver, elem1, overshoot_chance=0.0)
    last_after_first = human_mouse._get_last_pos(driver)
    assert last_after_first is not None
    # Должна быть около центра elem1 (825, 625).
    assert 820 <= last_after_first[0] <= 830
    assert 620 <= last_after_first[1] <= 630

    driver._action_log.clear()
    human_move_to(driver, elem2, overshoot_chance=0.0)
    moves = [op for op in driver._action_log if op[0] == "move"]
    # Первый move стартового пути должен быть ОЧЕНЬ близко к last_after_first
    # (это первая точка Bezier, которая = start). int() округляет до пикселя.
    first_move_x, first_move_y = moves[0][1], moves[0][2]
    assert abs(first_move_x - last_after_first[0]) <= 1
    assert abs(first_move_y - last_after_first[1]) <= 1


def test_human_move_to_overshoot_adds_extra_points(patch_actionchains):
    """С overshoot_chance=1.0 путь длиннее, чем без."""
    random.seed(1)
    driver = _DummyDriver()
    elem = _DummyElement()
    human_move_to(driver, elem, steps_range=(20, 20), overshoot_chance=0.0)
    base_moves = len([op for op in driver._action_log if op[0] == "move"])

    driver._action_log.clear()
    random.seed(1)  # тот же seed → тот же base, но overshoot включится
    human_move_to(driver, elem, steps_range=(20, 20), overshoot_chance=1.0)
    overshoot_moves = len([op for op in driver._action_log if op[0] == "move"])

    # Должно быть строго больше — overshoot добавляет ≥ 4 точек.
    assert overshoot_moves > base_moves
    assert overshoot_moves >= base_moves + 3


def test_human_move_to_stop_event_aborts(patch_actionchains):
    """stop_event сработавший в середине → прерываемся, возвращаем False."""
    driver = _DummyDriver()
    elem = _DummyElement()
    ev = threading.Event()
    ev.set()
    ok = human_move_to(driver, elem, stop_event=ev, overshoot_chance=0.0)
    assert ok is False


def test_human_move_to_webdriverexception_returns_false(patch_actionchains):
    """Если perform() кинет WebDriverException — False, не падаем наружу."""
    _DummyActionChains.raise_on_perform = True
    driver = _DummyDriver()
    elem = _DummyElement()
    ok = human_move_to(driver, elem, overshoot_chance=0.0)
    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# human_click: успех + fallback'и
# ─────────────────────────────────────────────────────────────────────────────


def test_human_click_full_path(patch_actionchains, patch_sleep):
    """Happy path: scroll → move → click через ActionChains."""
    random.seed(0)
    driver = _DummyDriver()
    elem = _DummyElement()
    ok = human_click(driver, elem, hesitate_chance=0.0)
    assert ok is True

    # 1. scrollIntoView был вызван.
    assert any("scrollIntoView" in s for s in driver.js_calls)
    # 2. mousemove'ы были.
    moves = [op for op in driver._action_log if op[0] == "move"]
    assert len(moves) >= 15
    # 3. Был клик через pointer_action.click().
    clicks = [op for op in driver._action_log if op[0] == "click"]
    assert len(clicks) == 1
    # 4. native click() НЕ дёргался — мы кликнули через actions.
    assert elem.native_click_calls == 0


def test_human_click_no_scroll_if_disabled(patch_actionchains, patch_sleep):
    driver = _DummyDriver()
    elem = _DummyElement()
    human_click(driver, elem, scroll_into_view=False, hesitate_chance=0.0)
    assert not any("scrollIntoView" in s for s in driver.js_calls)


def test_human_click_falls_back_to_native_on_action_failure(patch_actionchains, patch_sleep):
    """Если ActionChains.perform() кинет — пробуем element.click()."""
    _DummyActionChains.raise_on_perform = True
    driver = _DummyDriver()
    elem = _DummyElement()
    ok = human_click(driver, elem, hesitate_chance=0.0)
    assert ok is True
    # Native click был дёрнут как fallback.
    assert elem.native_click_calls == 1


def test_human_click_falls_back_to_js_on_native_failure(patch_actionchains, patch_sleep):
    """ActionChains падает + native click тоже → JS fallback."""
    _DummyActionChains.raise_on_perform = True
    driver = _DummyDriver()
    elem = _DummyElement()
    elem.native_click_raises = True

    ok = human_click(driver, elem, hesitate_chance=0.0)
    assert ok is True
    # native click пытался + JS click сработал.
    assert elem.native_click_calls >= 1
    # JS-click увеличивает native_click_calls тоже (наша stub-реализация
    # это делает) — поэтому проверяем что был хотя бы 1 JS-вызов:
    assert any("arguments[0].click" in s for s in driver.js_calls)


def test_human_click_returns_false_when_all_fail(patch_actionchains, patch_sleep):
    """ActionChains + native + JS всё падает — возвращаем False."""
    _DummyActionChains.raise_on_perform = True
    driver = _DummyDriver()
    elem = _DummyElement()
    elem.native_click_raises = True

    # Подменяем JS click на raise.
    original_execute = driver.execute_script

    def js_raises(script, *args):
        if "arguments[0].click" in script:
            raise WebDriverException("dummy js click failure")
        return original_execute(script, *args)

    driver.execute_script = js_raises  # type: ignore

    ok = human_click(driver, elem, hesitate_chance=0.0)
    assert ok is False


def test_human_click_stop_event_after_move_aborts(patch_actionchains, patch_sleep, monkeypatch):
    """stop_event срабатывает между move и click → False, без клика."""
    driver = _DummyDriver()
    elem = _DummyElement()
    ev = threading.Event()

    # Подменяем human_move_to: возвращает True (как будто доехал) и
    # выставляет event ровно перед выходом — имитирует /stop, прилетевший
    # пока курсор ехал.
    def fake_move(driver, element, **kwargs):
        ev.set()
        return True

    monkeypatch.setattr(human_mouse, "human_move_to", fake_move)

    ok = human_click(driver, elem, hesitate_chance=0.0, stop_event=ev)
    assert ok is False
    # Клика быть не должно.
    clicks = [op for op in driver._action_log if op[0] == "click"]
    assert clicks == []
    # Native click тоже не дёргали.
    assert elem.native_click_calls == 0


def test_human_click_stop_event_before_move_aborts(patch_actionchains, patch_sleep):
    """stop_event уже set до фазы move → False сразу."""
    driver = _DummyDriver()
    elem = _DummyElement()
    ev = threading.Event()
    ev.set()
    ok = human_click(driver, elem, hesitate_chance=0.0, stop_event=ev)
    assert ok is False
    assert elem.native_click_calls == 0


def test_human_click_hesitate_adds_wiggle(patch_actionchains, patch_sleep):
    """С hesitate_chance=1.0 в логе появляется доп. mousemove «в сторону»."""
    random.seed(0)
    driver = _DummyDriver()
    elem = _DummyElement()
    ok = human_click(driver, elem, hesitate_chance=1.0)
    assert ok is True

    # Перформ должен быть вызван минимум 2 раза (move + hesitate).
    performs = [op for op in driver._action_log if op[0] == "perform"]
    assert len(performs) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# Reset helper
# ─────────────────────────────────────────────────────────────────────────────


def test_reset_last_pos_clears_state(patch_actionchains):
    driver = _DummyDriver()
    elem = _DummyElement()
    human_move_to(driver, elem, overshoot_chance=0.0)
    assert human_mouse._get_last_pos(driver) is not None
    reset_last_pos(driver)
    assert human_mouse._get_last_pos(driver) is None


# ─────────────────────────────────────────────────────────────────────────────
# Параметризованные edge-cases
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("steps_min,steps_max", [(5, 5), (15, 30), (40, 50)])
def test_human_move_to_steps_range_respected(patch_actionchains, steps_min, steps_max):
    random.seed(0)
    driver = _DummyDriver()
    elem = _DummyElement()
    human_move_to(
        driver,
        elem,
        steps_range=(steps_min, steps_max),
        overshoot_chance=0.0,
    )
    moves = [op for op in driver._action_log if op[0] == "move"]
    assert steps_min <= len(moves) <= steps_max


@pytest.mark.parametrize("jitter_px", [0, 2, 5, 10])
def test_human_move_to_jitter_keeps_target_in_rect(patch_actionchains, jitter_px):
    """При любом jitter_px последняя точка остаётся внутри rect."""
    random.seed(0)
    driver = _DummyDriver()
    rect = (300.0, 200.0, 60.0, 30.0)
    elem = _DummyElement(rect=rect)
    human_move_to(driver, elem, jitter_px=jitter_px, overshoot_chance=0.0)

    moves = [op for op in driver._action_log if op[0] == "move"]
    last = moves[-1]
    left, top, w, h = rect
    # +/- 2 из-за int() и safety-margin внутри _element_viewport_target.
    assert left - 2 <= last[1] <= left + w + 2
    assert top - 2 <= last[2] <= top + h + 2
