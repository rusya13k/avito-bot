"""
T1: regression — три «бабушкиных» места (yandex_warmup ввод запроса, perform_login
ввод телефона, perform_login ввод пароля) больше НЕ должны содержать
inline-loop `for ch in ...: send_keys(ch); time.sleep(uniform(0.5, 1.5))`
(скорость 8-12 WPM = бабушка-палево).

Должны использовать `human_type(...)` (50-250ms/char для warmup,
100-350ms/char для логина).
"""

import ast
import inspect
import re
import textwrap

import bot


def _src(func) -> str:
    return textwrap.dedent(inspect.getsource(func))


def _count_human_type_calls(func) -> int:
    """Считаем количество вызовов human_type(...) в теле функции."""
    tree = ast.parse(_src(func))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name == "human_type":
                count += 1
    return count


# ── T1.1: yandex_warmup ───────────────────────────────────────────────────


def test_yandex_warmup_uses_human_type():
    """T1: yandex_warmup должен вводить query через human_type, не через slow loop."""
    src = _src(bot.yandex_warmup)
    # Должен быть как минимум один вызов human_type для query.
    assert "human_type(" in src, (
        "yandex_warmup больше не использует human_type — регресс к slow-loop?"
    )


def test_yandex_warmup_has_no_grandma_loop():
    """T1: в yandex_warmup нет inline-loop с time.sleep(uniform(0.5..., 1.5...))."""
    src = _src(bot.yandex_warmup)
    # «Бабушкина» сигнатура: time.sleep(random.uniform(<0.5+>, <1.5+>)) с большой минимальной.
    pattern = re.compile(r"time\.sleep\(\s*random\.uniform\(\s*0\.5\s*,\s*1\.5\s*\)\s*\)")
    assert not pattern.search(src), (
        "В yandex_warmup найден slow-typing loop time.sleep(random.uniform(0.5, 1.5)) — "
        "T1 регресс."
    )


# ── T1.2 / T1.3: perform_login (phone + password) ─────────────────────────


def test_perform_login_uses_human_type_twice():
    """T1: perform_login должен использовать human_type 2 раза — для phone и password."""
    n = _count_human_type_calls(bot.perform_login)
    assert n >= 2, (
        f"perform_login должен иметь >=2 вызовов human_type (phone + password), "
        f"найдено {n}. Возможный регресс на slow-loop."
    )


def test_perform_login_has_no_grandma_loop():
    """T1: в perform_login нет inline-loop с time.sleep(uniform(0.5, 1.5)) для phone/password."""
    src = _src(bot.perform_login)
    pattern = re.compile(r"time\.sleep\(\s*random\.uniform\(\s*0\.5\s*,\s*1\.5\s*\)\s*\)")
    assert not pattern.search(src), (
        "В perform_login найден slow-typing loop time.sleep(random.uniform(0.5, 1.5)) — "
        "T1 регресс."
    )


def test_perform_login_typing_speed_in_login_range():
    """T1: human_type в perform_login использует диапазон ~100-350ms (чуть медленнее warmup)."""
    src = _src(bot.perform_login)
    # Должен встретиться хотя бы один speed_range=(0.10, 0.35) или (0.1, 0.35).
    assert re.search(r"speed_range\s*=\s*\(\s*0\.1?0?\s*,\s*0\.35\s*\)", src), (
        "В perform_login human_type ожидается с speed_range=(0.10, 0.35) для логин-инпутов."
    )


# ── T1.4: human_type API не сломан ────────────────────────────────────────


def test_human_type_signature():
    """T1: human_type(element, text, speed_range=...) — публичный контракт."""
    sig = inspect.signature(bot.human_type)
    params = list(sig.parameters.keys())
    assert params[:2] == ["element", "text"], (
        f"human_type должен начинаться с (element, text, ...), получено {params}"
    )
    assert "speed_range" in sig.parameters, "human_type должен принимать speed_range"
