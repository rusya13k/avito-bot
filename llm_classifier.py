import json
import logging
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

from database import DatabaseManager
from heuristic_scorer import HeuristicScorer

logger = logging.getLogger(__name__)

# D5: путь к директории с промптами. Файлы хранятся как обычный текст,
# чтобы их можно было редактировать без релиза кода и легко A/B-тестить.
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """
    D5: читает шаблон промпта из prompts/{name}.txt. Кэширует на класс
    LLMClassifier (см. _PROMPT_CACHE), чтобы не открывать файл на каждый
    вызов классификации/ответа.
    """
    cached = _PROMPT_CACHE.get(name)
    if cached is not None:
        return cached
    path = _PROMPTS_DIR / name
    text = path.read_text(encoding="utf-8")
    _PROMPT_CACHE[name] = text
    return text


_PROMPT_CACHE: dict[str, str] = {}


class LLMClassifier:
    """
    Тонкая обёртка над OpenAI-клиентом (>=1.0).

    - Создаёт клиент один раз в __init__.
    - При отсутствии api_key или сетевой ошибке делает fallback на heuristic-скорер,
      который тоже инстанцируется один раз (не пересоздаём DatabaseManager на каждый запрос).
    - Все ошибки LLM логируются (раньше тихо проваливались в except).
    """

    DEFAULT_MODEL = "gpt-3.5-turbo"
    DEFAULT_API_BASE = "https://api.openai.com/v1"
    REQUEST_TIMEOUT = 30.0

    def __init__(
        self,
        config: dict[str, Any],
        db_manager: DatabaseManager | None = None,
        heuristic_scorer: HeuristicScorer | None = None,
    ):
        """
        Args:
            config: dict с ключами api_key / model / api_base
            db_manager: shared DatabaseManager (создастся, если не передан)
            heuristic_scorer: shared HeuristicScorer для fallback-классификации
        """
        self.config = config
        self.api_key = (config.get("api_key") or "").strip()
        self.model = config.get("model") or self.DEFAULT_MODEL
        self.api_base = (config.get("api_base") or self.DEFAULT_API_BASE).strip()

        # Шаринг heuristic_scorer / db_manager на всё время жизни классификатора —
        # вместо создания нового DatabaseManager() в каждом except.
        if heuristic_scorer is not None:
            self._scorer = heuristic_scorer
        else:
            self._scorer = HeuristicScorer(db_manager or DatabaseManager())

        # OpenAI client создаём только если есть ключ.
        self.client: OpenAI | None = None
        if self.api_key:
            try:
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.api_base,
                    timeout=self.REQUEST_TIMEOUT,
                )
            except Exception as exc:  # pragma: no cover — конструктор почти не падает
                logger.warning("Не удалось создать OpenAI-клиент: %s", exc)
                self.client = None

        # E2: db для записи llm_errors-метрики. Используем уже имеющийся
        # db_manager (или достаём через scorer, чтобы не плодить соединения).
        self._db: DatabaseManager | None = db_manager or getattr(self._scorer, "db_manager", None)

    def _incr_llm_error(self, kind: str) -> None:
        """
        E2: best-effort бамп счётчика llm_errors. account_name="" —
        метрика глобальная (LLMClassifier шарится между потоками
        аккаунтов, привязки к конкретному нет).
        """
        if self._db is None:
            return
        try:
            self._db.incr_metric("", "llm_errors")
        except Exception as exc:  # pragma: no cover — не должно влиять на бизнес-флоу
            logger.debug("incr_metric llm_errors (%s) failed: %s", kind, exc)

    # ---------- classification ----------

    def classify_listing(self, listing_data: dict[str, Any]) -> tuple[str, float, str]:
        """
        Возвращает (label, confidence, reason).

        Если ключа нет / клиент недоступен / LLM ошибка / невалидный JSON —
        делаем fallback на эвристику.

        D1: HeuristicScorer.calculate_score теперь возвращает 4-tuple
        (label, confidence, reason, breakdown). Здесь нам breakdown не
        нужен — мы держим узкую сигнатуру 3-tuple для совместимости
        с прежним протоколом. ListingClassifier работает напрямую со
        scorer'ом и breakdown получает там.
        """
        if self.client is None:
            return self._scorer.calculate_score(listing_data)[:3]

        try:
            user_prompt = self._create_classification_prompt(listing_data)
            # D5: input/output логируются на DEBUG для тюнинга промптов
            logger.debug("classify_listing prompt: %s", user_prompt[:300])
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._create_system_message()},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            content = (response.choices[0].message.content or "").strip()
            logger.debug("classify_listing output: %s", content[:300])
        except OpenAIError as exc:
            logger.warning("OpenAI classify_listing failed: %s — fallback на эвристику", exc)
            self._incr_llm_error("classify:openai")
            return self._scorer.calculate_score(listing_data)[:3]
        except Exception as exc:  # сеть / таймаут / proxy
            logger.warning("classify_listing unexpected error: %s — fallback на эвристику", exc)
            self._incr_llm_error("classify:unexpected")
            return self._scorer.calculate_score(listing_data)[:3]

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("LLM вернул не-JSON ответ: %r — fallback на эвристику", content[:200])
            self._incr_llm_error("classify:bad_json")
            return self._scorer.calculate_score(listing_data)[:3]

        label = result.get("label", "uncertain")
        if label not in ("owner", "agent", "uncertain"):
            label = "uncertain"
        try:
            confidence = float(result.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        reason = str(result.get("reason") or "Classified by LLM")
        return label, confidence, reason

    def _create_system_message(self) -> str:
        # D5: промпт хранится в prompts/classify_listing.system.txt
        return _load_prompt("classify_listing.system.txt")

    def _create_classification_prompt(self, listing_data: dict[str, Any]) -> str:
        # D5: шаблон + параметры. .format вместо f-string, потому что
        # шаблон лежит в файле как обычный текст.
        return _load_prompt("classify_listing.user.txt").format(
            title=listing_data.get("title", "") or "",
            description=(listing_data.get("description", "") or "")[:1500],
            seller_name=listing_data.get("seller_name", "") or "",
            phone=listing_data.get("phone", "") or "",
            profile_id=listing_data.get("profile_id", "") or "",
        )

    # ---------- response generation ----------

    def generate_response(self, context: dict[str, Any], chat_history: list[dict[str, Any]]) -> str:
        """
        Генерация ответа клиенту в Avito-чате.
        chat_history — список {'direction': 'in'|'out', 'text': '...'}

        D5: шаблоны system / user — в prompts/. input/output логируются
        на DEBUG-уровне (для последующего тюнинга промптов).
        """
        if self.client is None:
            return "Здравствуйте! Уточните, пожалуйста, ваш вопрос."

        try:
            history_lines = []
            for msg in chat_history or []:
                role = "Клиент" if msg.get("direction") == "in" else "Мы"
                text = (msg.get("text") or "").strip()
                if text:
                    history_lines.append(f"{role}: {text}")
            history_str = "\n".join(history_lines)

            system_message = _load_prompt("generate_response.system.txt")
            prompt = _load_prompt("generate_response.user.txt").format(
                title=context.get("title", "Недвижимость"),
                price=context.get("price", "Не указана"),
                description=(context.get("description") or "")[:500],
                history_str=history_str,
            )

            # D5: лог input для аналитики промптов (DEBUG, чтобы не шуметь)
            logger.debug(
                "generate_response prompt: %s",
                (prompt[:500] + "...") if len(prompt) > 500 else prompt,
            )

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
            )
            out = (response.choices[0].message.content or "").strip()
            logger.debug(
                "generate_response output: %s", (out[:300] + "...") if len(out) > 300 else out
            )
            return out
        except OpenAIError as exc:
            logger.warning("OpenAI generate_response failed: %s", exc)
            self._incr_llm_error("respond:openai")
        except Exception as exc:
            logger.warning("generate_response unexpected error: %s", exc)
            self._incr_llm_error("respond:unexpected")
        return "Здравствуйте! Подскажите, пожалуйста, какой объект вас интересует?"


# Example usage:
# config = {
#     "api_key": os.getenv("OPENAI_API_KEY", ""),
#     "model": "gpt-3.5-turbo",
#     "api_base": "https://api.openai.com/v1",
# }
# classifier = LLMClassifier(config, db_manager=DatabaseManager())
# label, confidence, reason = classifier.classify_listing(listing_data)
