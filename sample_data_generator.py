import random
import sqlite3
from datetime import datetime, timedelta


def create_sample_data(db_path="avito_bot.db"):
    """
    Create sample data for testing the classification system
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Sample seller names (reference data — пока не используются в самом
    # генераторе; пригодится при расширении сэмплов листингов).
    seller_names = [  # noqa: F841
        "Иванов И.И.",
        "Петров Петр",
        "Сидоров Сидор",
        "Агентство Недвижимости",
        "АН 'Комфорт'",
        "АН 'Этажи'",
        "ООО 'Миэль'",
        "ООО 'Инком-Недвижимость'",
        "Собственник",
    ]

    # Sample descriptions for owners
    owner_descriptions = [
        "Продам офисное помещение 50 кв.м. в центре города. Состояние хорошее, ремонт сделан недавно.",
        "Сдам склад 200 кв.м. Собственник. Без посредников. Цена договорная.",
        "Прямая аренда от собственника. Офисное помещение 30 кв.м. в деловом центре.",
        "Продается коммерческое помещение с парковкой. Без комиссии. Собственник.",
        "Собственник. Сдается офисное помещение под юридическую компанию.",
    ]

    # Sample descriptions for agents
    agent_descriptions = [
        "Комиссия 3%. Представляю интересы собственника. Готовы к переговорам.",
        "Работаем с собственником. Подберём объект под ваши требования.",
        "Наша база объектов в этом районе насчитывает более 50 единиц.",
        "Агентство недвижимости 'Этажи' предлагает лучшие условия.",
        "Профессиональный подбор объектов для бизнеса. Комиссия 5%.",
    ]

    # Insert sample listings
    for i in range(100):
        # Randomly choose if it's an owner or agent listing
        is_owner = random.choice([True, False])

        if is_owner:
            description = random.choice(owner_descriptions)
            seller_name = random.choice(
                ["Иванов И.И.", "Петров Петр", "Сидоров Сидор", "Собственник"]
            )
            active_listings = random.randint(1, 2)
        else:
            description = random.choice(agent_descriptions)
            seller_name = random.choice(
                ["Агентство Недвижимости", "АН 'Комфорт'", "АН 'Этажи'", "ООО 'Миэль'"]
            )
            active_listings = random.randint(6, 50)

        # Insert listing
        cursor.execute(
            """
            INSERT OR REPLACE INTO listings
            (url, category, area, price, location, description, date_parsed, date_published, date_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                f"https://www.avito.ru/test_listing_{i}",
                "офисные помещения",
                random.uniform(20, 200),
                random.uniform(50000, 5000000),
                "Москва",
                description,
                datetime.now().isoformat(),
                (datetime.now() - timedelta(days=random.randint(1, 30))).isoformat(),
                datetime.now().isoformat(),
            ),
        )

        # Insert account
        profile_id = f"profile_{i}"
        cursor.execute(
            """
            INSERT OR REPLACE INTO avito_accounts
            (profile_id, name, active_listings_count, registration_date, score)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                profile_id,
                seller_name,
                active_listings,
                (datetime.now() - timedelta(days=random.randint(30, 1000))).strftime("%Y-%m-%d"),
                0.0,
            ),
        )

        # Insert phone (with some phones appearing multiple times for agents)
        phone_count = 1 if is_owner else random.randint(1, 10)
        phone_normalized = f"+79{random.randint(100000000, 999999999)}"
        cursor.execute(
            """
            INSERT OR REPLACE INTO phones
            (phone_normalized, listing_count, score)
            VALUES (?, ?, ?)
        """,
            (phone_normalized, phone_count, 0.0),
        )

    conn.commit()
    conn.close()
    print(f"Created sample data with {100} listings")


if __name__ == "__main__":
    create_sample_data()
