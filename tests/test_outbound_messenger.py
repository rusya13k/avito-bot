"""
H1: тесты для outbound_messenger.py.

Проверяем без реального Selenium:
- _pick_persona_for_account: явный override / стабильность по hash
- _generate_first_message: LLM mock / sanitizer-rejection / no LLM client
- OutboundMessenger.run_one_cycle: budget check / cooldown / dedup / no candidates
- F8 cycle dispatch: outbound_only выбирается при правильных весах,
  warmup-режим его не выпадает.
"""

from unittest.mock import MagicMock, patch

import pytest

from outbound_messenger import (
    PERSONAS,
    OutboundMessenger,
    _generate_first_message,
    _pick_persona_for_account,
)

# ── _pick_persona_for_account ──────────────────────────────────────────────


def test_pick_persona_explicit_override():
    """Если account.persona указан и валиден — используется именно она."""
    persona = _pick_persona_for_account({"name": "acc1", "persona": "cafe_owner"})
    assert persona == "cafe_owner"


def test_pick_persona_unknown_override_falls_back_to_hash():
    """Если persona указана НО неизвестна — fallback на hash-based."""
    persona = _pick_persona_for_account({"name": "acc1", "persona": "nonexistent"})
    assert persona in PERSONAS
    # Стабильность: для одного и того же account_name всегда тот же выбор
    persona2 = _pick_persona_for_account({"name": "acc1", "persona": "nonexistent"})
    assert persona == persona2


def test_pick_persona_stable_per_account():
    """Без явного override — выбор детерминированный по account_name.
    Один аккаунт всегда получает ту же персону между запусками
    (важно для consistency: стиль не «прыгает»)."""
    p1 = _pick_persona_for_account({"name": "acc-stable-1"})
    p2 = _pick_persona_for_account({"name": "acc-stable-1"})
    p3 = _pick_persona_for_account({"name": "acc-stable-1"})
    assert p1 == p2 == p3


def test_pick_persona_different_accounts_different_personas():
    """Разные аккаунты — статистически разные персоны (не все одинаковые)."""
    personas = {_pick_persona_for_account({"name": f"acc-{i}"}) for i in range(20)}
    # 20 аккаунтов на 16 персон — должно быть >5 различных
    assert len(personas) >= 5


# ── _generate_first_message ────────────────────────────────────────────────


def test_generate_first_message_no_llm_returns_none():
    """Без llm_classifier → None (нет fallback'а — лучше пропустить чем
    отправить шаблонный текст)."""
    listing = {"title": "Офис 50 м"}
    assert _generate_first_message(None, listing, "small_business_office") is None


def test_generate_first_message_no_client_returns_none():
    """LLM есть, но client=None (нет API-ключа) → None."""
    llm = MagicMock()
    llm.client = None
    listing = {"title": "Офис 50 м"}
    assert _generate_first_message(llm, listing, "small_business_office") is None


def test_generate_first_message_calls_llm_with_prompts():
    """LLM получает system+user message, оба не пустые, в user есть
    persona_description."""
    llm = MagicMock()
    llm.client = MagicMock()
    llm.model = "gpt-3.5-turbo"
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "Привет! Площадь 50 м интересна, можно посмотреть?"
    llm.client.chat.completions.create.return_value = response

    listing = {
        "title": "Офис 50 м в центре",
        "category": "офисные помещения",
        "location": "Москва, центр",
        "area": 50.0,
        "price": 100000,
        "description": "Хороший офис с парковкой",
    }
    result = _generate_first_message(llm, listing, "small_business_office")
    assert result is not None
    assert "50" in result or "офис" in result.lower() or "посмотреть" in result.lower()
    # LLM был вызван 1 раз
    assert llm.client.chat.completions.create.called
    call = llm.client.chat.completions.create.call_args
    messages = call.kwargs.get("messages") or call.args[0] if call.args else call.kwargs["messages"]
    # Должно быть 2 сообщения: system + user
    assert len(messages) == 2
    # User-prompt содержит данные листинга
    user_content = messages[1]["content"]
    assert "Офис 50 м в центре" in user_content
    assert PERSONAS["small_business_office"] in user_content


def test_generate_first_message_sanitizer_rejects_phone():
    """Если LLM выдаёт телефон в ответе — sanitizer возвращает None,
    и мы НЕ отправляем."""
    llm = MagicMock()
    llm.client = MagicMock()
    llm.model = "gpt-3.5-turbo"
    response = MagicMock()
    response.choices = [MagicMock()]
    # LLM нарушил инструкции и впихнул телефон
    response.choices[0].message.content = "Здравствуйте, звоните 89991234567!"
    llm.client.chat.completions.create.return_value = response

    listing = {"title": "Офис"}
    result = _generate_first_message(llm, listing, "small_business_office")
    assert result is None  # sanitizer отрезал


def test_generate_first_message_strips_quotes():
    """LLM иногда оборачивает ответ в кавычки — мы их снимаем."""
    llm = MagicMock()
    llm.client = MagicMock()
    llm.model = "gpt-3.5-turbo"
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = '"Привет, актуально объявление?"'
    llm.client.chat.completions.create.return_value = response

    listing = {"title": "Офис"}
    result = _generate_first_message(llm, listing, "small_business_office")
    assert result is not None
    assert not result.startswith('"')
    assert not result.endswith('"')


# ── OutboundMessenger.run_one_cycle ────────────────────────────────────────


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.get_outbound_count_today.return_value = 0
    db.get_owners_to_contact.return_value = []
    db.was_owner_contacted.return_value = False
    return db


@pytest.fixture
def fake_llm():
    llm = MagicMock()
    llm.client = MagicMock()
    llm.model = "gpt-3.5-turbo"
    return llm


def _make_messenger(db, llm, **kwargs):
    """Вспомогательный fabric. driver/wait — MagicMock, поведение не важно
    (тесты идут по early-return ветвям)."""
    return OutboundMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        account_name=kwargs.pop("account_name", "acc1"),
        account=kwargs.pop("account", {"name": "acc1"}),
        db_manager=db,
        llm_classifier=llm,
        max_per_cycle=kwargs.pop("max_per_cycle", 2),
        listing_min_age_hours=kwargs.pop("listing_min_age_hours", 0),
    )


def test_run_one_cycle_no_db(fake_llm):
    """Без db_manager — ранний return 0."""
    m = OutboundMessenger(
        driver=MagicMock(),
        wait=MagicMock(),
        account_name="acc1",
        db_manager=None,
        llm_classifier=fake_llm,
    )
    log = MagicMock()
    assert m.run_one_cycle(log) == 0


def test_run_one_cycle_skipped_during_cooldown(fake_db, fake_llm):
    """Если аккаунт в captcha-cooldown (B4) — outbound пропускается."""
    m = _make_messenger(fake_db, fake_llm)
    with (
        patch("outbound_messenger._astate.is_cooled_down", return_value=True),
        patch("outbound_messenger._astate.cooldown_remaining_seconds", return_value=600),
    ):
        log = MagicMock()
        sent = m.run_one_cycle(log)
    assert sent == 0
    fake_db.get_owners_to_contact.assert_not_called()


def test_run_one_cycle_budget_exhausted(fake_db, fake_llm):
    """used >= limit → 0 контактов, get_owners_to_contact не вызывается."""
    fake_db.get_outbound_count_today.return_value = 100  # over limit
    m = _make_messenger(fake_db, fake_llm)
    with (
        patch("outbound_messenger._astate.is_cooled_down", return_value=False),
        patch("outbound_messenger._astate.get_effective_limit", return_value=10),
    ):
        log = MagicMock()
        sent = m.run_one_cycle(log)
    assert sent == 0
    fake_db.get_owners_to_contact.assert_not_called()


def test_run_one_cycle_no_candidates(fake_db, fake_llm):
    """Кандидатов нет → 0 контактов."""
    fake_db.get_owners_to_contact.return_value = []
    m = _make_messenger(fake_db, fake_llm)
    with (
        patch("outbound_messenger._astate.is_cooled_down", return_value=False),
        patch("outbound_messenger._astate.get_effective_limit", return_value=10),
    ):
        log = MagicMock()
        sent = m.run_one_cycle(log)
    assert sent == 0


def test_run_one_cycle_dedup_race_skipped(fake_db, fake_llm):
    """Между get_owners_to_contact и actual contact — race condition:
    другой поток уже законтактировал. _contact_one должен это поймать
    через was_owner_contacted и скипнуть, не открывая driver.get."""
    fake_db.get_owners_to_contact.return_value = [
        {
            "id": 1,
            "url": "https://x",
            "title": "Офис",
            "profile_id": "o1",
            "seller_name": "S",
            "location": "Москва",
            "area": 50.0,
            "price": 100000,
            "description": "...",
            "category": "офис",
        },
    ]
    fake_db.was_owner_contacted.return_value = True  # race!

    m = _make_messenger(fake_db, fake_llm)
    with (
        patch("outbound_messenger._astate.is_cooled_down", return_value=False),
        patch("outbound_messenger._astate.get_effective_limit", return_value=10),
    ):
        log = MagicMock()
        sent = m.run_one_cycle(log)
    assert sent == 0
    # driver.get НЕ должен быть вызван — мы скипнули до открытия URL
    m.driver.get.assert_not_called()


# ── F8 cycle integration ────────────────────────────────────────────────────


def test_outbound_only_in_default_cycle_kinds():
    """outbound_only присутствует в _CYCLE_KINDS_DEFAULT с положительным весом."""
    from bot import _CYCLE_KINDS_DEFAULT

    assert "outbound_only" in _CYCLE_KINDS_DEFAULT
    assert _CYCLE_KINDS_DEFAULT["outbound_only"] > 0


def test_outbound_only_disabled_in_warmup():
    """В warmup-режиме outbound_only имеет вес 0 — никогда не выпадает."""
    from bot import _CYCLE_KINDS_WARMUP, _pick_cycle_kind

    assert _CYCLE_KINDS_WARMUP.get("outbound_only", 0) == 0
    # 200 семплов — outbound_only не должен выпадать в warmup
    for _ in range(200):
        kind = _pick_cycle_kind({}, {}, is_warmup=True)
        assert kind != "outbound_only"


def test_outbound_only_can_be_picked_in_default():
    """В дефолтном режиме outbound_only хотя бы изредка выпадает (вес 0.30)."""
    from bot import _pick_cycle_kind

    kinds = [_pick_cycle_kind({}, {}, is_warmup=False) for _ in range(200)]
    assert "outbound_only" in kinds


# ── Account state: outbound action поддержан ──────────────────────────────


def test_default_daily_budget_includes_outbound():
    """DEFAULT_DAILY_BUDGET содержит ключ 'outbound' (для get_effective_limit)."""
    from account_state import DEFAULT_DAILY_BUDGET

    assert "outbound" in DEFAULT_DAILY_BUDGET
    assert DEFAULT_DAILY_BUDGET["outbound"] > 0


def test_budget_metric_map_includes_outbound():
    """_BUDGET_METRIC_MAP связывает outbound с метрикой outbound_initiated."""
    from account_state import _BUDGET_METRIC_MAP

    assert _BUDGET_METRIC_MAP.get("outbound") == "outbound_initiated"
