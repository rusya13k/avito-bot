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
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import tg_bot as _tg
from account_state import account_state as _astate
from captcha_detect import detect_phone_captcha
from human_delay import human_delay as _human_delay
from llm_sanitizer import sanitize_llm_reply

logger = logging.getLogger(__name__)


def _stopping() -> bool:
    return _tg.stop_event.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# Personas — кто бот «изображает», когда пишет первый раз собственнику.
# Конкретное значение — описание для LLM-промпта (поле persona_description).
# Каждый аккаунт получает СВОЮ персону через accounts.json["persona"], либо
# выбирается случайно из пула (один и тот же persona по аккаунту всю сессию,
# чтобы стиль не «прыгал»).
# ─────────────────────────────────────────────────────────────────────────────

PERSONAS: dict[str, str] = {
    "small_business_office": (
        "представитель небольшой компании (5-10 человек), ищу офис для команды. "
        "Бухгалтерия, переговорная, кухня. Бюджет ограничен."
    ),
    "retail_starter": (
        "открываю первый магазин, ищу торговое помещение в проходимом месте. "
        "Опыта мало, поэтому интересуют детали по трафику и соседям."
    ),
    "ecommerce_warehouse": (
        "веду интернет-магазин, нужен склад под товары. "
        "Важны удобный въезд для фургонов, сухость, отопление."
    ),
    "cafe_owner": (
        "ищу площадь под кафе/кофейню. Важны вытяжка, мокрые точки, "
        "размер помещения, район."
    ),
    "clinic_medical": (
        "ищу помещение под медицинский центр / стоматологию. "
        "Важны санузлы, водоотведение, лицензируемость."
    ),
    "beauty_salon": (
        "ищу под салон красоты / барбершоп. "
        "Важны окна, вытяжка, мокрые точки, проходимость."
    ),
    "fitness_studio": (
        "открываю небольшую студию (йога/растяжка/пилатес). "
        "Нужен ровный пол, высокие потолки, душевая зона."
    ),
    "service_workshop": (
        "ищу под сервис (мастерская, шиномонтаж, авторемонт). "
        "Важен заезд для авто, высота ворот, электричество."
    ),
    "educational": (
        "открываю детский центр / курсы. "
        "Нужны несколько кабинетов, санузел, безопасный двор."
    ),
    "coworking": (
        "развиваю небольшой коворкинг. "
        "Нужен open space, переговорная, скоростной интернет."
    ),
    "logistics": (
        "логистическая компания, ищу склад. "
        "Важны площадь, рампа, въезд для длинномера, охрана."
    ),
    "it_office": (
        "IT-команда, ищу офис на 10-15 рабочих мест. "
        "Важен интернет, отдельный санузел, тихий этаж."
    ),
    "legal_office": (
        "юридическая практика, ищу офис в центре. "
        "Желательно деловой район, парковка для клиентов."
    ),
    "retail_chain": (
        "представитель торговой сети, рассматриваем расширение. "
        "Важны метраж, трафик, условия расторжения договора."
    ),
    "investor": (
        "инвестор, рассматриваю объект под аренду или перепродажу. "
        "Нужен потенциал, документы, окупаемость."
    ),
    "family_business": (
        "семейный бизнес, ищу небольшое помещение. "
        "Важны цена, договор на длительный срок, лояльный собственник."
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
    """
    if llm_classifier is None or getattr(llm_classifier, "client", None) is None:
        # Без LLM не пишем первым: нет хорошего fallback'а — стандартный
        # шаблон выдаст всех ботов с потрохами.
        return None

    persona_description = PERSONAS.get(persona_id, "")
    formality = random.choice(_FORMALITY_LEVELS)
    length = random.choice(_LENGTH_HINTS)
    approach = random.choice(_APPROACH_HINTS)

    try:
        # Lazy import чтобы не плодить циклические зависимости.
        from llm_classifier import _load_prompt

        system_message = _load_prompt("outbound_first_message.system.txt")
        user_prompt = _load_prompt("outbound_first_message.user.txt").format(
            title=listing.get("title", "—"),
            category=listing.get("category", "коммерческая недвижимость"),
            location=listing.get("location", "—"),
            area=listing.get("area", "—"),
            price=listing.get("price", "не указана"),
            description=(listing.get("description") or "")[:600],
            persona_description=persona_description,
            formality=formality,
            length=length,
            approach=approach,
        )

        response = llm_classifier.client.chat.completions.create(
            model=llm_classifier.model,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.95,  # выше дефолта (0.7) — нам нужна вариативность
        )
        raw = (response.choices[0].message.content or "").strip()
        # Снимаем кавычки, если LLM их зачем-то добавил
        raw = raw.strip('"').strip("'").strip()

        clean, reason = sanitize_llm_reply(raw)
        if clean is None:
            logger.info("outbound: LLM sanitizer отбросил (%s): %r", reason, raw[:100])
            return None
        return clean
    except Exception as exc:
        logger.warning("outbound: generate_first_message failed: %s", exc)
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
        max_per_cycle: int = 2,
        listing_min_age_hours: float = 1.0,
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
        if _stopping() or self.db is None:
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
            if sent_count >= n_to_send or _stopping():
                break

            # Между двумя outbound в одном цикле — длинная human-пауза.
            # Первое сообщение — без паузы, иначе ждём.
            if sent_count > 0:
                _human_delay(
                    self.between_messages_min_sec,
                    self.between_messages_max_sec,
                    stop_event=_tg.stop_event,
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

        # 2. Открываем страницу листинга.
        try:
            self.driver.get(listing_url)
        except WebDriverException as exc:
            log_func(self.account_name, f"H1: driver.get({listing_url}) fail: {exc}")
            return False

        _human_delay(3, 7, stop_event=_tg.stop_event, distribution="normal")

        if _stopping():
            return False

        # 3. Pre-click captcha check.
        if detect_phone_captcha(
            self.driver, log_func=log_func, account_name=self.account_name
        ):
            log_func(self.account_name, "H1: капча на странице листинга — скип.")
            _astate.mark_captcha(self.account_name)
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
        """Клик «Написать сообщение» на странице листинга. Avito имеет
        несколько селекторов в зависимости от верстки. Пробуем по очереди.
        """
        # Попытка 1: главная кнопка «Написать сообщение» в item-contact-bar.
        candidates = [
            "//button[@data-marker='item-contact-bar/message-button']",
            "//a[@data-marker='item-contact-bar/message-button']",
            "//*[@data-marker='item-contact-bar/message-button']",
            "//button[contains(., 'Написать сообщение')]",
            "//a[contains(., 'Написать сообщение')]",
            "//button[contains(., 'Написать')]",
        ]
        for xpath in candidates:
            try:
                btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                # Скроллим чтобы кнопка точно была видна — иначе click перехватится.
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", btn
                )
                _human_delay(0.5, 1.5, stop_event=_tg.stop_event)
                try:
                    btn.click()
                except (ElementClickInterceptedException, StaleElementReferenceException):
                    # Иногда мешает sticky-header — кликаем через JS.
                    self.driver.execute_script("arguments[0].click();", btn)
                _human_delay(2, 4, stop_event=_tg.stop_event)
                return True
            except TimeoutException:
                continue
            except Exception as exc:
                logger.debug("open_chat_overlay candidate %s failed: %s", xpath, exc)

        log_func(self.account_name, "H1: кнопка «Написать» не найдена на листинге.")
        return False

    def _type_and_send(self, text: str, log_func) -> bool:
        """Найти chat-input, ввести текст по-человечески (символы с
        задержками), нажать Send. Avito может рендерить input разными
        способами (textarea / contenteditable / overlay), пробуем варианты.
        """
        input_xpaths = [
            "//textarea[@data-marker='message-input']",
            "//*[@data-marker='message-input']//textarea",
            "//textarea[contains(@placeholder, 'Сообщение')]",
            "//textarea[contains(@placeholder, 'сообщение')]",
            "//div[@contenteditable='true' and (@role='textbox' or contains(@class, 'input'))]",
            "//textarea",
        ]
        input_el = None
        for xpath in input_xpaths:
            try:
                input_el = WebDriverWait(self.driver, 4).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                break
            except TimeoutException:
                continue

        if input_el is None:
            log_func(self.account_name, "H1: chat-input не найден после клика.")
            return False

        try:
            input_el.click()
            _human_delay(0.4, 1.2, stop_event=_tg.stop_event)
        except Exception:
            pass

        # Типим по-человечески: 30-90 ms между символами.
        # А3-style: люди не вставляют 200 символов в input одним click'ом —
        # анти-fingerprinting боится «мгновенно появившийся текст».
        try:
            for ch in text:
                if _stopping():
                    return False
                input_el.send_keys(ch)
                time.sleep(random.uniform(0.03, 0.09))
        except StaleElementReferenceException:
            log_func(self.account_name, "H1: input стал stale во время ввода — скип.")
            return False
        except Exception as exc:
            log_func(self.account_name, f"H1: ошибка ввода текста: {exc}")
            return False

        _human_delay(1.5, 3.5, stop_event=_tg.stop_event)

        # Кнопка Send: data-marker='send-message-button' или icon-кнопка.
        send_xpaths = [
            "//button[@data-marker='send-message-button']",
            "//*[@data-marker='send-message-button']",
            "//button[contains(., 'Отправить')]",
            "//button[@type='submit']",
        ]
        send_el = None
        for xpath in send_xpaths:
            try:
                send_el = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                break
            except TimeoutException:
                continue

        if send_el is None:
            log_func(self.account_name, "H1: кнопка Отправить не найдена.")
            return False

        try:
            send_el.click()
        except (ElementClickInterceptedException, StaleElementReferenceException):
            self.driver.execute_script("arguments[0].click();", send_el)
        except Exception as exc:
            log_func(self.account_name, f"H1: ошибка клика Send: {exc}")
            return False

        _human_delay(2, 5, stop_event=_tg.stop_event)

        # Post-send capture-check: иногда Avito подсовывает капчу сразу
        # после первого outbound — это самый детектируемый момент.
        if detect_phone_captcha(
            self.driver, log_func=log_func, account_name=self.account_name
        ):
            log_func(self.account_name, "H1: КАПЧА после Send — записываем как captcha.")
            _astate.mark_captcha(self.account_name)
            return False

        return True
