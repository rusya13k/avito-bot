"""J1: In-memory LRU-кэш для LLM-ответов.

Ключ: (dialog_id, hash_of_last_incoming_message, persona_id).
TTL: 10 минут.
Применяется ТОЛЬКО для generate_response (reactive), НЕ для outbound
(там каждый первый message должен быть уникальным).
"""

import hashlib
import logging
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


class LLMResponseCache:
    def __init__(self, maxsize: int = 128, ttl_sec: float = 600.0):
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl_sec
        self._lock = threading.Lock()

    def _key(self, dialog_id: str | int, last_in_msg: str, persona: str | None) -> str:
        h = hashlib.sha256(f"{last_in_msg}:{persona or ''}".encode()).hexdigest()[:24]
        return f"{dialog_id}:{h}"

    def get(self, dialog_id, last_in_msg: str, persona: str | None) -> str | None:
        with self._lock:
            key = self._key(dialog_id, last_in_msg, persona)
            entry = self._cache.get(key)
            if entry is None:
                return None
            text, ts = entry
            if time.time() - ts > self.ttl:
                self._cache.pop(key, None)
                return None
            # LRU: move to end
            self._cache.move_to_end(key)
            logger.debug("LLM cache HIT %s", key)
            return text

    def set(self, dialog_id, last_in_msg: str, persona: str | None, text: str) -> None:
        with self._lock:
            key = self._key(dialog_id, last_in_msg, persona)
            if len(self._cache) >= self.maxsize:
                self._cache.popitem(last=False)
            self._cache[key] = (text, time.time())
            logger.debug("LLM cache SET %s", key)
