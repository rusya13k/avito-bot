# Commercial real estate configuration for million-plus cities

# List of million-plus cities in Russia for Avito
MILLION_CITIES = [
    "moskva",
    "sankt-peterburg",
    "novosibirsk",
    "ekaterinburg",
    "kazan",
    "nizhniy_novgorod",
    "chelyabinsk",
    "samara",
    "omsk",
    "rostov-na-donu",
    "ufa",
    "krasnoyarsk",
    "voronezh",
    "perm",
    "volgograd",
]

# Search categories and their price thresholds
# Sale: min 10,000,000 RUB
# Rent: min 200,000 RUB
COMMERCIAL_SEARCH_FILTERS = {
    "sale": {
        "min_price": 10000000,
        "paths": [
            "/kommercheskaya_nedvizhimost/prodam-ASgBAgICAUSwCNRW",
        ],
    },
    "rent": {
        "min_price": 200000,
        "paths": [
            "/kommercheskaya_nedvizhimost/sdam-ASgBAgICAUSwCNRW",
        ],
    },
}

# Legacy variables for backward compatibility if needed by other modules
COMMERCIAL_REAL_ESTATE_CATEGORIES = [
    "офисные_помещения",
    "торговые_помещения",
    "склады",
    "производственные_помещения",
    "свободного_назначения",
]

AVITO_COMMERCIAL_CATEGORIES = [
    "/kommercheskaya_nedvizhimost",
]
