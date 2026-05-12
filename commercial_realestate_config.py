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
            "/nedvizhimost/kommercheskaya_nedvizhimost/ofisy/prodam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/torgovie_pomescheniya/prodam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/skladi/prodam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/proizvodstvennie_pomescheniya/prodam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/svobodnogo_naznacheniya/prodam-ASgBAgICAUSwCMpB",
        ],
    },
    "rent": {
        "min_price": 200000,
        "paths": [
            "/nedvizhimost/kommercheskaya_nedvizhimost/ofisy/sdam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/torgovie_pomescheniya/sdam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/skladi/sdam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/proizvodstvennie_pomescheniya/sdam-ASgBAgICAUSwCMpB",
            "/nedvizhimost/kommercheskaya_nedvizhimost/svobodnogo_naznacheniya/sdam-ASgBAgICAUSwCMpB",
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
    "/nedvizhimost/kommercheskaya_nedvizhimost/ofisy",
    "/nedvizhimost/kommercheskaya_nedvizhimost/torgovie_pomescheniya",
    "/nedvizhimost/kommercheskaya_nedvizhimost/skladi",
    "/nedvizhimost/kommercheskaya_nedvizhimost/proizvodstvennie_pomescheniya",
    "/nedvizhimost/kommercheskaya_nedvizhimost/svobodnogo_naznacheniya",
]
