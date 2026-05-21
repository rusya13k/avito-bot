"""
T6: Human-like mouse movements (Bezier-траектория + jitter + клик).

Раньше клики были двух видов:

  1. ``element.click()``        — Selenium посылает MouseEvent без mousemove
                                  событий → курсор «телепортируется».
  2. ``execute_script("arguments[0].click();", el)``
                                — JS-клик, вообще без event'ов мыши.

Anti-фрод детектит «click без предшествующих mousemove» как сильный сигнал
бота (см. T6 в zadachi_zhivuchest.md). Реальный пользователь:

  • ведёт курсор к цели по плавной кривой с 15-30 промежуточными mousemove,
  • часто jitter ±1-3 px вокруг центра элемента,
  • иногда «передумывает» — наводит → отводит → возвращается,
  • иногда «промахивается» (overshoot) и корректируется.

Этот модуль реализует всё перечисленное через Selenium 4 W3C-pointer API:

    actions.w3c_actions.pointer_action.move_to_location(x, y)

принимает абсолютные viewport-координаты и порождает настоящие mousemove
события в браузере.

Public API:

    human_move_to(driver, element, *, jitter_px=3, steps_range=(15, 30),
                  overshoot_chance=0.10, stop_event=None) -> bool

    human_click(driver, element, *, hesitate_chance=0.10,
                stop_event=None) -> bool

    human_click(...) НИКОГДА не raise: на любой ошибке падает в fallback
    (selenium .click() → execute_script-click). Это критично для legacy
    мест, где раньше была одна строка `.click()`.

Тестируется без реального драйвера через `_DummyDriver` (см.
tests/test_human_mouse.py).
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Any

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains

logger = logging.getLogger(__name__)

# Последняя известная позиция курсора per driver (id(driver) → (x, y)).
# Selenium внутри тоже трекает позицию pointer'а, но мы не имеем к ней
# программного доступа, поэтому держим свою копию для построения Bezier.
_LAST_POS: dict[int, tuple[float, float]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Bezier helpers
# ─────────────────────────────────────────────────────────────────────────────


def _quadratic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    """Точка квадратичной Bezier-кривой при параметре t∈[0, 1]."""
    one_minus_t = 1.0 - t
    x = one_minus_t * one_minus_t * p0[0] + 2 * one_minus_t * t * p1[0] + t * t * p2[0]
    y = one_minus_t * one_minus_t * p0[1] + 2 * one_minus_t * t * p1[1] + t * t * p2[1]
    return (x, y)


def bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    num_points: int = 20,
    curvature: float = 0.20,
) -> list[tuple[float, float]]:
    """Сгенерировать список точек по квадратичной Bezier-кривой start → end.

    Args:
        start, end: координаты в пикселях.
        num_points: количество точек, ВКЛЮЧАЯ start и end. Минимум 2.
        curvature: 0..1 — насколько сильно кривая «выгибается» в сторону
            от прямой start→end. 0 = прямая, 0.20 = умеренная дуга,
            0.5+ = заметный изгиб. Direction (вверх/вниз) — случайный.

    Returns:
        Список из num_points точек. points[0] == start, points[-1] == end.
    """
    if num_points < 2:
        num_points = 2

    mid_x = (start[0] + end[0]) / 2.0
    mid_y = (start[1] + end[1]) / 2.0
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy) or 1.0
    # Перпендикуляр к start→end единичной длины.
    perp_x = -dy / dist
    perp_y = dx / dist
    # Случайное смещение control point'а в сторону перпендикуляра.
    # Отрицательное / положительное — кривая выгибается с разных сторон.
    offset = random.uniform(-curvature, curvature) * dist
    control = (mid_x + perp_x * offset, mid_y + perp_y * offset)

    return [_quadratic_bezier(start, control, end, i / (num_points - 1)) for i in range(num_points)]


# ─────────────────────────────────────────────────────────────────────────────
# Driver helpers — viewport rect, last position
# ─────────────────────────────────────────────────────────────────────────────


def _element_viewport_target(
    driver: Any, element: Any, *, jitter_px: int
) -> tuple[float, float] | None:
    """Получить целевые viewport-координаты для клика по элементу.

    Возвращает (x, y) — точка внутри bounding rect элемента, со случайным
    jitter ±jitter_px вокруг центра. Jitter обрезается так, чтобы точка
    гарантированно осталась внутри rect (с запасом в 2px).

    None если элемент не имеет видимых размеров или getBoundingClientRect
    упал.
    """
    try:
        rect = driver.execute_script(
            "var r = arguments[0].getBoundingClientRect();"
            "return [r.left, r.top, r.width, r.height];",
            element,
        )
    except WebDriverException:
        return None
    if not rect or rect[2] <= 0 or rect[3] <= 0:
        return None

    left, top, w, h = rect
    # Не позволяем jitter'у уйти за пределы rect (минус запас 2px).
    max_jx = max(0.0, w / 2.0 - 2.0)
    max_jy = max(0.0, h / 2.0 - 2.0)
    jx = min(float(jitter_px), max_jx)
    jy = min(float(jitter_px), max_jy)
    cx = left + w / 2.0 + random.uniform(-jx, jx)
    cy = top + h / 2.0 + random.uniform(-jy, jy)
    return (cx, cy)


def _viewport_size(driver: Any) -> tuple[int, int]:
    """Размер viewport. Falls back на 1280×720 если что-то пошло не так."""
    try:
        size = driver.execute_script("return [window.innerWidth || 0, window.innerHeight || 0];")
        if size and size[0] > 0 and size[1] > 0:
            return (int(size[0]), int(size[1]))
    except WebDriverException:
        pass
    return (1280, 720)


def _get_last_pos(driver: Any) -> tuple[float, float] | None:
    return _LAST_POS.get(id(driver))


def _set_last_pos(driver: Any, pos: tuple[float, float]) -> None:
    _LAST_POS[id(driver)] = pos


def _pick_start_pos(driver: Any) -> tuple[float, float]:
    """Подобрать «разумную» стартовую точку, если последняя позиция неизвестна.

    Реальный курсор после переходов на новую страницу обычно остаётся там
    же, где был. Но Selenium pointer position сбрасывается между driver.get(),
    и отследить это снаружи нельзя. Поэтому при первом клике после
    навигации стартуем со случайной точки в верхней половине viewport
    (там обычно сидит курсор после загрузки).
    """
    last = _get_last_pos(driver)
    if last is not None:
        return last

    width, height = _viewport_size(driver)
    sx = random.uniform(width * 0.10, width * 0.60)
    sy = random.uniform(height * 0.10, height * 0.50)
    return (sx, sy)


def reset_last_pos(driver: Any) -> None:
    """Забыть позицию курсора для драйвера. Вызывать после driver.get()
    (на самом деле не критично — всё равно следующий клик угадает старт)."""
    _LAST_POS.pop(id(driver), None)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def human_move_to(
    driver: Any,
    element: Any,
    *,
    steps_range: tuple[int, int] = (15, 30),
    curvature: float = 0.20,
    jitter_px: int = 3,
    pause_range: tuple[float, float] = (0.005, 0.020),
    overshoot_chance: float = 0.10,
    stop_event: threading.Event | None = None,
) -> bool:
    """T6: курсор движется к элементу по Bezier через 15-30 mousemove.

    Args:
        driver: Selenium WebDriver.
        element: целевой WebElement.
        steps_range: (min, max) количество промежуточных точек кривой
            (включая start и end). Default 15-30 — реалистично для
            движения 200-800px.
        curvature: 0..1 — кривизна траектории (см. bezier_path).
        jitter_px: ±N px разброс целевой точки вокруг центра элемента.
        pause_range: (min, max) сек паузы между mousemove. Default
            5-20ms — обычная частота mousemove от руки.
        overshoot_chance: вероятность «промаха» — курсор уходит на
            8-20px дальше цели и возвращается (мини-correction).
        stop_event: если задан и сработал — прерываемся.

    Returns:
        True — курсор успешно доехал. False — элемент не имеет
        видимых размеров / прервано stop_event / WebDriverException
        (детали в logger.debug).
    """
    target = _element_viewport_target(driver, element, jitter_px=jitter_px)
    if target is None:
        return False

    start = _pick_start_pos(driver)
    steps_min, steps_max = steps_range
    if steps_max < steps_min:
        steps_max = steps_min
    num_points = random.randint(max(2, steps_min), max(2, steps_max))
    points = bezier_path(start, target, num_points=num_points, curvature=curvature)

    # T6: иногда промахиваемся и корректируемся.
    if overshoot_chance > 0 and random.random() < overshoot_chance:
        dx = target[0] - start[0]
        dy = target[1] - start[1]
        d = math.hypot(dx, dy) or 1.0
        over = random.uniform(8.0, 20.0)
        overshoot = (target[0] + dx / d * over, target[1] + dy / d * over)
        # Микро-возврат: добавляем 2-3 точки overshoot → target.
        points.append(overshoot)
        # Небольшая дуга обратно (без curvature — короткое расстояние).
        return_path = bezier_path(overshoot, target, num_points=4, curvature=0.05)
        # bezier_path начинается с overshoot (уже добавлен), пропускаем [0].
        points.extend(return_path[1:])

    pause_lo, pause_hi = pause_range
    try:
        actions = ActionChains(driver)
        pa = actions.w3c_actions.pointer_action
        for px, py in points:
            if stop_event is not None and stop_event.is_set():
                return False
            pa.move_to_location(int(px), int(py))
            pa.pause(random.uniform(pause_lo, pause_hi))
        actions.perform()
        _set_last_pos(driver, points[-1])
        return True
    except WebDriverException as exc:
        logger.debug("human_move_to: WebDriverException — %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 — на всякий случай не падаем
        logger.debug("human_move_to: unexpected — %s", exc)
        return False


def human_click(
    driver: Any,
    element: Any,
    *,
    scroll_into_view: bool = True,
    hesitate_chance: float = 0.10,
    pre_click_pause: tuple[float, float] = (0.06, 0.18),
    post_click_pause: tuple[float, float] = (0.10, 0.30),
    stop_event: threading.Event | None = None,
) -> bool:
    """T6: «человеческий» клик — Bezier-движение + hover + click.

    Никогда не raise: на любой ошибке делает fallback в порядке
    1) selenium element.click(), 2) execute_script JS-click. Возвращает
    True если хоть какой-то из путей сработал, False — если все упали.

    Args:
        driver: WebDriver.
        element: WebElement.
        scroll_into_view: True (default) → перед движением скроллим
            элемент в центр viewport (часто нужно из-за sticky-header
            и т.п.).
        hesitate_chance: вероятность мини-«передумывания» — курсор
            наводится → отводится в случайную сторону (10-30px) →
            возвращается. Имитирует «прицеливание».
        pre_click_pause: (lo, hi) сек паузы между приездом курсора
            и собственно кликом (hover delay).
        post_click_pause: (lo, hi) сек паузы после клика, перед
            возвратом из функции.
        stop_event: прерывает движение между фазами.

    Returns:
        True если клик произошёл (любым из 3 способов), False иначе.
    """
    if scroll_into_view:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                element,
            )
            # Микро-пауза, чтобы scroll успел отрендериться.
            time.sleep(random.uniform(0.15, 0.35))
        except WebDriverException:
            # Не критично — продолжаем, click может всё равно сработать.
            pass

    if stop_event is not None and stop_event.is_set():
        return False

    moved = human_move_to(driver, element, stop_event=stop_event)

    # T6: «прицеливание» — иногда дёрнулись в сторону и вернулись.
    if moved and hesitate_chance > 0 and random.random() < hesitate_chance:
        try:
            actions = ActionChains(driver)
            pa = actions.w3c_actions.pointer_action
            last = _get_last_pos(driver) or (0.0, 0.0)
            jitter_x = random.randint(-30, 30)
            jitter_y = random.randint(-20, 20)
            wiggle = (last[0] + jitter_x, last[1] + jitter_y)
            pa.move_to_location(int(wiggle[0]), int(wiggle[1]))
            pa.pause(random.uniform(0.15, 0.40))
            # Возвращаемся к цели (jitter уже сидит в last, поэтому
            # просто перемещаемся обратно к target через Bezier).
            target = _element_viewport_target(driver, element, jitter_px=2)
            if target is not None:
                pa.move_to_location(int(target[0]), int(target[1]))
                pa.pause(random.uniform(0.05, 0.15))
                _set_last_pos(driver, target)
            actions.perform()
        except WebDriverException as exc:
            logger.debug("human_click: hesitate failed — %s", exc)

    # Pre-click hover delay.
    if moved:
        time.sleep(random.uniform(*pre_click_pause))

    if stop_event is not None and stop_event.is_set():
        return False

    # Сам клик. Если move_to доехал — кликаем через ActionChains
    # (это пошлёт mousedown/mouseup в текущей позиции pointer'а).
    if moved:
        try:
            actions = ActionChains(driver)
            actions.w3c_actions.pointer_action.click()
            actions.perform()
            time.sleep(random.uniform(*post_click_pause))
            return True
        except WebDriverException as exc:
            logger.debug("human_click: ActionChains click failed — %s", exc)

    # Fallback 1: selenium native click (он умеет scrollIntoView сам).
    try:
        element.click()
        time.sleep(random.uniform(*post_click_pause))
        return True
    except WebDriverException as exc:
        logger.debug("human_click: native click failed — %s", exc)

    # Fallback 2: JS click — последняя надежда.
    try:
        driver.execute_script("arguments[0].click();", element)
        time.sleep(random.uniform(*post_click_pause))
        return True
    except WebDriverException as exc:
        logger.debug("human_click: JS click failed — %s", exc)
        return False
