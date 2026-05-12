"""
F1: тесты HeuristicScorer.

Покрытие:
- D1: возвращается breakdown с ожидаемыми сигналами и contributions.
- D1: веса берутся из HEURISTIC_WEIGHTS (изменение конфига влияет на счёт).
- D2: confidence пропорциональна |score| / norm.
- Empty input -> 'uncertain' с conf=0.
"""

from classification_config import HEURISTIC_WEIGHTS
from heuristic_scorer import HeuristicScorer


class FakeDB:
    """Подставная БД с настраиваемыми ответами."""

    def __init__(self, active_listings=0, phone_count=0):
        self._active = active_listings
        self._phone = phone_count

    def get_account_active_listings(self, profile_id):
        return self._active

    def get_phone_count(self, phone):
        return self._phone


def test_owner_listing_is_classified_owner():
    scorer = HeuristicScorer(FakeDB())
    listing = {
        "url": "https://t/1",
        "description": "собственник, без посредников, прямая аренда",
        "seller_name": "Дмитрий Смирнов",  # без агентских ключей
        "phone": "+79991234567",
        "profile_id": "p1",
    }
    cls, conf, reason, breakdown = scorer.calculate_score(listing)
    assert cls == "owner"
    assert conf > 0
    sig_names = {b["signal"] for b in breakdown}
    assert "owner_signals_in_desc" in sig_names


def test_agent_listing_with_strong_signals():
    scorer = HeuristicScorer(FakeDB(active_listings=50, phone_count=10))
    listing = {
        "url": "https://t/2",
        "description": "комиссия 50%, наша база",
        "seller_name": "АН Этажи",
        "phone": "+79991111111",
        "profile_id": "p2",
    }
    cls, conf, reason, breakdown = scorer.calculate_score(listing)
    sig_names = {b["signal"] for b in breakdown}
    assert cls == "agent"
    assert {
        "active_listings_count",
        "phone_frequency",
        "agent_name_match",
        "agent_signals_in_desc",
    } <= sig_names
    assert conf >= 0.9


def test_empty_listing_is_uncertain_with_zero_confidence():
    scorer = HeuristicScorer(FakeDB())
    listing = {
        "url": "https://t/3",
        "description": "",
        "seller_name": "",
        "phone": "",
        "profile_id": "",
    }
    cls, conf, reason, breakdown = scorer.calculate_score(listing)
    assert cls == "uncertain"
    assert conf == 0
    assert breakdown == []


def test_weak_signal_falls_below_llm_threshold():
    """Только длинное описание — score=0.1 → 'uncertain' с низкой conf."""
    scorer = HeuristicScorer(FakeDB())
    listing = {
        "url": "https://t/4",
        "description": "a" * 200,
        "seller_name": "Дмитрий Смирнов",
        "phone": "",
        "profile_id": "",
    }
    cls, conf, reason, breakdown = scorer.calculate_score(listing)
    assert cls == "uncertain"
    assert conf < 0.5  # D2: должен трактоваться как требующий LLM


def test_weight_override_changes_contribution():
    """D1: изменение HEURISTIC_WEIGHTS меняет contribution в breakdown."""
    scorer = HeuristicScorer(FakeDB())
    listing = {
        "url": "https://t/5",
        "description": "собственник",
        "seller_name": "Дмитрий Смирнов",
        "phone": "",
        "profile_id": "",
    }

    old = HEURISTIC_WEIGHTS["owner_signal_per_match"]
    HEURISTIC_WEIGHTS["owner_signal_per_match"] = 5.0
    try:
        _, _, _, breakdown = scorer.calculate_score(listing)
        contrib = next(
            b["contribution"] for b in breakdown if b["signal"] == "owner_signals_in_desc"
        )
        assert contrib == 5.0
    finally:
        HEURISTIC_WEIGHTS["owner_signal_per_match"] = old


def test_confidence_normalization_uses_score_norm():
    """D2: confidence = min(1.0, |score| / confidence_score_norm)."""
    scorer = HeuristicScorer(FakeDB(active_listings=50, phone_count=10))
    listing = {
        "url": "https://t/big",
        "description": "",
        "seller_name": "АН Этажи",
        "phone": "+71112223344",
        "profile_id": "p",
    }
    # score должен быть очень отрицательным: -2 (active) -2 (phone) -1.5 (name)
    cls, conf, _, _ = scorer.calculate_score(listing)
    assert cls == "agent"
    assert conf == 1.0  # |score| > norm → насыщаемся
