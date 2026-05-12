"""
F1: тесты D3 (phone parsing) и D4 (URL normalization).
"""

import pytest

from commercial_parser import (
    extract_phones_from_text,
    normalize_listing_url,
    normalize_phone,
)

# ── D3: normalize_phone ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inp,expected",
    [
        ("+7 (495) 123-45-67", "+74951234567"),
        ("8 (495) 123-45-67", "+74951234567"),
        ("8 495 123 4567", "+74951234567"),
        ("+7-915-123-45-67", "+79151234567"),
        ("89151234567", "+79151234567"),  # 8XXX
        ("9151234567", "+79151234567"),  # 10 digits, начин с 9 — мобильный
        ("+7 (***) ***-**-**", None),  # скрытый номер
        ("+1 415 555 0123", None),  # иностранный
        ("415-555-0123", None),  # 10 digits, не 9XX
        ("123", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_phone(inp, expected):
    assert normalize_phone(inp) == expected


def test_extract_phones_finds_multiple():
    text = (
        "Звоните: +7 495 123 4567 или 8 (916) 555-22-33.\n"
        "Дополнительно: +7 (812) 999-00-11.\n"
        "Также есть +1 415 555 0123 (не российский)."
    )
    phones = extract_phones_from_text(text)
    assert "+74951234567" in phones
    assert "+79165552233" in phones
    assert "+78129990011" in phones
    assert len(phones) == 3


def test_extract_phones_dedups():
    text = "Звоните +79991234567 или +7 (999) 123-45-67"
    phones = extract_phones_from_text(text)
    assert len(phones) == 1
    assert phones[0] == "+79991234567"


def test_extract_phones_empty():
    assert extract_phones_from_text("") == []
    assert extract_phones_from_text(None) == []


# ── D4: normalize_listing_url ─────────────────────────────────────────


def test_normalize_url_strips_query():
    canonical = "https://www.avito.ru/moskva/kommercheskaya/ofis_2747365"
    variants = [
        "https://www.avito.ru/moskva/kommercheskaya/ofis_2747365?utm_source=fb",
        "https://www.avito.ru/moskva/kommercheskaya/ofis_2747365/",
        "https://www.avito.ru/moskva/kommercheskaya/ofis_2747365#section",
        "https://WWW.avito.ru/moskva/kommercheskaya/ofis_2747365?slocation_id=1",
    ]
    for v in variants:
        assert normalize_listing_url(v) == canonical, v


def test_normalize_url_different_ids_differ():
    a = normalize_listing_url("https://www.avito.ru/x/ofis_111")
    b = normalize_listing_url("https://www.avito.ru/x/ofis_222")
    assert a != b


def test_normalize_url_handles_empty():
    assert normalize_listing_url("") == ""
    assert normalize_listing_url(None) is None
