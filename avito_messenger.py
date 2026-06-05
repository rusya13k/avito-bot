import datetime
import hashlib
import logging
import random
import re
import threading
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import tg_bot as _tg
from account_state import account_state as _astate
from human_delay import human_delay as _human_delay
from human_mouse import human_click as _human_click
from human_typing import length_speed_multiplier, persona_speed_multiplier
from human_typing import type_human as _type_human
from llm_sanitizer import sanitize_llm_reply

logger = logging.getLogger(__name__)

# C3-fix: per-(account, dialog) lock предотвращает двойную отправку
_send_locks: dict[tuple[str, str], threading.Lock] = {}
_send_locks_guard = threading.Lock()
# Периодическая очистка _send_locks: храним максимум 1000 ключей,
# старые (неактивные диалоги) удаляем при превышении.
_SEND_LOCKS_MAX = 1000


def _cleanup_send_locks() -> None:
    """Удаляет лишние записи из _send_locks если их слишком много."""
    global _send_locks
    with _send_locks_guard:
        if len(_send_locks) > _SEND_LOCKS_MAX:
            # Оставляем последние добавленные (по логике dict сохраняет insertion order)
            keys = list(_send_locks.keys())
            for k in keys[: len(keys) - _SEND_LOCKS_MAX]:
                del _send_locks[k]


def _parse_message_timestamp(msg_el) -> str | None:
    """Парсит реальный timestamp сообщения из DOM Avito.

    Ищет элемент с data-marker='messenger/message/time' или time-элемент
    рядом с сообщением. Возвращает строку 'YYYY-MM-DD HH:MM:SS' или None.
    """
    # Способ 1: data-marker для timestamp (Avito обычно рендерит время)
    try:
        time_el = msg_el.find_element(By.XPATH, ".//*[@data-marker='messenger/message/time']")
        time_text = time_el.text.strip()
        if time_text:
            return _avito_time_to_iso(time_text)
    except Exception:
        pass

    # Способ 2: <time> элемент с datetime атрибутом
    try:
        time_el = msg_el.find_element(By.XPATH, ".//time")
        dt_attr = time_el.get_attribute("datetime")
        if dt_attr:
            # datetime может быть ISO 8601 или UNIX timestamp
            try:
                dt = datetime.datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass
            # Может быть UNIX timestamp (millis)
            try:
                ts = int(dt_attr)
                if ts > 1e12:
                    ts = ts // 1000
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except (ValueError, TypeError):
                pass
    except Exception:
        pass

    return None


def _avito_time_to_iso(time_text: str) -> str | None:
    """Конвертирует текстовое время Avito в 'YYYY-MM-DD HH:MM:SS'.

    Поддерживаемые форматы:
    - '12:34' → сегодня 12:34
    - 'вчера в 12:34' → вчера 12:34
    - '2 янв в 12:34' → этот год
    - '2 янв 2024 в 12:34' → конкретный год
    """
    now = datetime.datetime.now()

    # 'HH:MM' — сегодня
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_text)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    # 'вчера в HH:MM'
    m = re.match(r"вчера\s+в?\s*(\d{1,2}):(\d{2})", time_text, re.IGNORECASE)
    if m:
        yesterday = now - datetime.timedelta(days=1)
        yesterday = yesterday.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
        return yesterday.strftime("%Y-%m-%d %H:%M:%S")

    # '2 янв в 12:34' или '2 янв 2024 в 12:34'
    months_ru = {
        "янв": 1,
        "фев": 2,
        "мар": 3,
        "апр": 4,
        "мая": 5,
        "июн": 6,
        "июл": 7,
        "авг": 8,
        "сен": 9,
        "окт": 10,
        "ноя": 11,
        "дек": 12,
        "май": 5,
    }
    m = re.match(
        r"(\d{1,2})\s+(\w{3})\s+(\d{4})?\s*в?\s*(\d{1,2}):(\d{2})", time_text, re.IGNORECASE
    )
    if m:
        day = int(m.group(1))
        month = months_ru.get(m.group(2).lower()[:3])
        year = int(m.group(3)) if m.group(3) else now.year
        hour, minute = int(m.group(4)), int(m.group(5))
        if month:
            try:
                return datetime(year, month, day, hour, minute, 0).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

    return None


def _get_send_lock(account_name: str, dialog_id: str) -> threading.Lock:
    """C3-fix: возвращает lock для конкретного (account, dialog) — предотвращает двойную отправку."""
    key = (account_name, dialog_id)
    with _send_locks_guard:
        # Периодическая очистка: при >500 локов удаляем для остановленных аккаунтов
        # и обрезаем до _SEND_LOCKS_MAX
        if len(_send_locks) > 500:
            for k in list(_send_locks.keys()):
                if _tg.is_stop_requested(k[0]):
                    del _send_locks[k]
            _cleanup_send_locks()
        if key not in _send_locks:
            _send_locks[key] = threading.Lock()
        return _send_locks[key]


def _stopping(account_name: str) -> bool:
    """Was a global stop requested via the Telegram controller?"""
    return _tg.is_stop_requested(account_name)


def _wf(driver, xpath, timeout=3):
    """
    C5: короткий WebDriverWait вместо голого driver.find_element.
    Без него страница после клика не успевает прогрузиться -> мы попадаем
    в except и теряем реальные данные (или падаем на send-button, который
    ещё не отрендерен).
    """
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))


# B2: human-like delay (normal distribution + stop_event-aware).
# Реализация в human_delay.py (импортирован выше).


def hp(lo=0.5, hi=1.5, *, distribution="normal", stop_event=None):
    """
    Backward-compatible wrapper: старые `hp(lo, hi)` теперь идут через
    human_delay с распределением normal и проверкой stop_event.
    """
    return _human_delay(lo, hi, distribution=distribution, stop_event=stop_event)


_AVITO_CHANNEL_RE = re.compile(r"/messenger/(?:channel/|c\d*/?|\?channelId=)?([A-Za-z0-9._-]{6,})")


def _extract_avito_user_uid_from_dom(dialog_el):
    """
    Пытается достать стабильный user-id визитёра из элемента диалога.
    """
    if dialog_el is None:
        return None
    for attr in ("data-uid", "data-user-id", "data-uid-from", "data-channel-id"):
        try:
            val = dialog_el.get_attribute(attr)
            if val:
                return val.strip()
        except Exception:
            continue
    try:
        link = dialog_el.find_element(
            By.XPATH, ".//a[contains(@href, '/user/') or contains(@href, '/brands/')]"
        )
        href = link.get_attribute("href") or ""
        m = re.search(r"/user/([^/?]+)", href)
        if m:
            return f"user:{m.group(1)}"
    except Exception:
        pass
    try:
        avatar = dialog_el.find_element(
            By.XPATH, ".//img[contains(@src, 'avito.st') or contains(@src, 'avatar')]"
        )
        src = avatar.get_attribute("src") or ""
        if src:
            return f"avatar:{hashlib.sha1(src.encode('utf-8')).hexdigest()[:16]}"
    except Exception:
        pass
    return None


def _extract_channel_id_from_driver(driver):
    try:
        url = driver.current_url or ""
    except Exception:
        return None
    m = _AVITO_CHANNEL_RE.search(url)
    if m:
        return f"channel:{m.group(1)}"
    return None


def build_visitor_id(*, dom_uid=None, channel_id=None, visitor_name=None, listing_id=None) -> str:
    if channel_id:
        return channel_id
    if dom_uid:
        return dom_uid
    if visitor_name and listing_id is not None:
        return f"name:{visitor_name}|lid:{listing_id}"
    if visitor_name:
        return f"name:{visitor_name}"
    return "unknown"


def human_type(element, text, *, speed_multiplier: float = 1.0, stop_event=None) -> bool:
    """T5: реалистичный typing с бёрстами + опечатками (через human_typing).

    Сохраняет старый bool-контракт: True = допечатало, False = прервано stop_event.

    Args:
        speed_multiplier: множитель задержек (persona-driven). 1.0 = default.
    """
    return _type_human(
        element,
        text,
        speed_range=(0.05, 0.20),
        speed_multiplier=speed_multiplier,
        enable_typos=True,
        stop_event=stop_event,
    )


# F11: Variable number of dialogs to process per messenger-cycle.
# Веса смещены к 1-3 (короткие сессии), редко 5-7 (зашёл «прочитать всё»).
# Индекс = число диалогов; weights[0] = 0 (никогда не «0 диалогов» —
# в этой ветке мы уже знаем что хотя бы один диалог найден).
_DIALOG_COUNT_WEIGHTS = [0, 0.30, 0.25, 0.20, 0.10, 0.08, 0.05, 0.02]  # idx 0..7


def _pick_dialog_count(available: int) -> int:
    """F11: возвращает число диалогов для обработки в этой сессии.

    available — сколько диалогов реально найдено на странице мессенджера.
    Возвращаемое значение всегда ≥ 1 (если available ≥ 1) и ≤ available.

    Распределение из _DIALOG_COUNT_WEIGHTS, обрезанное до доступного
    диапазона. Если available > 7 — обрабатываем максимум 7 за цикл (длинный
    хвост распределения отсекаем; на следующем цикле снова бросок).
    """
    if available <= 0:
        return 0
    effective_max = min(available, len(_DIALOG_COUNT_WEIGHTS) - 1)
    weights = _DIALOG_COUNT_WEIGHTS[: effective_max + 1]
    n = random.choices(range(len(weights)), weights=weights)[0]
    return max(1, n)  # гарантируем ≥ 1


class AvitoMessenger:
    def __init__(
        self,
        driver,
        wait,
        db_manager,
        llm_classifier,
        account_name,
        *,
        # F5: реалистичные задержки ответа. По умолчанию: НИЖНИЙ порог 15 мин,
        # верхний 600 мин (10 часов), lognormal(2.5, 1.0) — mean ~30 мин,
        # тяжёлый правый хвост. Если все нули — F5 эффективно выключен.
        min_reply_age_min: float = 15.0,
        max_reply_age_min: float = 600.0,
        reply_delay_mu: float = 2.5,
        reply_delay_sigma: float = 1.0,
        # F5b: 5% шанс «никогда не отвечать» — только для НОВЫХ диалогов
        # (где у нас ещё нет ни одного out-сообщения). Имитирует «увидел,
        # неинтересно, проигнорил». State хранится in-memory в account_state
        # и сбрасывается при рестарте — это допустимо.
        ignore_new_dialog_chance: float = 0.05,
        # T5: persona — для подбора темпа печати (молодой/инвестор/...).
        # None / неизвестная persona → multiplier 1.0 (нейтрально).
        persona: str | None = None,
    ):
        self.driver = driver
        self.wait = wait
        self.db = db_manager
        self.llm = llm_classifier
        self.account_name = account_name

        self.min_reply_age_min = float(min_reply_age_min)
        self.max_reply_age_min = float(max_reply_age_min)
        self.reply_delay_mu = float(reply_delay_mu)
        self.reply_delay_sigma = float(reply_delay_sigma)
        self.ignore_new_dialog_chance = float(ignore_new_dialog_chance)
        # T5: persona-driven typing speed multiplier.
        self.persona = persona
        self.typing_speed_multiplier = persona_speed_multiplier(persona)
        # Fix: per-(dialog_id, text) cached lognormal target — не перебрасываем
        # кости каждый цикл, гарантируя сходимость ответа.
        self._reply_targets: dict[tuple[int, str], float] = {}

    def process_messages(self, log_func):
        """Main entry point for processing new messages"""
        if _stopping(self.account_name):
            return
        log_func(self.account_name, "Checking Avito messages...")
        try:
            # J2: avoid full reload if already on messenger page
            current_url = ""
            try:
                current_url = (self.driver.current_url or "").lower()
            except Exception:
                pass

            if "/profile/messenger" not in current_url:
                self.driver.get("https://www.avito.ru/profile/messenger")
            else:
                # Already on messenger — soft refresh: click reload or press F5
                log_func(self.account_name, "  J2: already on messenger, soft refresh")
                try:
                    from selenium.webdriver.common.keys import Keys

                    self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.F5)
                except Exception:
                    # Fallback: small JS reload
                    self.driver.execute_script("window.location.reload()")
                hp(1, 3, stop_event=_tg.get_account_stop_event(self.account_name))

            # Wait for dialogs to load
            try:
                self.wait.until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//*[@data-marker='messenger/chat-item']")
                    )
                )
            except TimeoutException:
                log_func(self.account_name, "No dialogs found or messenger didn't load.")
                return

            # Find all unread dialogs or just all dialogs to check
            dialogs = self.driver.find_elements(By.XPATH, "//*[@data-marker='messenger/chat-item']")
            log_func(self.account_name, f"Found {len(dialogs)} dialogs.")

            # F11: Variable number of dialogs to process per cycle.
            # Реальный пользователь не всегда читает первые 5 подряд: часто
            # 1-3 (короткая сессия) и редко 5-7 (зашёл «прочитать всё»).
            # Веса: пик на 1-3, длинный хвост до 7.
            n_dialogs = _pick_dialog_count(len(dialogs))
            log_func(self.account_name, f"F11: planning to process {n_dialogs} dialog(s).")

            for i in range(n_dialogs):
                if _stopping(self.account_name):
                    log_func(self.account_name, "Stop requested — aborting messenger loop.")
                    return
                # Re-find dialogs as the page might refresh
                dialogs = self.driver.find_elements(
                    By.XPATH, "//*[@data-marker='messenger/chat-item']"
                )
                if i >= len(dialogs):
                    break

                dialog = dialogs[i]

                # Check if it's unread (usually has a specific class or indicator)
                is_unread = False
                try:
                    unread_indicator = dialog.find_elements(
                        By.XPATH,
                        ".//*[contains(@class, 'messenger-chat-item-unread')] | .//span[contains(@class, 'badge')]",
                    )
                    if unread_indicator:
                        is_unread = True
                except Exception:
                    pass

                if not is_unread:
                    # Optional: skip already read dialogs to save time
                    # continue
                    pass

                # Extract visitor name and last message
                visitor_name = "Unknown"
                try:
                    visitor_name = dialog.find_element(
                        By.XPATH, ".//*[@data-marker='messenger/chat-item/user-name']"
                    ).text
                except Exception:
                    pass
                # last-message-snippet — сохраняем в отдельную переменную,
                # чтобы исключение не обнулило visitor_name
                _last_msg_snippet = ""
                try:
                    _last_msg_snippet = dialog.find_element(
                        By.XPATH, ".//*[@data-marker='messenger/chat-item/last-message']"
                    ).text
                except Exception:
                    pass

                # B3: пытаемся вытащить устойчивый ID визитёра из самой карточки
                # ДО клика, на случай если после клика DOM перерендерится.
                dom_uid = _extract_avito_user_uid_from_dom(dialog)

                log_func(
                    self.account_name,
                    f"Processing dialog with {visitor_name} (dom_uid={dom_uid or '-'})...",
                )

                # T6: human-like click по карточке диалога вместо JS-телепорта.
                _human_click(
                    self.driver, dialog, stop_event=_tg.get_account_stop_event(self.account_name)
                )
                hp(2, 4, stop_event=_tg.get_account_stop_event(self.account_name))

                # После клика URL может содержать channel_id — это самый
                # стабильный идентификатор, поэтому собираем visitor_id
                # уже внутри _handle_current_chat.
                self._handle_current_chat(visitor_name, log_func, dom_uid=dom_uid)
                # E2: считаем КАЖДЫЙ обработанный диалог (даже если в нём
                # ничего не отправили). messages_sent — отдельная метрика,
                # инкрементится только при успешном _send_message.
                try:
                    self.db.incr_metric(self.account_name, "dialogs_handled")
                except Exception as exc:
                    logger.debug("incr_metric dialogs_handled failed: %s", exc)

        except Exception as e:
            log_func(self.account_name, f"Error in process_messages: {str(e)}")

    def _handle_current_chat(self, visitor_name, log_func, dom_uid=None):
        """Processes the currently opened chat window"""
        if _stopping(self.account_name):
            return
        try:
            # Try to identify the listing this chat is about
            listing_id = None
            listing_title = "Unknown Object"
            listing_description = ""
            listing_price = "Не указана"

            try:
                # C5: chat-header дорисовывается после клика по диалогу,
                # без wait почти всегда падали в except и теряли listing_url.
                listing_elem = _wf(
                    self.driver, "//*[@data-marker='messenger/chat-header/item-title']", timeout=4
                )
                listing_title = listing_elem.text
                listing_url = listing_elem.get_attribute("href")

                # Try to find listing in DB by URL
                listing = self.db.get_listing_by_url(listing_url)
                if listing:
                    listing_id = listing["id"]
                    listing_description = listing.get("description", "")
                    listing_price = listing.get("price", "Не указана")
            except Exception:
                pass

            # B3: устойчивый visitor_id (channel_id из URL > dom_uid > composite > имя).
            channel_id = _extract_channel_id_from_driver(self.driver)
            visitor_id = build_visitor_id(
                channel_id=channel_id,
                dom_uid=dom_uid,
                visitor_name=visitor_name,
                listing_id=listing_id,
            )
            log_func(self.account_name, f"  visitor_id resolved: {visitor_id}")

            # Extract chat history
            messages_elements = self.driver.find_elements(
                By.XPATH, "//*[@data-marker='messenger/message']"
            )
            chat_history = []

            # Save dialog to DB
            dialog_id = self.db.upsert_dialog(
                our_account=self.account_name,
                visitor_id=visitor_id,
                listing_id=listing_id,
                status="active",
                last_message_text="",
                last_message_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            )

            for msg_el in messages_elements:
                try:
                    # Determine direction (in/out)
                    marker = msg_el.get_attribute("data-marker")
                    is_out = "out" in marker if marker else False
                    direction = "out" if is_out else "in"
                    text = msg_el.find_element(
                        By.XPATH, ".//*[@data-marker='messenger/message/text']"
                    ).text
                    # Парсим реальный timestamp из DOM (Avito показывает время
                    # рядом с сообщением). Fallback на server time если не найден.
                    timestamp = _parse_message_timestamp(msg_el) or time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                    chat_history.append({"direction": direction, "text": text})

                    # Save message to DB
                    self.db.add_message(dialog_id, direction, text, timestamp)
                except Exception:
                    continue

            if not chat_history:
                return

            # Update dialog with last message info
            self.db.upsert_dialog(
                our_account=self.account_name,
                visitor_id=visitor_id,
                listing_id=listing_id,
                status="active",
                last_message_text=chat_history[-1]["text"],
                last_message_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            )

            # Re-check stop before any outgoing action.
            if _stopping(self.account_name):
                return

            # Check if the last message is from the visitor
            if chat_history[-1]["direction"] == "in":
                log_func(
                    self.account_name,
                    f"Last message from {visitor_name} is: {chat_history[-1]['text']}",
                )

                # ── F5: реалистичная задержка ответа ─────────────────────
                # Реальный пользователь не отвечает мгновенно после прихода
                # пуша (5-30 мин — увидел; 1-4 ч — частая задержка). Бот же
                # сейчас отвечает в том же messenger-цикле — это паттерн.
                # Откладываем отправку до следующего цикла, если возраст
                # in-сообщения не превысил случайно выбранный target.
                if not self._should_reply_now(dialog_id, chat_history, log_func):
                    return

                # Use LLM to generate response
                context = {
                    "title": listing_title,
                    "price": listing_price,
                    "description": listing_description,
                    "dialog_id": dialog_id,  # J1: для LLM-кэша
                    "persona": self.persona,  # J1: для LLM-кэша
                }

                response_text = self.llm.generate_response(context, chat_history)

                # K2: фильтр исходящих ответов перед отправкой в Avito-чат.
                # Блокирует ответы с телефонами / @telegram / wa.me /
                # email / слишком короткие/длинные. Если LLM (или prompt-
                # injection из листинга) попытается «увести» клиента из
                # Avito — этот цикл пропустим, на следующем проходе LLM
                # с большой вероятностью даст безопасный ответ.
                clean_text, block_reason = sanitize_llm_reply(response_text)
                if clean_text is None:
                    logger.warning(
                        "[%s] LLM response blocked (reason=%s, len=%d): %r",
                        self.account_name,
                        block_reason,
                        len(response_text or ""),
                        (response_text or "")[:120],
                    )
                    log_func(
                        self.account_name,
                        f"K2: LLM-ответ заблокирован ({block_reason}) — пропускаю отправку.",
                    )
                    # Метрика для /report и health-мониторинга.
                    try:
                        self.db.incr_metric(self.account_name, "llm_response_blocked")
                    except Exception as exc:
                        logger.debug("incr_metric llm_response_blocked failed: %s", exc)
                    return

                response_text = clean_text  # очищенный (стрипнутый) текст
                log_func(self.account_name, f"Generated response: {response_text}")

                if _stopping(self.account_name):
                    log_func(self.account_name, "Stop requested before send — skipping.")
                    return

                # C3-fix: lock на (account, dialog) — предотвращает двойную отправку
                # при быстром перезапуске цикла.
                send_lock = _get_send_lock(self.account_name, str(dialog_id))
                if not send_lock.acquire(blocking=False):
                    log_func(self.account_name, "C3: send already in progress — skipping.")
                    return
                try:
                    # Send the response
                    sent_ok = self._send_message(response_text, log_func)
                finally:
                    send_lock.release()

                if sent_ok:
                    # C4: запись факта отправки и обновление last_message
                    # диалога — в одной транзакции. Если краш произойдёт
                    # между этими двумя шагами, оба откатятся вместе, и
                    # на следующей итерации мы перечитаем чат через DOM
                    # (где наше сообщение уже видно благодаря Avito) и
                    # проставим всё корректно.
                    now = time.strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        with self.db.transaction() as cur:
                            self.db.add_message(
                                dialog_id,
                                "out",
                                response_text,
                                now,
                                cursor=cur,
                            )
                            self.db.upsert_dialog(
                                our_account=self.account_name,
                                visitor_id=visitor_id,
                                listing_id=listing_id,
                                status="active",
                                last_message_text=response_text,
                                last_message_time=now,
                                cursor=cur,
                            )
                            # E2: метрику отправки кладём в ту же транзакцию,
                            # чтобы счётчик и сама запись об out-сообщении
                            # были консистентны.
                            self.db.incr_metric(
                                self.account_name,
                                "messages_sent",
                                cursor=cur,
                            )
                    except Exception as db_exc:
                        # Сообщение в Avito уже ушло (sent_ok=True), а БД
                        # обновить не удалось. add_message идемпотентен по
                        # (dialog_id, direction, text, timestamp), поэтому
                        # на следующей итерации запись восстановится при
                        # повторном чтении чата.
                        log_func(self.account_name, f"DB update after send failed: {db_exc}")
                    log_func(self.account_name, "Response sent.")
                else:
                    log_func(self.account_name, "Response NOT sent (stop or send error).")
            else:
                log_func(self.account_name, "Last message is from us. Waiting for reply.")

        except Exception as e:
            log_func(self.account_name, f"Error in _handle_current_chat: {str(e)}")

    def _should_reply_now(self, dialog_id, chat_history, log_func) -> bool:
        """
        F5: решает, отвечать ли в ТЕКУЩЕМ messenger-цикле или отложить.

        Логика:
           1. Если диалог в `account_state.ignored_dialogs` (legacy — больше
              не добавляем, но старые записи ещё могут быть) — снимаем ignore
              и отвечаем.
           2. Возраст последнего in-сообщения. Бросаем lognormal target_age
              (clamped к [min_reply_age_min, max_reply_age_min]).
              Если возраст меньше target — пропускаем цикл (на следующем
              бросок будет другой; статистически среднее задержки ≈ mean
              lognormal'a).

        Returns:
            True — отвечать сейчас. False — отложить (return из caller'а).
        """
        # 1) Legacy ignored dialogs — снимаем пометку и продолжаем (ignore отключён).
        if _astate.is_dialog_ignored(self.account_name, dialog_id):
            _astate.unignore_dialog(self.account_name, dialog_id)
            log_func(
                self.account_name,
                f"F5b: dialog#{dialog_id} снят ignore — отвечаем.",
            )

        # 2) Реалистичная lognormal-задержка.
        last_in_text = chat_history[-1]["text"]
        age_seconds = self.db.get_first_in_message_age_seconds(dialog_id, last_in_text)
        if age_seconds is None:
            # Если БД не знает первого появления — отвечаем (fallback к старому
            # поведению, не блокируем коммуникацию).
            return True
        age_min = age_seconds / 60.0

        # Fix: кешируем target для (dialog_id, text) — не перебрасываем кости
        # каждый цикл, гарантируя что ответ рано или поздно будет отправлен.
        cache_key = (int(dialog_id), last_in_text)
        if cache_key not in self._reply_targets:
            self._reply_targets[cache_key] = max(
                self.min_reply_age_min,
                min(
                    self.max_reply_age_min,
                    random.lognormvariate(self.reply_delay_mu, self.reply_delay_sigma),
                ),
            )
        target_min = self._reply_targets[cache_key]

        if age_min < target_min:
            log_func(
                self.account_name,
                f"F5: in-сообщение {age_min:.1f} мин назад "
                f"(target={target_min:.1f} мин) — отложено до следующего цикла.",
            )
            return False
        return True

    def _send_message(self, text, log_func=None):
        """Types and sends a message in the current chat. Returns True on success."""
        try:
            if _stopping(self.account_name):
                return False
            input_box = self.wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[@data-marker='messenger/input-field']")
                )
            )

            # C4-fix: ВСЕГДА очищать input field перед вводом — Avito может
            # предзаполнить его айсбрейкером, или остался текст от предыдущей
            # неудачной отправки. Без очистки сообщение обрезается.
            try:
                input_box.send_keys(Keys.CONTROL + "a")
                input_box.send_keys(Keys.DELETE)
                hp(0.2, 0.4)
            except Exception:
                pass

            # T6: human-like focus вместо одиночного click без mousemove.
            _human_click(
                self.driver, input_box, stop_event=_tg.get_account_stop_event(self.account_name)
            )
            hp(0.5, 1)
            # T5: persona-driven typing speed (молодой быстрее, инвестор медленнее).
            # + адаптивный множитель по длине сообщения (короткие — быстрее).
            effective_multiplier = self.typing_speed_multiplier * length_speed_multiplier(text)
            if not human_type(input_box, text, speed_multiplier=effective_multiplier):
                # Stop signaled mid-typing — do NOT send a half-typed message.
                # C4-fix: очистить поле чтобы не оставлять мусор.
                try:
                    input_box.send_keys(Keys.CONTROL + "a")
                    input_box.send_keys(Keys.DELETE)
                except Exception:
                    pass
                return False
            hp(0.5, 1)

            if _stopping(self.account_name):
                # C4-fix: очистить поле при stop mid-send.
                try:
                    input_box.send_keys(Keys.CONTROL + "a")
                    input_box.send_keys(Keys.DELETE)
                except Exception:
                    pass
                return False

            # C5: кнопка send иногда отрендерена не сразу после ввода текста.
            send_btn = _wf(self.driver, "//*[@data-marker='messenger/send-button']", timeout=3)
            # T6: human-like click по Send.
            _human_click(
                self.driver, send_btn, stop_event=_tg.get_account_stop_event(self.account_name)
            )
            hp(1, 2)
            return True
        except Exception as e:
            msg = f"Failed to send message: {e}"
            if log_func is not None:
                log_func(self.account_name, msg)
            else:
                logger.warning("[%s] %s", self.account_name, msg)
            return False
