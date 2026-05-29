"""
T5: Realistic typing 2.0 — burst + опечатки.

Старая `human_type` была "равномерно медленной": per-char uniform sleep —
антифрод видит ровную гистограмму задержек (легко детектится).

Новая модель:

1. БЁРСТЫ: пачки по 3-5 символов с малыми задержками 40-100ms,
   потом отдых 180-380ms между бёрстами. Имитирует моторное
   программирование пальцев (мозг готовит «пачку нажатий», потом
   короткая пауза, потом следующая пачка).

2. ОПЕЧАТКИ: ~5-8% слов длиной ≥ 3 — печатается «соседняя» буква
   по QWERTY/ЙЦУКЕН, «замечается» через 100-400ms, исправляется
   BACKSPACE и правильной буквой.

3. ЦИФРЫ / ПУНКТУАЦИЯ: задержка ×1.5-2.0 (shift / numpad — медленнее).

4. ДЛИННЫЕ СЛОВА (≥ 8 символов): чуть быстрее (×0.85) — моторная память.

5. ИНОГДА (~5%) — «задумчивая» пауза 300-800ms после слова.

6. PERSONA / SPEED_MULTIPLIER: общий множитель для всех задержек.
   Default 1.0. < 1 = быстрее ("молодой / стартапер"),
   > 1 = медленнее ("инвестор / врач"). См. persona_speed_multiplier().

Public API:

    type_human(element, text, *,
               speed_range=(0.05, 0.25),
               speed_multiplier=1.0,
               typo_rate=0.06,
               enable_bursts=True,
               enable_typos=True,
               stop_event=None) -> bool

    Возвращает True если допечатало до конца, False если прервано stop_event.

    persona_speed_multiplier(persona_id: str | None) -> float
"""

from __future__ import annotations

import random
import threading
import time

from selenium.webdriver.common.keys import Keys

# QWERTY-соседи для имитации латиничных опечаток.
# Сосед — буква, до которой палец «промахивается» на стандартной клавиатуре.
_LATIN_NEIGHBORS: dict[str, str] = {
    "q": "wa", "w": "qeas", "e": "wrds", "r": "etdf", "t": "ryfg",
    "y": "tuhg", "u": "yihj", "i": "uojk", "o": "ipkl", "p": "ol",
    "a": "qwsz", "s": "awdze", "d": "serfxc", "f": "drtgcv", "g": "ftyhvb",
    "h": "gyjbn", "j": "hukmn", "k": "jilm", "l": "kop",
    "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn",
    "n": "bhjm", "m": "njk",
}  # fmt: skip

# Кириллица — ЙЦУКЕН раскладка.
_CYR_NEIGHBORS: dict[str, str] = {
    "й": "цф", "ц": "йук", "у": "цкв", "к": "уеа", "е": "кнп",
    "н": "ег", "г": "нш", "ш": "гщ", "щ": "шз", "з": "щх",
    "х": "зъ", "ъ": "х",
    "ф": "йя", "ы": "вч", "в": "ыас", "а": "вп", "п": "ар",
    "р": "по", "о": "рл", "л": "од", "д": "лж", "ж": "дэ", "э": "ж",
    "я": "фч", "ч": "яыс", "с": "чм", "м": "си", "и": "мт",
    "т": "иь", "ь": "тб", "б": "ью", "ю": "б",
}  # fmt: skip


def _neighbor_char(ch: str) -> str | None:
    """Вернуть «соседнюю» букву по QWERTY/ЙЦУКЕН (для опечатки) или None
    если для символа нет соседей (цифра, пунктуация, неизвестный язык).
    """
    lower = ch.lower()
    neighbors = _LATIN_NEIGHBORS.get(lower) or _CYR_NEIGHBORS.get(lower)
    if not neighbors:
        return None
    wrong = random.choice(neighbors)
    return wrong.upper() if ch.isupper() else wrong


def _char_delay(ch: str, *, base_lo: float, base_hi: float, word_len: int) -> float:
    """Задержка для одного символа внутри бёрста.

    - Цифры / пунктуация — ×1.5-2.0 (shift / numpad).
    - Длинные слова (≥ 8 символов) — ×0.85 (моторная память).
    - Обычная буква — uniform(base_lo, base_hi).
    """
    base = random.uniform(base_lo, base_hi)
    if ch.isdigit() or (not ch.isalpha() and not ch.isspace()):
        base *= random.uniform(1.5, 2.0)
    elif word_len >= 8:
        base *= 0.85
    return base


def _sleep_chunked(seconds: float, stop_event: threading.Event | None) -> bool:
    """Sleep, дробимый на чанки по 0.1s, чтобы прерываться по stop_event.

    Возвращает True если выспалось полностью, False если прервано.
    """
    if seconds <= 0:
        return True
    if stop_event is None:
        time.sleep(seconds)
        return True
    end = time.time() + seconds
    while True:
        if stop_event.is_set():
            return False
        remaining = end - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(0.1, remaining))


def _split_tokens(text: str) -> list[str]:
    """Разбить текст на токены: подряд идущие пробельные / непробельные.

    Сохраняем разделители (включая \\n) — нужны и для тайминга, и для
    того, чтобы их физически отправить через send_keys.
    """
    tokens: list[str] = []
    if not text:
        return tokens
    cur = text[0]
    cur_is_space = cur.isspace()
    for ch in text[1:]:
        is_space = ch.isspace()
        if is_space == cur_is_space:
            cur += ch
        else:
            tokens.append(cur)
            cur = ch
            cur_is_space = is_space
    tokens.append(cur)
    return tokens


def _send_burst(
    element,
    chars: str,
    *,
    base_lo: float,
    base_hi: float,
    multiplier: float,
    word_len: int,
    stop_event: threading.Event | None,
) -> bool:
    """Напечатать пачку символов «бёрстом» — быстро, без отдыха внутри.

    Возвращает True если допечатало, False если прервано.
    """
    for ch in chars:
        if stop_event is not None and stop_event.is_set():
            return False
        element.send_keys(ch)
        delay = _char_delay(ch, base_lo=base_lo, base_hi=base_hi, word_len=word_len) * multiplier
        if not _sleep_chunked(delay, stop_event):
            return False
    return True


def _type_word(
    element,
    word: str,
    *,
    base_lo: float,
    base_hi: float,
    multiplier: float,
    typo_rate: float,
    enable_bursts: bool,
    enable_typos: bool,
    stop_event: threading.Event | None,
) -> bool:
    """Напечатать одно «слово» (непрерывный непробельный токен).

    Возвращает True если допечатало, False если прервано.
    """
    word_len = len(word)

    # Опечатка применяется максимум 1 раз и только если есть QWERTY-сосед.
    typo_pos = -1
    if enable_typos and word_len >= 3 and random.random() < typo_rate:
        candidate = random.randint(0, word_len - 1)
        if _neighbor_char(word[candidate]) is not None:
            typo_pos = candidate

    pos = 0
    while pos < word_len:
        if stop_event is not None and stop_event.is_set():
            return False

        # Если выпало место опечатки внутри текущего бёрста — обрабатываем
        # её отдельно: префикс → wrong → backspace → correct → продолжаем.
        if pos <= typo_pos < pos + 5:
            # 1. Префикс до опечатки.
            if pos < typo_pos:
                if not _send_burst(
                    element,
                    word[pos:typo_pos],
                    base_lo=base_lo,
                    base_hi=base_hi,
                    multiplier=multiplier,
                    word_len=word_len,
                    stop_event=stop_event,
                ):
                    return False
            # 2. «Не та» буква.
            wrong = _neighbor_char(word[typo_pos]) or word[typo_pos]
            element.send_keys(wrong)
            # 3. Замечаем опечатку — 100-400ms «о, не та».
            if not _sleep_chunked(random.uniform(0.10, 0.40) * multiplier, stop_event):
                return False
            # 4. Backspace.
            element.send_keys(Keys.BACKSPACE)
            if not _sleep_chunked(random.uniform(0.05, 0.15) * multiplier, stop_event):
                return False
            # 5. Правильная буква.
            element.send_keys(word[typo_pos])
            correct_delay = (
                _char_delay(
                    word[typo_pos],
                    base_lo=base_lo,
                    base_hi=base_hi,
                    word_len=word_len,
                )
                * multiplier
            )
            if not _sleep_chunked(correct_delay, stop_event):
                return False
            pos = typo_pos + 1
            typo_pos = -1
            continue

        # Обычный бёрст 3-5 символов (или 1 если бёрсты отключены).
        burst_len = random.randint(3, 5) if enable_bursts else 1
        end_pos = min(pos + burst_len, word_len)
        if not _send_burst(
            element,
            word[pos:end_pos],
            base_lo=base_lo,
            base_hi=base_hi,
            multiplier=multiplier,
            word_len=word_len,
            stop_event=stop_event,
        ):
            return False
        pos = end_pos

        # Отдых между бёрстами (только если ещё не конец слова).
        if pos < word_len and enable_bursts:
            rest = random.uniform(0.18, 0.38) * multiplier
            if not _sleep_chunked(rest, stop_event):
                return False
            # ~5% — «задумался» дольше.
            if random.random() < 0.05:
                if not _sleep_chunked(random.uniform(0.30, 0.80) * multiplier, stop_event):
                    return False

    return True


def type_human(
    element,
    text: str,
    *,
    speed_range: tuple[float, float] = (0.05, 0.25),
    speed_multiplier: float = 1.0,
    typo_rate: float = 0.06,
    enable_bursts: bool = True,
    enable_typos: bool = True,
    stop_event: threading.Event | None = None,
) -> bool:
    """T5: реалистичный typing с бёрстами и опечатками.

    Args:
        element: Selenium-элемент с методом .send_keys.
        text: текст для ввода.
        speed_range: (lo, hi) базовый диапазон задержки между символами
            внутри бёрста. Default (0.05, 0.25) — обычная скорость.
            Логин-инпуты лучше (0.10, 0.35) — чуть медленнее, аккуратнее.
        speed_multiplier: множитель ВСЕХ задержек. 1.0 = default,
            < 1 = быстрее (молодой / стартапер), > 1 = медленнее
            (инвестор / врач). Используется через persona_speed_multiplier.
        typo_rate: вероятность опечатки на слово ≥ 3 символов. 0 = выкл.
        enable_bursts: True (default) = бёрсты 3-5 + отдых 180-380ms.
            False = char-by-char (для очень коротких полей).
        enable_typos: False = опечатки выключены. Рекомендуется False
            для логин-форм (phone / password) — там backspace может
            сбивать валидаторы.
        stop_event: если задан и сработал — прерываем typing раньше.

    Returns:
        True если допечатало до конца, False если прервано stop_event.
    """
    if not text:
        return True
    lo, hi = speed_range
    if speed_multiplier <= 0:
        speed_multiplier = 1.0

    tokens = _split_tokens(text)
    for token in tokens:
        if stop_event is not None and stop_event.is_set():
            return False

        if token[0].isspace():
            # Пробелы / \n печатаем char-by-char с короткой паузой 60-180ms.
            for ch in token:
                if stop_event is not None and stop_event.is_set():
                    return False
                element.send_keys(ch)
                delay = random.uniform(0.06, 0.18) * speed_multiplier
                if not _sleep_chunked(delay, stop_event):
                    return False
            continue

        # Слово.
        if not _type_word(
            element,
            token,
            base_lo=lo,
            base_hi=hi,
            multiplier=speed_multiplier,
            typo_rate=typo_rate,
            enable_bursts=enable_bursts,
            enable_typos=enable_typos,
            stop_event=stop_event,
        ):
            return False

        # ~5% — после слова «задумчивая» пауза 300-800ms.
        if random.random() < 0.05:
            if not _sleep_chunked(random.uniform(0.30, 0.80) * speed_multiplier, stop_event):
                return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Persona → multiplier (T5 / T23 — частичный).
# ─────────────────────────────────────────────────────────────────────────────

# Чем больше число — тем медленнее печатает (= аккуратнее). Persona-id
# совпадает с ключами outbound_messenger.PERSONAS, но мы НЕ импортируем
# оттуда (избегаем циклической зависимости). Неизвестная persona → 1.0.
_PERSONA_SPEED_MULTIPLIERS: dict[str, float] = {
    # Молодые / шустрые (стартаперы)
    "retail_starter": 0.95,
    "ecommerce_warehouse": 0.95,
    "fitness_studio": 0.95,
    # Средние — обычный темп
    "cafe_owner": 1.00,
    "beauty_salon": 1.00,
    "small_business_office": 1.00,
    # Аккуратные / солидные — печатают медленнее
    "clinic_medical": 1.10,
    "investor": 1.20,
    "tatarstan_developer": 1.15,
}


def persona_speed_multiplier(persona_id: str | None) -> float:
    """Вернуть коэффициент скорости печати для конкретной persona.

    Если персона не задана / неизвестна → 1.0 (нейтрально).
    """
    if not persona_id:
        return 1.0
    return _PERSONA_SPEED_MULTIPLIERS.get(persona_id, 1.0)


def length_speed_multiplier(text: str) -> float:
    """Адаптивный множитель скорости в зависимости от длины сообщения.

    Реальный человек:
    - Короткие сообщения (<30 символов): печатает быстрее (×0.8) — "привет", "ок", "да"
    - Средние (30-100): нормальная скорость (×1.0)
    - Длинные (100-200): чуть медленнее (×1.1) — больше "задумчивых" пауз
    - Очень длинные (>200): ещё медленнее (×1.2) — перечитывает, думает

    Возвращает множитель для speed_multiplier.
    """
    n = len(text)
    if n < 30:
        return 0.8
    if n <= 100:
        return 1.0
    if n <= 200:
        return 1.1
    return 1.2
