import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import DatabaseManager
from listing_classifier import ListingClassifier


def test_classification():
    """
    Test the classification system
    """
    db_manager = DatabaseManager()
    llm_config = {
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "model": "gpt-3.5-turbo",
        "api_base": "https://api.openai.com/v1",
    }
    classifier = ListingClassifier(db_manager, llm_config)

    # Classify all listings
    results = classifier.classify_all_listings()
    print(
        f"Classified {results['total_processed']} listings: {results['owners']} owners, {results['agents']} agents, {results['uncertain']} uncertain"
    )


if __name__ == "__main__":
    test_classification()
