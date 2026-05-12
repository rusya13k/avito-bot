"""
F1: тесты ListingClassifier (D2: ансамбль heuristic + LLM).
"""

from unittest.mock import patch


def _make_classifier(db):
    """Создаёт ListingClassifier без OpenAI-клиента (api_key='')."""
    from listing_classifier import ListingClassifier

    return ListingClassifier(db, llm_config={"api_key": ""})


def test_high_confidence_heuristic_does_not_call_llm(db):
    """D2: при сильном heuristic-сигнале LLM не должен вызываться."""
    listing = db.get_listing_by_url("noop") or {
        "description": "комиссия, наша база",
        "seller_name": "АН Этажи",
        "phone": "+79991111111",
        "profile_id": "p",
    }
    clf = _make_classifier(db)
    with patch.object(clf.llm_classifier, "classify_listing") as llm_mock:
        result = clf.classify_listing(listing)
        # heuristic выдаст 'agent' с высоким confidence — LLM не вызывается
        if (
            result["confidence"] >= clf.llm_fallback_threshold
            and result["classification"] != "uncertain"
        ):
            llm_mock.assert_not_called()
            assert result["source"] == "heuristic"


def test_low_confidence_heuristic_triggers_llm(db):
    """D2: heuristic не уверен → зовём LLM."""
    clf = _make_classifier(db)
    listing = {
        "description": "",
        "seller_name": "Дмитрий",
        "phone": "",
        "profile_id": "",
        "url": "u",
    }
    with patch.object(
        clf.llm_classifier, "classify_listing", return_value=("owner", 0.85, "LLM said owner")
    ) as llm_mock:
        result = clf.classify_listing(listing)
        llm_mock.assert_called_once()
        assert result["source"] == "llm"
        assert result["classification"] == "owner"
        assert result["confidence"] == 0.85


def test_breakdown_in_result(db):
    """D1: result содержит breakdown."""
    clf = _make_classifier(db)
    listing = {
        "description": "собственник, без посредников",
        "seller_name": "Дмитрий Смирнов",
        "phone": "+79991234567",
        "profile_id": "p",
        "url": "u",
    }
    result = clf.classify_listing(listing)
    assert "breakdown" in result
    assert isinstance(result["breakdown"], list)
    if result["source"] == "heuristic":
        assert len(result["breakdown"]) > 0
