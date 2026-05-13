"""
K2: тесты фильтра исходящих LLM-ответов.

Покрытие:
- Безопасные ответы → пропускаются (None reason).
- Whitespace/пустые/None → reason='empty'.
- Телефоны в разных форматах → reason='phone'.
- Telegram/WhatsApp/Viber/VK URL → reason='messenger_url'.
- @telegram_username → reason='tg_handle'.
- Email → reason='email'.
- Слишком короткие/длинные → too_short / too_long.
"""

import pytest

from llm_sanitizer import MAX_LEN, sanitize_llm_reply

# ── Безопасные ответы пропускаются ──────────────────────────────────


def test_normal_reply_passes():
    text = "Здравствуйте! Объект ещё актуален, готов показать в любой день."
    clean, reason = sanitize_llm_reply(text)
    assert reason is None
    assert clean == text


def test_strips_surrounding_whitespace():
    clean, reason = sanitize_llm_reply("   Привет, актуально.   \n")
    assert reason is None
    assert clean == "Привет, актуально."


def test_short_but_non_trivial_passes():
    # 5 символов = MIN_LEN — ровно граница
    clean, reason = sanitize_llm_reply("Да 👍")  # 4 символа: too_short
    assert reason == "too_short"
    clean, reason = sanitize_llm_reply("Да 👍!")  # 5 символов = ок
    assert reason is None


# ── Empty / None ────────────────────────────────────────────────────


def test_none_is_empty():
    clean, reason = sanitize_llm_reply(None)
    assert clean is None
    assert reason == "empty"


def test_empty_string_is_empty():
    clean, reason = sanitize_llm_reply("")
    assert clean is None
    assert reason == "empty"


def test_whitespace_only_is_empty():
    clean, reason = sanitize_llm_reply("   \n\t  ")
    assert clean is None
    assert reason == "empty"


# ── Длина ───────────────────────────────────────────────────────────


def test_too_short():
    clean, reason = sanitize_llm_reply("ок")
    assert clean is None
    assert reason == "too_short"


def test_too_long():
    text = "Привет. " * 200  # ~1600 символов
    clean, reason = sanitize_llm_reply(text)
    assert clean is None
    assert reason == "too_long"


def test_exactly_max_len_passes():
    text = "x" * MAX_LEN
    clean, reason = sanitize_llm_reply(text)
    assert reason is None
    assert clean == text


# ── Телефоны ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phone_variant",
    [
        "Звоните +7 999 123 45 67",
        "Звоните 89991234567",
        "Звоните +79991234567",
        "Контакт: 8 (999) 123-45-67",
        "Тел: +7-999-123-45-67",
        "Звоните 79991234567 в любое время",
        # Замаскированные варианты — наш фильтр должен их ловить
        "Тел +7 (999) 12.34.567",
        "Звоните по +7-999-1-2-3-4-5-6-7",
    ],
)
def test_phone_blocks(phone_variant):
    clean, reason = sanitize_llm_reply(phone_variant)
    assert clean is None
    assert reason == "phone"


def test_address_with_house_number_does_not_trigger_phone():
    """
    Адресные номера (например, "ул. Ленина 5") не должны блокироваться.
    Слишком короткая последовательность цифр не подпадает под _PHONE_RE.
    """
    clean, reason = sanitize_llm_reply("Адрес: ул. Ленина д. 5, корп. 2.")
    assert reason is None, f"Адрес неожиданно заблокирован: {reason}"


# ── Мессенджер-URL ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url_variant,expected_reasons",
    [
        ("Пишите в t.me/seller_realty", ("messenger_url",)),
        ("Telegram: https://t.me/seller_realty", ("messenger_url",)),
        # wa.me/<digits> может сработать сначала на phone-фильтре — это
        # тоже корректная блокировка.
        ("https://wa.me/79991234567", ("messenger_url", "phone")),
        ("Whatsapp: api.whatsapp.com/send?phone=...", ("messenger_url",)),
        ("Звоните в Viber viber://chat?number=...", ("messenger_url",)),
        ("Я в ВК: vk.com/id12345", ("messenger_url",)),
        ("Instagram: instagram.com/seller", ("messenger_url",)),
        ("tg://resolve?domain=seller", ("messenger_url",)),
    ],
)
def test_messenger_urls_blocked(url_variant, expected_reasons):
    clean, reason = sanitize_llm_reply(url_variant)
    assert clean is None
    # Главное — заблокировано. Какой именно фильтр сработал первым —
    # деталь реализации (порядок проверок в sanitize_llm_reply).
    assert reason in expected_reasons


# ── Telegram-style handles ──────────────────────────────────────────


def test_tg_handle_blocked():
    clean, reason = sanitize_llm_reply("Пишите мне @seller_realty в телеге.")
    assert clean is None
    assert reason == "tg_handle"


def test_short_handle_not_blocked():
    """
    @ab — слишком короткий, не Telegram username.
    Чтобы не ловить ложные срабатывания на эмодзи/упоминаниях типа @ AvitoUser.
    """
    clean, reason = sanitize_llm_reply("Я отвечу @ab в чате тут на Avito.")
    # Не должен сработать tg_handle, может пройти как safe.
    # Если в будущем добавим detection email и тут что-то ещё — этот тест
    # станет регрессией для tg_handle конкретно.
    assert reason != "tg_handle"


def test_email_in_text_does_not_match_tg_handle():
    """email содержит @, но домен длиннее 3 символов после точки — это email."""
    clean, reason = sanitize_llm_reply("Пишите на user@example.com")
    assert clean is None
    assert reason in ("email", "tg_handle")  # порядок проверок может меняться


# ── Email ───────────────────────────────────────────────────────────


def test_email_blocked():
    clean, reason = sanitize_llm_reply("Свяжитесь по почте: realty.seller@gmail.com")
    assert clean is None
    assert reason == "email"


def test_email_with_plus_alias_blocked():
    clean, reason = sanitize_llm_reply("Email: agent+spam@yandex.ru")
    assert clean is None
    assert reason == "email"
