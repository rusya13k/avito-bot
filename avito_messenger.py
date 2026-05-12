import hashlib
import logging
import random
import re
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import tg_bot as _tg
from human_delay import human_delay as _human_delay

logger = logging.getLogger(__name__)


def _stopping() -> bool:
    """Was a global stop requested via the Telegram controller?"""
    return _tg.stop_event.is_set()


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


def hp(lo=0.5, hi=1.5, *, distribution="normal"):
    """
    Backward-compatible wrapper: старые `hp(lo, hi)` теперь идут через
    human_delay с распределением normal и проверкой stop_event.
    """
    return _human_delay(lo, hi, distribution=distribution, stop_event=_tg.stop_event)


# B3: устойчивая идентификация визитёра (раньше visitor_id = visitor_name —
# два визитёра с одинаковыми ФИО склеивались в один диалог).

# Регексп выдёргивает ID канала/диалога из URL вида:
#   /profile/messenger/channel/abc123def
#   /profile/messenger/?channelId=abc123
#   /profile/messenger/<channel_id>?...
_AVITO_CHANNEL_RE = re.compile(r"/messenger/(?:channel/|c\d*/?|\?channelId=)?([A-Za-z0-9._-]{6,})")


def _extract_avito_user_uid_from_dom(dialog_el):
    """
    Пытается достать стабильный user-id визитёра из элемента диалога.
    Avito может использовать разные атрибуты — пробуем по очереди.

    Returns: str ID или None.
    """
    if dialog_el is None:
        return None
    # 1) data-* атрибуты на самом dialog-элементе
    for attr in ("data-uid", "data-user-id", "data-uid-from", "data-channel-id"):
        try:
            val = dialog_el.get_attribute(attr)
            if val:
                return val.strip()
        except Exception:
            continue
    # 2) Ссылка на профиль внутри карточки
    try:
        link = dialog_el.find_element(
            By.XPATH, ".//a[contains(@href, '/user/') or contains(@href, '/brands/')]"
        )
        href = link.get_attribute("href") or ""
        # /user/<uid>/profile -> uid
        m = re.search(r"/user/([^/?]+)", href)
        if m:
            return f"user:{m.group(1)}"
    except Exception:
        pass
    # 3) Аватар (src часто содержит стабильный хэш user-id)
    try:
        avatar = dialog_el.find_element(
            By.XPATH, ".//img[contains(@src, 'avito.st') or contains(@src, 'avatar')]"
        )
        src = avatar.get_attribute("src") or ""
        # хэшируем src — он стабилен на сервере, но длинный
        if src:
            return f"avatar:{hashlib.sha1(src.encode('utf-8')).hexdigest()[:16]}"
    except Exception:
        pass
    return None


def _extract_channel_id_from_driver(driver):
    """Берёт channel_id из текущего URL открытого чата, если он есть."""
    try:
        url = driver.current_url or ""
    except Exception:
        return None
    m = _AVITO_CHANNEL_RE.search(url)
    if m:
        return f"channel:{m.group(1)}"
    return None


def build_visitor_id(*, dom_uid=None, channel_id=None, visitor_name=None, listing_id=None) -> str:
    """
    Возвращает устойчивый visitor_id (B3).

    Приоритет:
        1) channel_id (стабильный со стороны Avito)
        2) dom_uid (data-uid / user-href / avatar-hash)
        3) composite: visitor_name + listing_id (slabый fallback, но лучше
           чем чистое имя — два визитёра с одним ФИО, но по разным листингам
           уже не склеятся).
        4) visitor_name только как последняя соломинка.

    Никогда не возвращает None — всегда строка (для UNIQUE-индексов в БД).
    """
    if channel_id:
        return channel_id
    if dom_uid:
        return dom_uid
    if visitor_name and listing_id is not None:
        return f"name:{visitor_name}|lid:{listing_id}"
    if visitor_name:
        return f"name:{visitor_name}"
    return "unknown"


def human_type(element, text):
    """Type text character-by-character; abort early on global stop."""
    for ch in text:
        if _stopping():
            return False
        element.send_keys(ch)
        if random.random() < 0.05:
            time.sleep(random.uniform(0.3, 0.7))
        else:
            time.sleep(random.uniform(0.05, 0.20))
    return True


class AvitoMessenger:
    def __init__(self, driver, wait, db_manager, llm_classifier, account_name):
        self.driver = driver
        self.wait = wait
        self.db = db_manager
        self.llm = llm_classifier
        self.account_name = account_name

    def process_messages(self, log_func):
        """Main entry point for processing new messages"""
        if _stopping():
            return
        log_func(self.account_name, "Checking Avito messages...")
        try:
            self.driver.get("https://www.avito.ru/profile/messenger")
            hp(3, 5)

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

            # For simplicity, we'll process the first 5 dialogs
            for i in range(min(5, len(dialogs))):
                if _stopping():
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
                except:
                    pass

                if not is_unread:
                    # Optional: skip already read dialogs to save time
                    # continue
                    pass

                # Extract visitor name and last message
                try:
                    visitor_name = dialog.find_element(
                        By.XPATH, ".//*[@data-marker='messenger/chat-item/user-name']"
                    ).text
                    # last-message-snippet не нужен в логике, но и не делаем
                    # лишнего find_element без причины — оставляем no-op
                    # на случай, если позже захотим логировать.
                    _ = dialog.find_element(
                        By.XPATH, ".//*[@data-marker='messenger/chat-item/last-message']"
                    ).text
                except:
                    visitor_name = "Unknown"

                # B3: пытаемся вытащить устойчивый ID визитёра из самой карточки
                # ДО клика, на случай если после клика DOM перерендерится.
                dom_uid = _extract_avito_user_uid_from_dom(dialog)

                log_func(
                    self.account_name,
                    f"Processing dialog with {visitor_name} (dom_uid={dom_uid or '-'})...",
                )

                # Click on the dialog
                self.driver.execute_script("arguments[0].click();", dialog)
                hp(2, 4)

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
        if _stopping():
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
            except:
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
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")  # Real timestamp harder to get

                    chat_history.append({"direction": direction, "text": text})

                    # Save message to DB
                    self.db.add_message(dialog_id, direction, text, timestamp)
                except:
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
            if _stopping():
                return

            # Check if the last message is from the visitor
            if chat_history[-1]["direction"] == "in":
                log_func(
                    self.account_name,
                    f"Last message from {visitor_name} is: {chat_history[-1]['text']}",
                )

                # Use LLM to generate response
                context = {
                    "title": listing_title,
                    "price": listing_price,
                    "description": listing_description,
                    "visitor_name": visitor_name,
                }

                response_text = self.llm.generate_response(context, chat_history)
                if not response_text or not response_text.strip():
                    log_func(self.account_name, "LLM returned empty response — skipping send.")
                    return
                log_func(self.account_name, f"Generated response: {response_text}")

                if _stopping():
                    log_func(self.account_name, "Stop requested before send — skipping.")
                    return

                # Send the response
                sent_ok = self._send_message(response_text, log_func)

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

    def _send_message(self, text, log_func=None):
        """Types and sends a message in the current chat. Returns True on success."""
        try:
            if _stopping():
                return False
            input_box = self.wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[@data-marker='messenger/input-field']")
                )
            )
            input_box.click()
            hp(0.5, 1)
            if not human_type(input_box, text):
                # Stop signaled mid-typing — do NOT send a half-typed message.
                return False
            hp(0.5, 1)

            if _stopping():
                return False

            # C5: кнопка send иногда отрендерена не сразу после ввода текста.
            send_btn = _wf(self.driver, "//*[@data-marker='messenger/send-button']", timeout=3)
            send_btn.click()
            hp(1, 2)
            return True
        except Exception as e:
            msg = f"Failed to send message: {e}"
            if log_func is not None:
                log_func(self.account_name, msg)
            else:
                logger.warning("[%s] %s", self.account_name, msg)
            return False
