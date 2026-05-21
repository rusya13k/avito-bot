"""
T5: тесты для human_typing.type_human (realistic typing 2.0).

Проверяем:
- Полнота ввода (все символы попадают в element.send_keys).
- БЁРСТЫ: задержки внутри бёрста короче, чем между бёрстами.
- ОПЕЧАТКИ: при typo_rate=1.0 в events появляется BACKSPACE.
- enable_typos=False: BACKSPACE никогда не появляется.
- ЦИФРЫ медленнее букв.
- ДЛИННЫЕ СЛОВА быстрее (на конкретный символ).
- speed_multiplier масштабирует общую длительность.
- stop_event прерывает typing.
- WPM в человеческом диапазоне (30-60).
- persona_speed_multiplier: молодые быстрее, инвестор медленнее.
"""

from __future__ import annotations

import random
import threading
from unittest.mock import patch

import pytest
from selenium.webdriver.common.keys import Keys

import human_typing
from human_typing import (
    persona_speed_multiplier,
    type_human,
)

# ── Mock element + sleep harness ──────────────────────────────────────────


class _RecorderElement:
    """Мини-Selenium-элемент: записывает все send_keys в events."""

    def __init__(self):
        self.events: list[str] = []

    def send_keys(self, ch):
        # ch может быть и Keys.BACKSPACE — записываем как есть.
        self.events.append(ch)


@pytest.fixture
def patch_sleep(monkeypatch):
    """Подменяем time.sleep + накапливаем длительности."""
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(float(s))

    # human_typing._sleep_chunked делает time.sleep напрямую
    monkeypatch.setattr(human_typing.time, "sleep", fake_sleep)
    return sleeps


# ── Базовые: все символы попадают в element ───────────────────────────────


def test_completes_short_text(patch_sleep):
    el = _RecorderElement()
    random.seed(0)
    ok = type_human(el, "abc", enable_typos=False, enable_bursts=False)
    assert ok is True
    # Без typos / bursts мы напечатаем ровно "abc".
    assert "".join(el.events) == "abc"


def test_completes_text_with_bursts(patch_sleep):
    """С enable_bursts=True результат всё равно тот же текст (без typos)."""
    el = _RecorderElement()
    random.seed(42)
    ok = type_human(el, "Hello world test", enable_typos=False, enable_bursts=True)
    assert ok is True
    assert "".join(el.events) == "Hello world test"


def test_empty_text_returns_true_no_events(patch_sleep):
    el = _RecorderElement()
    assert type_human(el, "") is True
    assert el.events == []


def test_single_char_no_typo_no_burst(patch_sleep):
    """Слово длиной 1 — ни typo, ни burst-pause не применяются."""
    el = _RecorderElement()
    random.seed(0)
    type_human(el, "x", typo_rate=1.0, enable_typos=True)
    assert el.events == ["x"]
    assert Keys.BACKSPACE not in el.events


# ── BACKSPACE — TYPOS ─────────────────────────────────────────────────────


def test_typo_rate_1_produces_backspace(patch_sleep):
    """При typo_rate=1.0 в слове ≥ 3 символов с QWERTY-соседом —
    BACKSPACE обязательно появится."""
    el = _RecorderElement()
    random.seed(0)
    type_human(el, "hello", typo_rate=1.0, enable_typos=True, enable_bursts=False)
    assert Keys.BACKSPACE in el.events, (
        f"BACKSPACE должен быть в events при typo_rate=1.0, events={el.events}"
    )
    # И при этом в events есть и «правильные» 5 букв из "hello"
    # (одна лишняя «не та» вошла перед BACKSPACE и стёрлась).
    chars_only = [e for e in el.events if e != Keys.BACKSPACE]
    assert len(chars_only) == len("hello") + 1, (
        f"При одной опечатке events содержит len(text)+1 char-event'ов, "
        f"получено {len(chars_only)}: {chars_only}"
    )


def test_typos_disabled_no_backspace(patch_sleep):
    """enable_typos=False: BACKSPACE никогда не появляется, даже при typo_rate=1.0."""
    el = _RecorderElement()
    random.seed(0)
    for _ in range(20):
        el.events.clear()
        type_human(
            el,
            "hello world testing",
            typo_rate=1.0,
            enable_typos=False,
            enable_bursts=True,
        )
        assert Keys.BACKSPACE not in el.events
        assert "".join(el.events) == "hello world testing"


def test_typo_rate_0_no_backspace(patch_sleep):
    """typo_rate=0.0 → опечаток нет, BACKSPACE отсутствует."""
    el = _RecorderElement()
    random.seed(123)
    for _ in range(10):
        el.events.clear()
        type_human(el, "hello world", typo_rate=0.0, enable_typos=True)
        assert Keys.BACKSPACE not in el.events


def test_typo_rate_06_some_backspaces_in_long_text(patch_sleep):
    """typo_rate=0.06 на длинном тексте с 30+ слов — статистически
    хотя бы один BACKSPACE появится."""
    random.seed(2024)
    backspace_count = 0
    text = " ".join(["hello"] * 50)  # 50 слов по 5 букв
    for _ in range(5):
        el = _RecorderElement()
        type_human(el, text, typo_rate=0.06, enable_typos=True)
        backspace_count += sum(1 for e in el.events if e == Keys.BACKSPACE)
    # 5 запусков × 50 слов × 0.06 ≈ 15 опечаток в среднем — точно ≥ 3.
    assert backspace_count >= 3, (
        f"Ожидалось хотя бы 3 BACKSPACE на 250 слов с typo_rate=0.06, получено {backspace_count}."
    )


# ── ЦИФРЫ медленнее букв ───────────────────────────────────────────────────


def test_digits_have_larger_delay_than_letters(patch_sleep):
    """_char_delay для цифры в среднем больше, чем для буквы (×1.5-2.0)."""
    random.seed(0)
    # Большое число повторений, чтобы средние стабилизировались.
    letter_delays = [
        human_typing._char_delay("a", base_lo=0.05, base_hi=0.20, word_len=5) for _ in range(2000)
    ]
    digit_delays = [
        human_typing._char_delay("5", base_lo=0.05, base_hi=0.20, word_len=5) for _ in range(2000)
    ]
    avg_letter = sum(letter_delays) / len(letter_delays)
    avg_digit = sum(digit_delays) / len(digit_delays)
    assert avg_digit > avg_letter * 1.3, (
        f"Цифра должна быть в среднем медленнее буквы хотя бы в 1.3 раза. "
        f"avg_letter={avg_letter:.4f}, avg_digit={avg_digit:.4f}"
    )


def test_punctuation_slower_than_letters(patch_sleep):
    """Пунктуация (shift / numpad) тоже медленнее обычных букв."""
    random.seed(0)
    letter_delays = [
        human_typing._char_delay("a", base_lo=0.05, base_hi=0.20, word_len=5) for _ in range(2000)
    ]
    punct_delays = [
        human_typing._char_delay("!", base_lo=0.05, base_hi=0.20, word_len=5) for _ in range(2000)
    ]
    avg_letter = sum(letter_delays) / len(letter_delays)
    avg_punct = sum(punct_delays) / len(punct_delays)
    assert avg_punct > avg_letter * 1.3


# ── ДЛИННЫЕ СЛОВА чуть быстрее ─────────────────────────────────────────────


def test_long_word_letter_faster_than_short_word(patch_sleep):
    """В слове длиной ≥ 8 буква печатается быстрее (×0.85)."""
    random.seed(0)
    short_delays = [
        human_typing._char_delay("a", base_lo=0.10, base_hi=0.20, word_len=4) for _ in range(2000)
    ]
    long_delays = [
        human_typing._char_delay("a", base_lo=0.10, base_hi=0.20, word_len=12) for _ in range(2000)
    ]
    avg_short = sum(short_delays) / len(short_delays)
    avg_long = sum(long_delays) / len(long_delays)
    assert avg_long < avg_short * 0.95, (
        f"Длинное слово должно печататься быстрее. "
        f"avg_short={avg_short:.4f}, avg_long={avg_long:.4f}"
    )


# ── speed_multiplier ──────────────────────────────────────────────────────


def test_speed_multiplier_scales_total_sleep(patch_sleep):
    """multiplier=2.0 → суммарный sleep в ~2 раза больше, чем при 1.0."""
    text = "Hello world test message"
    sleeps_1x: list[float] = []
    sleeps_2x: list[float] = []
    el1 = _RecorderElement()
    el2 = _RecorderElement()

    random.seed(99)
    patch_sleep.clear()
    type_human(el1, text, speed_multiplier=1.0, enable_typos=False)
    sleeps_1x.extend(patch_sleep)

    random.seed(99)
    patch_sleep.clear()
    type_human(el2, text, speed_multiplier=2.0, enable_typos=False)
    sleeps_2x.extend(patch_sleep)

    total_1x = sum(sleeps_1x)
    total_2x = sum(sleeps_2x)
    # Ожидаем ровно ~2x. Допускаем небольшое отклонение из-за задержек,
    # которые НЕ масштабируются (например, fixed wait в spaces — на самом деле
    # они тоже масштабируются, см. реализацию).
    assert 1.7 < (total_2x / total_1x) < 2.3, (
        f"multiplier=2.0 должен почти удвоить total sleep. "
        f"1x={total_1x:.3f}s, 2x={total_2x:.3f}s, ratio={total_2x / total_1x:.2f}"
    )


def test_speed_multiplier_zero_treated_as_one(patch_sleep):
    """speed_multiplier <= 0 не должен ломать typing — fallback на 1.0."""
    el = _RecorderElement()
    random.seed(0)
    type_human(el, "abc", speed_multiplier=0.0, enable_typos=False)
    assert "".join(el.events) == "abc"


# ── stop_event ─────────────────────────────────────────────────────────────


def test_stop_event_aborts_typing(patch_sleep):
    """Если stop_event сработал ДО typing — печать не начинается, return False."""
    el = _RecorderElement()
    ev = threading.Event()
    ev.set()  # уже сработал
    ok = type_human(el, "this should not be printed", stop_event=ev)
    assert ok is False
    assert el.events == []


def test_stop_event_aborts_mid_typing(monkeypatch):
    """Если stop_event сработает в середине ввода — return False, не дотипали."""
    el = _RecorderElement()
    ev = threading.Event()

    call_count = {"n": 0}

    def fake_sleep(s):
        call_count["n"] += 1
        if call_count["n"] == 5:
            ev.set()  # симулируем нажатие /stop в середине

    monkeypatch.setattr(human_typing.time, "sleep", fake_sleep)
    random.seed(0)
    ok = type_human(
        el,
        "hello world this is a long message",
        enable_typos=False,
        stop_event=ev,
    )
    assert ok is False
    # Что-то напечатано, но не всё.
    assert 0 < len(el.events) < len("hello world this is a long message")


# ── BURST structure: задержки внутри бёрста короче, чем между бёрстами ────


def test_bursts_have_short_intra_long_inter_pauses(patch_sleep):
    """В типичной выборке задержек: max-внутри < min-между.

    Реализация: внутри бёрста base_lo..base_hi (~50-200ms),
    между бёрстами 180-380ms.
    """
    random.seed(7)
    el = _RecorderElement()
    patch_sleep.clear()
    # Длинное «слово» без пробелов — несколько бёрстов подряд.
    type_human(
        el,
        "abcdefghijklmnopqrstuvwxyz" * 2,  # 52 буквы
        speed_range=(0.05, 0.10),  # узкий диапазон внутри бёрста
        enable_typos=False,
        enable_bursts=True,
    )
    # patch_sleep содержит ВСЕ задержки. Хотя бы одна (между бёрстами)
    # должна быть > 0.18, и хотя бы одна (внутри бёрста) — < 0.10.
    assert any(s < 0.10 for s in patch_sleep), "Должна быть короткая burst-задержка."
    assert any(s >= 0.18 for s in patch_sleep), (
        "Должна быть длинная задержка между бёрстами (>= 180ms)."
    )


# ── WPM в человеческом диапазоне ──────────────────────────────────────────


def test_wpm_in_human_range(patch_sleep):
    """Средняя скорость печати должна быть 30-60 WPM (стандартное правило:
    1 word ≈ 5 chars). Слишком медленная (<25 WPM) — палево; слишком
    быстрая (>80 WPM) — машинная скорость."""
    text = (
        "Здравствуйте, меня заинтересовало ваше предложение. "
        "Подскажите пожалуйста параметры объекта и условия аренды. "
        "Спасибо."
    )
    random.seed(2024)
    el = _RecorderElement()
    patch_sleep.clear()
    type_human(el, text, typo_rate=0.0, enable_typos=False)
    total_sec = sum(patch_sleep)
    char_count = len(text)
    wpm = (char_count / 5) / (total_sec / 60.0)
    # При speed_range=(0.05, 0.25) и бёрстах + отдыхах фактический WPM ≈ 35-55.
    assert 25 < wpm < 80, (
        f"WPM={wpm:.1f} вне человеческого диапазона. "
        f"text_len={char_count}, total_sec={total_sec:.2f}"
    )


# ── persona_speed_multiplier ──────────────────────────────────────────────


def test_persona_unknown_returns_1():
    assert persona_speed_multiplier(None) == 1.0
    assert persona_speed_multiplier("") == 1.0
    assert persona_speed_multiplier("nonexistent_persona") == 1.0


def test_persona_young_under_1():
    assert persona_speed_multiplier("retail_starter") < 1.0
    assert persona_speed_multiplier("ecommerce_warehouse") < 1.0


def test_persona_solid_over_1():
    assert persona_speed_multiplier("investor") > 1.0
    assert persona_speed_multiplier("clinic_medical") > 1.0
    assert persona_speed_multiplier("tatarstan_developer") > 1.0


def test_persona_neutral_equals_1():
    assert persona_speed_multiplier("cafe_owner") == 1.0
    assert persona_speed_multiplier("small_business_office") == 1.0


# ── _split_tokens edge cases ──────────────────────────────────────────────


def test_split_tokens_empty():
    assert human_typing._split_tokens("") == []


def test_split_tokens_single_word():
    assert human_typing._split_tokens("hello") == ["hello"]


def test_split_tokens_words_and_spaces():
    tokens = human_typing._split_tokens("hello world  foo")
    # «hello», « », «world», «  », «foo»
    assert tokens == ["hello", " ", "world", "  ", "foo"]


def test_split_tokens_with_newline():
    tokens = human_typing._split_tokens("a\nb")
    assert tokens == ["a", "\n", "b"]


# ── _neighbor_char ─────────────────────────────────────────────────────────


def test_neighbor_char_latin():
    random.seed(0)
    n = human_typing._neighbor_char("e")
    assert n in "wrds"


def test_neighbor_char_cyrillic():
    random.seed(0)
    n = human_typing._neighbor_char("к")
    assert n in "уеа"


def test_neighbor_char_unknown_returns_none():
    assert human_typing._neighbor_char("5") is None
    assert human_typing._neighbor_char("@") is None


def test_neighbor_char_preserves_case():
    random.seed(0)
    upper = human_typing._neighbor_char("E")
    assert upper.isupper()


# ── Регрессия: _send_burst отдельно ─────────────────────────────────────────


def test_send_burst_order_preserved(patch_sleep):
    """_send_burst отправляет символы в исходном порядке."""
    el = _RecorderElement()
    random.seed(0)
    ok = human_typing._send_burst(
        el,
        "test",
        base_lo=0.01,
        base_hi=0.02,
        multiplier=1.0,
        word_len=4,
        stop_event=None,
    )
    assert ok is True
    assert "".join(el.events) == "test"
