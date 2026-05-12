# Add these imports at the top of the file
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# F4: telebot-импорты добавлены, чтобы snippet был валидным Python и
# проходил линтер. Сам файл — заметка для интеграции в TelegramController.
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: F401

# Add these new callback handlers to the TelegramController class

# Add to the kb_main() function:
# InlineKeyboardButton("🔍 Классификация", callback_data="classification_menu"),


def kb_classification() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        InlineKeyboardButton("🔄 Переклассифицировать базу", callback_data="reclassify_all"),
        InlineKeyboardButton("📊 Статистика классификации", callback_data="classification_stats"),
        InlineKeyboardButton("📋 Разметка 50 объявлений", callback_data="create_ground_truth"),
        InlineKeyboardButton("📈 Оценка качества", callback_data="evaluate_quality"),
        InlineKeyboardButton("◀️ Назад", callback_data="menu_main"),
    )
    return m


# Add to the _setup method in the TelegramController class:

# Add classification menu handler
#             elif d == "classification_menu":
#                 self._show_classification(cid, call.message)

# Add to the callback handlers:
#             elif d == "classification_menu":
#                 self._show_classification(cid, call.message)

# Add these new methods to the TelegramController class:


def _show_classification(self, chat_id, edit_msg=None):
    text = "🔍 Классификация объявлений"
    if edit_msg:
        try:
            self.bot.edit_message_text(
                text, edit_msg.chat.id, edit_msg.message_id, reply_markup=kb_classification()
            )
            return
        except Exception:
            pass
    self._send(chat_id, text, kb_classification())


# Add to the callback handler section:

#             elif d == "reclassify_all":
#                 # Import the classifier
#                 try:
#                     db_manager = DatabaseManager()
#                     llm_config = {
#                         "api_key": self._cfg().get("openai_api_key", ""),
#                         "model": self._cfg().get("openai_model", "gpt-3.5-turbo")
#                     }
#                     classifier = ListingClassifier(db_manager, llm_config)
#                     results = classifier.classify_all_listings()
#                     text = (f"Переклассификация завершена:\n"
#                            f"Всего обработано: {results['total_processed']}\n"
#                            f"Собственники: {results['owners']}\n"
#                            f"Агенты: {results['agents']}\n"
#                            f"Неопределенные: {results['uncertain']}")
#                     self._send(cid, text, kb_classification())
#                 except Exception as e:
#                     self._send(cid, f"Ошибка переклассификации: {str(e)}", kb_classification())

#             elif d == "classification_stats":
#                 try:
#                     db_manager = DatabaseManager()
#                     # Get classification statistics
#                     conn = db_manager.conn
#                     cursor = conn.cursor()
#                     cursor.execute("SELECT classification, COUNT(*) as count FROM listings WHERE classification IS NOT NULL GROUP BY classification")
#                     results = cursor.fetchall()
#                     stats = {row[0]: row[1] for row in results}
#                     total = sum(count for _, count in results)
#                     text = f"📊 Статистика классификации:\n"
#                     for classification, count in stats.items():
#                         text += f"{classification}: {count}\n"
#                     text += f"Всего: {total}"
#                     self._send(cid, text, kb_classification())
#                 except Exception as e:
#                     self._send(cid, f"Ошибка получения статистики: {str(e)}", kb_classification())

#             elif d == "create_ground_truth":
#                 # This would trigger the creation of ground truth data
#                 self._send(cid, "Создание разметки 50 объявлений...", kb_classification())

#             elif d == "evaluate_quality":
#                 # This would run the evaluation script
#                 self._send(cid, "Оценка качества классификации...", kb_classification())
