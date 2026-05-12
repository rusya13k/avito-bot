import csv
import sqlite3

from database import DatabaseManager


def create_ground_truth_sample(output_file="ground_truth.csv", sample_size=50):
    """
    Create a ground truth sample of listings for evaluation.

    Args:
        output_file (str): Path to the output CSV file
        sample_size (int): Number of samples to include
    """
    db_manager = DatabaseManager()
    conn = sqlite3.connect(db_manager.db_path)
    cursor = conn.cursor()

    # Get sample listings from the database
    cursor.execute(
        """
        SELECT id, description, category, location
        FROM listings
        ORDER BY RANDOM()
        LIMIT ?
    """,
        (sample_size,),
    )

    listings = cursor.fetchall()

    # Write to CSV
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = ["id", "description", "category", "location", "true_label", "notes"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for listing in listings:
            listing_id, description, category, location = listing
            writer.writerow(
                {
                    "id": listing_id,
                    "description": description,
                    "category": category,
                    "location": location,
                    "true_label": "",  # To be filled manually
                    "notes": "",
                }
            )

    conn.close()
    print(f"Created ground truth sample with {len(listings)} listings in {output_file}")


def evaluate_classification_accuracy(ground_truth_file="ground_truth.csv"):
    """
    Evaluate the classification accuracy based on ground truth data.

    Args:
        ground_truth_file (str): Path to the ground truth CSV file
    """
    # This would be implemented to read the ground truth file and compare with classification results
    print("Evaluation function placeholder")
    # In a real implementation, this would:
    # 1. Read the ground truth file
    # 2. Compare with actual classifications in the database
    # 3. Calculate precision/recall metrics
    # 4. Generate a report


if __name__ == "__main__":
    create_ground_truth_sample()
