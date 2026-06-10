import json
import logging
from pathlib import Path
from typing import Any

import requests

from database import DatabaseManager
from heuristic_scorer import HeuristicScorer

logger = logging.getLogger(__name__)


def _escape_format(text: object) -> str:
    """Экранирует фигурные скобки в пользовательских данных перед .format().
    Предотвращает Prompt Injection через {0}, {__class__} и т.д.
    """
    s = str(text)
    return s.replace("{", "{{").replace("}", "}}")


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
    Обёртка над LLM API с поддержкой двух форматов:
    - OpenAI-совместимый (/v1/chat/completions) — для GPT, Mistral, DeepSeek и т.д.
    - Anthropic Messages (/v1/messages) — для Claude через vibecode и аналоги.

    Авто-детект: если model содержит "claude" → Anthropic формат, иначе OpenAI.
    """

    DEFAULT_MODEL = "gpt-5.5"
    DEFAULT_API_BASE = "https://api.coda.ink/v1"
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
        self.api_base = (config.get("api_base") or self.DEFAULT_API_BASE).strip().rstrip("/")

        # Авто-детект: Claude модели → Anthropic Messages API
        # Также поддерживает Ollama/vLLM с "claude" в имени модели.
        self._is_anthropic = "claude" in self.model.lower() or self.api_base.rstrip("/").endswith(
            "/v1/messages"
        )

        # Security: warn if API key sent over unencrypted HTTP
        if self.api_key and self.api_base.startswith("http://"):
            api_base_host = self.api_base.split("//", 1)[1].split("/")[0].split(":")[0]
            if api_base_host not in ("localhost", "127.0.0.1"):
                logger.warning(
                    "API key передаётся по нешифрованному HTTP (%s) — MITM риск!", self.api_base
                )

        # Шаринг heuristic_scorer / db_manager на всё время жизни классификатора
        if heuristic_scorer is not None:
            self._scorer = heuristic_scorer
        else:
            self._scorer = HeuristicScorer(db_manager or DatabaseManager())

        # E2: db для записи llm_errors-метрики.
        self._db: DatabaseManager | None = db_manager or getattr(self._scorer, "db_manager", None)

        # J1: in-memory LRU-кэш для LLM-ответов (только reactive).
        from llm_cache import LLMResponseCache

        self._response_cache = LLMResponseCache()

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

    def _call_llm(
        self,
        system_message: str,
        user_message: str,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str | None:
        """Универсальный вызов LLM — авто-детект OpenAI vs Anthropic формата."""
        if not self.api_key:
            return None

        if self._is_anthropic:
            return self._call_anthropic(system_message, user_message, temperature, json_mode)
        return self._call_openai(system_message, user_message, temperature, json_mode)

    def _call_anthropic(
        self,
        system_message: str,
        user_message: str,
        temperature: float,
        json_mode: bool = False,
    ) -> str | None:
        """Вызов через Anthropic Messages API (/v1/messages)."""
        url = f"{self.api_base}/messages"
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": self.model,
            "system": system_message,
            "messages": [{"role": "user", "content": user_message}],
            "max_tokens": 1024,
            "temperature": temperature,
        }
        if json_mode:
            # Anthropic не поддерживает response_format как OpenAI,
            # но можно использовать prefill чтобы триггернуть JSON
            payload["messages"].append({"role": "assistant", "content": "{"})
        resp = requests.post(url, headers=headers, json=payload, timeout=self.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # Anthropic format: {"content": [{"type": "text", "text": "..."}]}
        content_blocks = data.get("content", [])
        if content_blocks:
            text = content_blocks[0].get("text", "").strip()
            # Prefill добавляет "{" перед JSON — если ответ не начинается с "{",
            # восстанавливаем (Anthropic продолжает с prefill).
            if json_mode and text and not text.startswith("{"):
                text = "{" + text
            return text
        return None

    def _call_openai(
        self, system_message: str, user_message: str, temperature: float, json_mode: bool
    ) -> str | None:
        """Вызов через OpenAI Chat Completions API (/v1/chat/completions)."""
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = requests.post(url, headers=headers, json=payload, timeout=self.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()

    # ---------- classification ----------

    def classify_listing(self, listing_data: dict[str, Any]) -> tuple[str, float, str]:
        """
        Возвращает (label, confidence, reason).

        Если ключа нет / клиент недоступен / LLM ошибка / невалидный JSON —
        делаем fallback на эвристику.
        """
        if not self.api_key:
            return self._scorer.calculate_score(listing_data)[:3]

        try:
            user_prompt = self._create_classification_prompt(listing_data)
            logger.debug("classify_listing prompt: %s", user_prompt[:300])
            content = self._call_llm(
                system_message=self._create_system_message(),
                user_message=user_prompt,
                temperature=0.0,
                json_mode=True,
            )
            if content is None:
                return self._scorer.calculate_score(listing_data)[:3]
            logger.debug("classify_listing output: %s", content[:300])
        except Exception as exc:
            logger.warning("classify_listing failed: %s — fallback на эвристику", exc)
            self._incr_llm_error("classify:api")
            return self._scorer.calculate_score(listing_data)[:3]

        try:
            # Fix: Claude prefill может добавить { перед существующим { → {{...}
            if content.startswith("{{"):
                content = content[1:]
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
        params = listing_data.get("params") or {}
        params_str = ", ".join(f"{k}: {v}" for k, v in params.items()) if params else "не указаны"
        views = listing_data.get("views") or 0
        active_count = listing_data.get("active_listings_count") or 0
        similar_count = listing_data.get("similar_listings_count") or 0
        return _load_prompt("classify_listing.user.txt").format(
            title=_escape_format(listing_data.get("title", "") or ""),
            description=_escape_format((listing_data.get("description", "") or "")[:1500]),
            seller_name=_escape_format(listing_data.get("seller_name", "") or ""),
            phone=_escape_format(listing_data.get("phone", "") or ""),
            profile_id=_escape_format(listing_data.get("profile_id", "") or ""),
            params=_escape_format(params_str),
            views=views,
            active_listings_count=active_count,
            similar_listings_count=similar_count,
        )

    # ---------- response generation ----------

    def generate_response(self, context: dict[str, Any], chat_history: list[dict[str, Any]]) -> str:
        """
        Генерация ответа клиенту в Avito-чате.
        chat_history — список {'direction': 'in'|'out', 'text': '...'}

        J1: результат кэшируется по (dialog_id, last_in_msg, persona) на 10 мин.
        """
        if not self.api_key:
            return "Здравствуйте! Уточните, пожалуйста, ваш вопрос."

        # J1: cache lookup
        last_in_msg = (chat_history or [])[-1].get("text", "") if chat_history else ""
        cached = self._response_cache.get(
            context.get("dialog_id"),
            last_in_msg,
            context.get("persona"),
        )
        if cached is not None:
            return cached

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
                title=_escape_format(context.get("title", "Недвижимость")),
                price=_escape_format(context.get("price", "Не указана")),
                description=_escape_format((context.get("description") or "")[:500]),
                history_str=_escape_format(history_str),
            )

            logger.debug("generate_response prompt: %s", prompt[:500])
            out = self._call_llm(
                system_message=system_message,
                user_message=prompt,
                temperature=0.7,
            )
            if out is None:
                return "Здравствуйте! Подскажите, пожалуйста, какой объект вас интересует?"

            logger.debug("generate_response output: %s", out[:300])
            # J1: cache the result
            self._response_cache.set(
                context.get("dialog_id"),
                last_in_msg,
                context.get("persona"),
                out,
            )
            return out
        except Exception as exc:
            logger.warning("generate_response failed: %s", exc)
            self._incr_llm_error("respond:api")
        return "Здравствуйте! Подскажите, пожалуйста, какой объект вас интересует?"


# Example usage:
# config = {
#     "api_key": os.getenv("OPENAI_API_KEY", ""),
#     "model": "gpt-3.5-turbo",
#     "api_base": "https://api.openai.com/v1",
# }
# classifier = LLMClassifier(config, db_manager=DatabaseManager())
# label, confidence, reason = classifier.classify_listing(listing_data)
