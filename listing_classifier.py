import logging
from datetime import datetime
from typing import Any

from classification_config import (
    CLASSIFICATION_THRESHOLDS,
    LLM_FALLBACK_CONFIDENCE_THRESHOLD,
)
from database import DatabaseManager
from heuristic_scorer import HeuristicScorer
from llm_classifier import LLMClassifier

logger = logging.getLogger("classifier.listing")


class ListingClassifier:
    def __init__(self, db_manager: DatabaseManager, llm_config: dict[str, Any]):
        self.db_manager = db_manager
        self.heuristic_scorer = HeuristicScorer(db_manager)
        # Reuse the same heuristic_scorer для fallback-классификации внутри LLMClassifier,
        # чтобы не плодить лишние DatabaseManager-ы.
        self.llm_classifier = LLMClassifier(
            llm_config,
            db_manager=db_manager,
            heuristic_scorer=self.heuristic_scorer,
        )
        self.thresholds = CLASSIFICATION_THRESHOLDS
        # D2: порог уверенности эвристики, ниже которого зовём LLM.
        self.llm_fallback_threshold = LLM_FALLBACK_CONFIDENCE_THRESHOLD

    def classify_listing(self, listing_data: dict[str, Any]) -> dict[str, Any]:
        """
        Two-stage classification of a listing.

        Stage 1 (heuristic): быстро, без затрат — и возвращает breakdown
            по сработавшим сигналам (D1).
        Stage 2 (LLM, опц.): зовётся, если эвристика выдала 'uncertain'
            ИЛИ эвристический confidence ниже LLM_FALLBACK_CONFIDENCE_THRESHOLD
            (D2: ансамбль вместо строгого 'uncertain').

        Returns classification results dictionary (с дополнительным
        полем `breakdown` — для аналитики/тюнинга порогов).
        """
        # Stage 1: heuristic scoring (D1: получаем breakdown)
        classification, confidence, reason, breakdown = self.heuristic_scorer.calculate_score(
            listing_data
        )
        source = "heuristic"

        # D2: LLM-fallback не только при 'uncertain', но и при низкой
        # уверенности эвристики (например, 'owner' с confidence=0.2).
        needs_llm = classification == "uncertain" or confidence < self.llm_fallback_threshold

        if needs_llm:
            llm_classification, llm_confidence, llm_reason = self.llm_classifier.classify_listing(
                listing_data
            )
            logger.info(
                "LLM fallback url=%s heuristic=(%s,%.2f) -> llm=(%s,%.2f)",
                listing_data.get("url"),
                classification,
                confidence,
                llm_classification,
                llm_confidence,
            )
            # D3-fix: ансамбль — доверяем LLM только если она увереннее
            # эвристики И не вернула "uncertain".
            llm_confident = llm_classification != "uncertain" and llm_confidence > confidence
            if llm_confident:
                classification = llm_classification
                confidence = llm_confidence
                reason = llm_reason
                source = "llm"
                logger.info(
                    "Ensemble: LLM перебил эвристику (%s,%.2f vs %s,%.2f)",
                    llm_classification,
                    llm_confidence,
                    classification,
                    confidence,
                )

        return {
            "classification": classification,
            "confidence": confidence,
            "reason": reason,
            "source": source,
            "breakdown": breakdown,
            "classified_at": datetime.now().isoformat(),
        }

    def classify_all_listings(self):
        """
        Classify all unclassified listings in the database.
        Per-listing error handling — one bad listing doesn't stop the batch.
        """
        unclassified_listings = self.db_manager.get_unclassified_listings()
        results = {"total_processed": 0, "owners": 0, "agents": 0, "uncertain": 0, "errors": 0}

        for listing in unclassified_listings:
            try:
                # Classify the listing
                result = self.classify_listing(listing)

                # Update listing in database
                self.db_manager.update_listing_classification(
                    listing["id"],
                    result["classification"],
                    result["confidence"],
                    result["source"],
                    result["classified_at"],
                )

                # Update account classification if profile_id exists
                if listing.get("profile_id") and listing["profile_id"] != "unknown":
                    self.db_manager.update_account_classification(
                        listing["profile_id"],
                        result["classification"],
                        result["confidence"],
                        result["source"],
                        result["classified_at"],
                    )

                # Update statistics
                results["total_processed"] += 1
                if result["classification"] == "owner":
                    results["owners"] += 1
                elif result["classification"] == "agent":
                    results["agents"] += 1
                else:
                    results["uncertain"] += 1
            except Exception as exc:
                logger.warning(
                    "classify_all_listings: listing id=%s failed: %s",
                    listing.get("id"),
                    exc,
                )
                results["errors"] += 1

        return results


# Example usage:
# db_manager = DatabaseManager()
# llm_config = {
#     "api_key": "your-openai-api-key",
#     "model": "gpt-3.5-turbo"
# }
# classifier = ListingClassifier(db_manager, llm_config)
# results = classifier.classify_all_listings()
# print(f"Classified {results['total_processed']} listings")
