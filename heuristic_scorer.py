import logging
from typing import Any

from classification_config import (
    AGENT_KEYWORDS,
    CLASSIFICATION_THRESHOLDS,
    HEURISTIC_WEIGHTS,
)
from database import DatabaseManager

# D1: отдельный logger, чтобы можно было поднять уровень только для
# классификатора (полезно при тюнинге порогов на размеченном наборе).
logger = logging.getLogger("classifier.heuristic")


class HeuristicScorer:
    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self.agent_keywords = AGENT_KEYWORDS
        self.thresholds = CLASSIFICATION_THRESHOLDS
        self.weights = HEURISTIC_WEIGHTS

    def count_active_listings_signal(self, profile_id: str) -> int:
        """Count active listings for the seller"""
        return self.db_manager.get_account_active_listings(profile_id)

    def phone_frequency_signal(self, phone_normalized: str) -> int:
        """Count how many listings this phone appears in"""
        return self.db_manager.get_phone_count(phone_normalized)

    def name_contains_agent_keywords(self, name: str) -> int:
        """Check if name contains agent-related keywords"""
        if not name:
            return 0
        name_lower = name.lower()
        for keyword in self.agent_keywords["agent_names"]:
            if keyword.lower() in name_lower:
                return 1
        return 0

    def description_contains_agent_signals(self, description: str) -> int:
        """Check if description contains agent signals"""
        if not description:
            return 0
        desc_lower = description.lower()
        count = 0
        for signal in self.agent_keywords["agent_signals"]:
            if signal in desc_lower:
                count += 1
        return count

    def description_contains_owner_signals(self, description: str) -> int:
        """Check if description contains owner signals"""
        if not description:
            return 0
        desc_lower = description.lower()
        count = 0
        for signal in self.agent_keywords["owner_signals"]:
            if signal in desc_lower:
                count += 1
        return count

    # ──────────────────────────────────────────────────────────────────
    # D1: внутренний хелпер — добавить запись в breakdown.
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _add_signal(
        breakdown: list[dict[str, Any]],
        name: str,
        value: Any,
        contribution: float,
        description: str,
    ) -> None:
        breakdown.append(
            {
                "signal": name,
                "value": value,
                "contribution": round(contribution, 4),
                "description": description,
            }
        )

    def calculate_score(
        self, listing_data: dict[str, Any]
    ) -> tuple[str, float, str, list[dict[str, Any]]]:
        """
        Считает классификацию для одного листинга.

        Возвращает: (classification, confidence, reason, breakdown).
        breakdown — список dict-ов по каждому СРАБОТАВШЕМУ сигналу
        (signal, value, contribution, description). Используется в
        ListingClassifier для логирования и аналитики.
        """
        score = 0.0
        reasons: list[str] = []
        breakdown: list[dict[str, Any]] = []

        profile_id = listing_data.get("profile_id") or ""
        description = listing_data.get("description") or ""
        seller_name = listing_data.get("seller_name") or ""
        phone = listing_data.get("phone") or ""
        w = self.weights

        # Сигнал 1: много активных объявлений
        # Приоритет: реальный счётчик из профиля продавца (если визит был),
        # иначе fallback на количество в нашей БД.
        active_listings = listing_data.get("active_listings_count") or 0
        if active_listings == 0:
            active_listings = self.count_active_listings_signal(profile_id)
        if active_listings > w["active_listings_threshold"]:
            contrib = w["active_listings_weight"]
            score += contrib
            msg = f"много активных объявлений ({active_listings})"
            reasons.append(msg)
            self._add_signal(breakdown, "active_listings_count", active_listings, contrib, msg)

        # Сигнал 1b: много похожих объявлений в категории недвижимости
        similar_listings = listing_data.get("similar_listings_count") or 0
        if similar_listings >= 3:
            contrib = w.get("similar_listings_weight", -1.5)
            score += contrib
            msg = f"похожих объявлений в недвижимости: {similar_listings}"
            reasons.append(msg)
            self._add_signal(
                breakdown, "similar_listings_count", similar_listings, contrib, msg
            )

        # Сигнал 2: телефон встречается в N+ листингах
        phone_count = self.phone_frequency_signal(phone)
        if phone_count > w["phone_frequency_threshold"]:
            contrib = w["phone_frequency_weight"]
            score += contrib
            msg = f"телефон встречается в {phone_count} объявлениях"
            reasons.append(msg)
            self._add_signal(breakdown, "phone_frequency", phone_count, contrib, msg)

        # Сигнал 3: имя продавца — название агентства
        if self.name_contains_agent_keywords(seller_name):
            contrib = w["agent_name_weight"]
            score += contrib
            msg = "имя содержит признаки агентства"
            reasons.append(msg)
            self._add_signal(breakdown, "agent_name_match", seller_name, contrib, msg)

        # Сигнал 4: agent-signals в описании
        agent_signals_in_desc = self.description_contains_agent_signals(description)
        if agent_signals_in_desc > 0:
            contrib = agent_signals_in_desc * w["agent_signal_per_match"]
            score += contrib
            msg = f"в описании найдено {agent_signals_in_desc} признаков агента"
            reasons.append(msg)
            self._add_signal(
                breakdown, "agent_signals_in_desc", agent_signals_in_desc, contrib, msg
            )

        # Сигнал 5: owner-signals в описании
        owner_signals_in_desc = self.description_contains_owner_signals(description)
        if owner_signals_in_desc > 0:
            contrib = owner_signals_in_desc * w["owner_signal_per_match"]
            score += contrib
            msg = f"в описании найдено {owner_signals_in_desc} признаков собственника"
            reasons.append(msg)
            self._add_signal(
                breakdown, "owner_signals_in_desc", owner_signals_in_desc, contrib, msg
            )

        # Слабый сигнал: длина описания
        if description and len(description) > w["long_description_threshold"]:
            contrib = w["long_description_weight"]
            score += contrib
            msg = "подробное описание"
            reasons.append(msg)
            self._add_signal(breakdown, "long_description", len(description), contrib, msg)

        # ── Финальная классификация ──────────────────────────────────
        # D2: confidence теперь пропорционален |score| и нормируется
        # на confidence_score_norm. Раньше делили на 2.0 — пограничные
        # 'owner' с score=0.5 получали confidence=0.25, что МЕНЬШЕ, чем
        # дефолтный 0.5 у 'uncertain'. Это ломало логику D2 (LLM-fallback
        # по порогу confidence).
        norm = w["confidence_score_norm"]
        if score >= self.thresholds["owner_threshold"]:
            classification = "owner"
            confidence = min(1.0, score / norm)
        elif score <= self.thresholds["agent_threshold"]:
            classification = "agent"
            confidence = min(1.0, abs(score) / norm)
        else:
            # 'uncertain': явно низкая уверенность; чем ближе score к 0,
            # тем меньше confidence. Используется только для отчётности —
            # ListingClassifier всё равно зовёт LLM при 'uncertain'.
            classification = "uncertain"
            confidence = round(abs(score) / norm, 4)

        reason = "; ".join(reasons) if reasons else "нет сильных признаков"

        # D1: компактный лог для последующего анализа порогов.
        logger.info(
            "score listing url=%s class=%s conf=%.3f score=%.3f signals=%s",
            listing_data.get("url"),
            classification,
            confidence,
            score,
            [b["signal"] for b in breakdown] or ["none"],
        )

        return classification, confidence, reason, breakdown


# Example usage:
# scorer = HeuristicScorer(db_manager)
# classification, confidence, reason, breakdown = scorer.calculate_score(listing_data)
