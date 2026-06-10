"""
H1: Outbound messenger — proactive flow «бот пишет собственнику первым».

В отличие от avito_messenger.py (reactive — отвечаем только на входящие),
этот модуль инициирует диалог:
  1. Берёт N кандидатов из БД (классифицированы как 'owner', не контактированы).
  2. Открывает страницу листинга через Selenium.
  3. Находит кнопку «Написать» / chat-input.
  4. LLM генерирует первое сообщение по контексту листинга + persona аккаунта.
  5. Отправляет, записывает в outbound_contacts (UNIQUE по profile_id).
  6. Учитывает в A2-budget (метрика outbound_initiated) и captcha-checks.

Почему отдельный модуль (не внутри AvitoMessenger):
- Совершенно другой entry-point: не /messenger, а /<listing-url>.
- Свой anti-fingerprinting: персоны, варьирование стиля, минимальная
  скорость (one outbound per cycle с человекоподобными паузами).
- Свой dedup-протокол (UNIQUE profile_id в БД, см. database.py:H1).

Anti-fingerprinting:
- 16 персон × 3 уровня формальности × 2 длины × 3 подхода = 288 комбо.
- LLM получает разный stylistic hint каждый раз → разный текст.
- llm_sanitizer.sanitize_llm_reply отрезает phone/email/url/messenger-handle.
- min_listing_age_hours отсекает «только что распарсенные» — реальный
  человек не пишет через 30 секунд после публикации.
"""

import logging
import random
import time

from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import tg_bot as _tg
from account_state import account_state as _astate
from captcha_detect import detect_phone_captcha
from human_delay import human_delay as _human_delay
from llm_sanitizer import sanitize_llm_reply

logger = logging.getLogger(__name__)


def _escape_format(text: object) -> str:
    """Экранирует фигурные скобки в пользовательских данных перед .format()."""
    s = str(text)
    return s.replace("{", "{{").replace("}", "}}")


def _stopping(account_name: str) -> bool:
    return _tg.is_stop_requested(account_name)


# ─────────────────────────────────────────────────────────────────────────────
# Personas — кто бот «изображает», когда пишет первый раз собственнику.
# Конкретное значение — описание для LLM-промпта (поле persona_description).
# Каждый аккаунт получает СВОЮ персону через accounts.json["persona"], либо
# выбирается случайно из пула (один и тот же persona по аккаунту всю сессию,
# чтобы стиль не «прыгал»).
# ─────────────────────────────────────────────────────────────────────────────

PERSONAS: dict[str, str] = {
    "tatarstan_developer": (
        "девелопер из Татарстана, занимаюсь строительством и сдачей небольших торговых центров. "
        "Уже есть 15 готовых арендных бизнесов. Ищу финансовых партнеров для строительства новых объектов. "
        "Средняя окупаемость 6-8 лет. Интересует партнерство, не аренда конкретного объекта."
    ),
}

# Стилистические вариации — добавляются в промпт чтобы LLM каждый раз
# писал по-другому. Иначе один аккаунт даёт одинаковый паттерн всем
# собственникам — это палево.
_FORMALITY_LEVELS = [
    "очень неформальный (как с приятелем, можно «привет», без вежливых форм)",
    "нейтральный (без формальностей, но и без панибратства)",
    "вежливо-деловой (полное «здравствуйте», но без канцеляризмов)",
]

_LENGTH_HINTS = [
    "очень коротко (1 предложение, прямой вопрос)",
    "среднее (2-3 предложения, контекст + вопрос)",
]

_APPROACH_HINTS = [
    "сразу к делу (один открытый вопрос про объект)",
    "сначала упомянуть деталь объявления, потом вопрос",
    "представиться чем интересуюсь, что важно — короткой строкой",
]


# ─────────────────────────────────────────────────────────────────────────────
# Pitch-mode: персоны, которые НЕ откликаются на конкретный объект, а делают
# B2B-питч собственнику (партнёрство, инвестиции). Отдельные prompts +
# отдельные стилистические подсказки (длиннее, формальнее, без привязки к
# деталям объявления). Добавляешь персону сюда → используется pitch-prompt.
# ─────────────────────────────────────────────────────────────────────────────

_PITCH_PERSONAS: frozenset[str] = frozenset(
    {
        "tatarstan_developer",
    }
)

_PITCH_FORMALITY_LEVELS = [
    "вежливо-деловой (полное «Здравствуйте», без канцеляризмов и без «уважаемые»)",
    "нейтрально-уверенный (как опытный предприниматель — ёмко и по делу)",
]

_PITCH_LENGTH_HINTS = [
    "стандарт (3-4 предложения: представление + предложение + вопрос)",
    "развёрнутый (4-5 предложений с одной дополнительной деталью)",
]

_PITCH_APPROACH_HINTS = [
    "сразу к питчу: представился — изложил предложение — задал вопрос",
    "сначала вежливое «Здравствуйте.», затем что делаешь и что ищешь, в конце — открытый вопрос",
    "представление как ёмкая характеристика того, чем занимаешься, и сразу — поиск партнёров",
]


def _is_pitch_persona(persona_id: str) -> bool:
    """H1: вернёт True, если для этой персоны нужно использовать pitch-mode
    promptы (B2B-предложение партнёрства), а не обычный rent-mode (отклик
    арендатора на объявление). См. _PITCH_PERSONAS.
    """
    return persona_id in _PITCH_PERSONAS


def _pick_persona_for_account(account: dict) -> str:
    """H1: выбор персоны. Per-account фиксация (через accounts.json)
    предпочтительна: стиль не «прыгает». Если не задана — берём
    случайную из PERSONAS, но с учётом: один и тот же random.seed по
    account_name, чтобы между запусками персона была стабильна.

    Возвращает persona_id (ключ в PERSONAS).
    """
    explicit = account.get("persona")
    if explicit and explicit in PERSONAS:
        return explicit

    # Стабильный выбор: hash(account_name) → индекс. Так каждый аккаунт
    # получит свою персону детерминировано, без явной настройки в JSON.
    account_name = account.get("name", "")
    if not account_name:
        return random.choice(list(PERSONAS.keys()))
    seeded = random.Random(account_name)
    return seeded.choice(list(PERSONAS.keys()))


def _generate_first_message(llm_classifier, listing: dict, persona_id: str) -> str | None:
    """H1: вызывает LLM для генерации первого сообщения. Возвращает
    sanitized текст или None если LLM/sanitizer отбросили результат.

    LLM получает context листинга + персону + случайные стилевые подсказки
    (formality, length, approach). Это даёт высокую вариативность даже
    при одной и той же персоне.

    Retry: до 2 попыток при transient-ошибках (timeout, 5xx).
    """
    if llm_classifier is None or not getattr(llm_classifier, "api_key", ""):
        return None

    persona_description = PERSONAS.get(persona_id, "")
    is_pitch = _is_pitch_persona(persona_id)

    if is_pitch:
        formality = random.choice(_PITCH_FORMALITY_LEVELS)
        length = random.choice(_PITCH_LENGTH_HINTS)
        approach = random.choice(_PITCH_APPROACH_HINTS)
        system_prompt_name = "outbound_first_message_pitch.system.txt"
        user_prompt_name = "outbound_first_message_pitch.user.txt"
    else:
        formality = random.choice(_FORMALITY_LEVELS)
        length = random.choice(_LENGTH_HINTS)
        approach = random.choice(_APPROACH_HINTS)
        system_prompt_name = "outbound_first_message.system.txt"
        user_prompt_name = "outbound_first_message.user.txt"

    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            from llm_classifier import _load_prompt

            system_message = _load_prompt(system_prompt_name)
            user_prompt = _load_prompt(user_prompt_name).format(
                title=_escape_format(listing.get("title", "—")),
                category=_escape_format(listing.get("category", "коммерческая недвижимость")),
                location=_escape_format(listing.get("location", "—")),
                area=_escape_format(listing.get("area", "—")),
                price=_escape_format(listing.get("price", "не указана")),
                description=_escape_format((listing.get("description") or "")[:600]),
                persona_description=persona_description,
                formality=formality,
                length=length,
                approach=approach,
            )

            raw = llm_classifier._call_llm(
                system_message=system_message,
                user_message=user_prompt,
                temperature=0.95,
            )
            if raw is None:
                if attempt < max_attempts - 1:
                    time.sleep(2)
                    continue
                llm_classifier._incr_llm_error("outbound:empty")
                return None
            raw = raw.strip('"').strip("'").strip()

            clean, reason = sanitize_llm_reply(raw)
            if clean is None:
                logger.info("outbound: LLM sanitizer отбросил (%s): %r", reason, raw[:100])
                return None
            return clean
        except Exception as exc:
            logger.warning(
                "outbound: generate_first_message attempt %d/%d failed: %s",
                attempt + 1,
                max_attempts,
                exc,
            )
            if attempt < max_attempts - 1:
                time.sleep(3)
            else:
                llm_classifier._incr_llm_error("outbound:api")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# OutboundMessenger — основной класс
# ─────────────────────────────────────────────────────────────────────────────


class OutboundMessenger:
    """H1: proactive outreach к собственникам коммерческой недвижимости.

    Использование (см. AvitoClient.run_outbound_cycle):
        m = OutboundMessenger(driver, wait, account_name, account=account,
                              db_manager=db, llm_classifier=llm)
        m.run_one_cycle(max_per_cycle=2)  # пишет 1-3 собственникам за цикл

    Параметры (per-account, передаются в __init__ из AvitoClient):
        max_per_cycle: int      — сколько максимум контактов за один цикл
        listing_min_age_hours   — отсекает свежие листинги (анти-fingerprint)
        between_messages_min/max_sec — пауза между двумя outbound в одном цикле
    """

    def __init__(
        self,
        driver,
        wait,
        account_name: str,
        *,
        account: dict | None = None,
        db_manager=None,
        llm_classifier=None,
        max_per_cycle: int = 3,
        listing_min_age_hours: float = 4.0,
        between_messages_min_sec: float = 90.0,
        between_messages_max_sec: float = 240.0,
    ):
        self.driver = driver
        self.wait = wait
        self.account_name = account_name
        self.account = account or {"name": account_name}
        self.db = db_manager
        self.llm = llm_classifier
        self.max_per_cycle = int(max_per_cycle)
        self.listing_min_age_hours = float(listing_min_age_hours)
        self.between_messages_min_sec = float(between_messages_min_sec)
        self.between_messages_max_sec = float(between_messages_max_sec)
        self.persona_id = _pick_persona_for_account(self.account)

    # ── Public API ─────────────────────────────────────────────────────────

    def run_one_cycle(self, log_func) -> int:
        """Один цикл outbound: 1..max_per_cycle сообщений собственникам.
        Возвращает сколько сообщений реально отправлено (0 если budget
        исчерпан, нет кандидатов, или произошли ошибки).
        """
        if _stopping(self.account_name) or self.db is None:
            return 0

        # Проверяем cooldown (B4) — если аккаунт «остывает» после капч,
        # outbound запрещён вместе со всеми остальными write-действиями.
        if _astate.is_cooled_down(self.account_name):
            log_func(
                self.account_name,
                f"H1: outbound пропущен — аккаунт в cooldown ещё "
                f"{_astate.cooldown_remaining_seconds(self.account_name)}s",
            )
            return 0

        # A2 budget — общий лимит на outbound сегодня.
        try:
            limit = _astate.get_effective_limit(self.account_name, "outbound")
        except Exception:
            limit = 10  # fallback дефолт
        used = self.db.get_outbound_count_today(self.account_name)
        if used >= limit:
            log_func(
                self.account_name,
                f"H1: outbound budget исчерпан ({used}/{limit}). Пропускаем цикл.",
            )
            return 0

        remaining = limit - used
        n_to_send = min(self.max_per_cycle, remaining)
        if n_to_send <= 0:
            return 0

        candidates = self.db.get_owners_to_contact(
            self.account_name,
            limit=n_to_send + 2,  # +2 на случай если конкретный листинг отвалится
            min_age_hours=self.listing_min_age_hours,
        )
        if not candidates:
            log_func(self.account_name, "H1: нет кандидатов-собственников для outbound.")
            return 0

        log_func(
            self.account_name,
            f"H1: outbound план — до {n_to_send} контактов, найдено "
            f"{len(candidates)} кандидатов, persona={self.persona_id}.",
        )

        sent_count = 0
        for listing in candidates:
            if sent_count >= n_to_send or _stopping(self.account_name):
                break

            # Между двумя outbound в одном цикле — длинная human-пауза.
            # Первое сообщение — без паузы, иначе ждём.
            if sent_count > 0:
                _human_delay(
                    self.between_messages_min_sec,
                    self.between_messages_max_sec,
                    stop_event=_tg.get_account_stop_event(self.account_name),
                    distribution="lognormal",
                )

            ok = self._contact_one(listing, log_func)
            if ok:
                sent_count += 1

        log_func(
            self.account_name,
            f"H1: outbound завершён — отправлено {sent_count}/{n_to_send}.",
        )
        return sent_count

    # ── Internal flow ──────────────────────────────────────────────────────

    def _contact_one(self, listing: dict, log_func) -> bool:
        """Один outbound-контакт: открыть листинг → клик «Написать» →
        сгенерировать первое сообщение → отправить → record в БД.
        Возвращает True если сообщение реально отправлено и записано.
        """
        profile_id = listing.get("profile_id")
        listing_id = listing.get("id")
        listing_url = listing.get("url")

        if not profile_id or not listing_url:
            log_func(self.account_name, f"H1: листинг id={listing_id} без profile_id/url, скип.")
            return False

        # Race-defense: между get_owners_to_contact и сейчас другой
        # поток мог уже законтактировать. Перед открытием URL проверяем.
        if self.db.was_owner_contacted(profile_id):
            log_func(
                self.account_name,
                f"H1: profile_id={profile_id} уже контактирован — race, скип.",
            )
            return False

        # 1. Генерим текст ДО открытия URL — если LLM не справится,
        # сэкономим Selenium-транзит.
        text = _generate_first_message(self.llm, listing, self.persona_id)
        if not text:
            log_func(
                self.account_name,
                "H1: не получилось сгенерировать сообщение (LLM/sanitizer), скип.",
            )
            return False

        # 2. Открываем страницу листинга с referrer-симуляцией.
        # Прямой driver.get() оставляет пустой referrer — паттерн бота.
        # Реальный пользователь приходит из поиска или из списка объявлений.
        try:
            # Сначала переходим на страницу поиска (если ещё не там),
            # чтобы referrer был avito.ru/...
            current = self.driver.current_url or ""
            if "avito.ru" not in current:
                self.driver.get("https://www.avito.ru/")
                _human_delay(
                    2,
                    4,
                    stop_event=_tg.get_account_stop_event(self.account_name),
                    distribution="normal",
                )
            # Переход на листинг через JS — сохраняет referrer текущей страницы
            self.driver.execute_script("window.location.href = arguments[0];", listing_url)
        except WebDriverException as exc:
            log_func(self.account_name, f"H1: navigate to {listing_url} fail: {exc}")
            return False

        _human_delay(
            3, 7, stop_event=_tg.get_account_stop_event(self.account_name), distribution="normal"
        )

        if _stopping(self.account_name):
            return False

        # 3. Pre-click captcha check.
        if detect_phone_captcha(self.driver, log_func=log_func, account_name=self.account_name):
            log_func(self.account_name, "H1: капча на странице листинга — скип.")
            # T17: листинг-уровень капча. Триггерит outbound-disable на 24h.
            _astate.mark_captcha(self.account_name, captcha_type="avito_listing")
            return False

        # 4. Открываем chat-overlay.
        if not self._open_chat_overlay(log_func):
            return False

        # 5. Отправляем текст.
        if not self._type_and_send(text, log_func):
            return False

        # 6. Post-send capture: record + metrics.
        try:
            persona_label = self.persona_id
            with self.db.transaction() as cur:
                created = self.db.record_outbound(
                    account_name=self.account_name,
                    profile_id=profile_id,
                    listing_id=listing_id,
                    listing_url=listing_url,
                    status="sent",
                    persona=persona_label,
                    message_text=text,
                    cursor=cur,
                )
                if created:
                    self.db.incr_metric(self.account_name, "outbound_initiated", cursor=cur)
                else:
                    # UNIQUE hit: гонка, кто-то записал параллельно.
                    log_func(
                        self.account_name,
                        f"H1: profile_id={profile_id} race-hit при INSERT, метрику не пишем.",
                    )
        except Exception as exc:
            logger.exception("H1: ошибка записи outbound в БД: %s", exc)
            return False

        log_func(
            self.account_name,
            f"H1: ✉ outbound отправлен -> profile_id={profile_id} (persona={persona_label}, "
            f"len={len(text)}): {text[:80]}{'...' if len(text) > 80 else ''}",
        )
        return True

    def _open_chat_overlay(self, log_func) -> bool:
        """Делегирует в chat_sender."""
        from chat_sender import open_chat_overlay as _open_overlay

        ok = _open_overlay(self.driver, self.account_name)
        if not ok:
            log_func(self.account_name, "H1: кнопка «Написать» не найдена на листинге.")
        return ok

    def _type_and_send(self, text: str, log_func) -> bool:
        """Делегирует в chat_sender."""
        from chat_sender import type_and_send as _type_send

        ok = _type_send(self.driver, self.account_name, text, self.persona_id)
        if not ok:
            log_func(self.account_name, "H1: type_and_send не удался.")
        else:
            log_func(self.account_name, f"H1: сообщение отправлено ({len(text)} chars).")
        return ok

    def _verify_message_appeared(self, log_func) -> bool:
        """После отправки проверить что сообщение появилось в DOM чата.
        Считаем количество элементов messenger/message до и после отправки."""
        xpath = "//*[@data-marker='messenger/message']"
        try:
            before = len(self.driver.find_elements(By.XPATH, xpath))
            # Ждём до 3 секунд появления нового сообщения
            WebDriverWait(self.driver, 3).until(
                lambda d: len(d.find_elements(By.XPATH, xpath)) > before
            )
            return True
        except TimeoutException:
            return False
        except Exception:
            return False
