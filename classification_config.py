# Agent detection configuration
AGENT_KEYWORDS = {
    "agent_names": [
        "АН",
        "агентство",
        "недвижимость",
        "realty",
        "estate",
        "Этажи",
        "Миэль",
        "Инком",
        "А101",
        "Самолет",
        "ПИК",
    ],
    "agent_signals": [
        "комиссия",
        "представляю интересы",
        "наша база",
        "подберём",
        "работаем с собственником",
    ],
    "owner_signals": ["собственник", "без посредников", "прямая аренда", "без комиссии"],
}

# Thresholds for classification
CLASSIFICATION_THRESHOLDS = {
    "owner_threshold": 0.5,  # Score >= this value -> owner
    "agent_threshold": -0.5,  # Score <= this value -> agent
}

# D1: веса эвристики вынесены сюда из HeuristicScorer.calculate_score.
# Раньше были захардкожены в коде, что мешало быстро тюнить пороги
# на размеченном датасете. Теперь — одно место для правки.
#
# Знак веса: отрицательный = в сторону "agent", положительный = "owner".
# Пороги срабатывания (*_threshold) — минимальное значение признака,
# при котором сигнал засчитывается; per-match веса (*_per_match) —
# умножаются на количество совпадений.
HEURISTIC_WEIGHTS = {
    # Сигнал 1: много активных объявлений у продавца
    "active_listings_threshold": 5,
    "active_listings_weight": -2.0,
    # Сигнал 2: телефон встречается в N+ листингах
    "phone_frequency_threshold": 3,
    "phone_frequency_weight": -2.0,
    # Сигнал 3: имя продавца содержит ключевое слово агентства
    "agent_name_weight": -1.5,
    # Сигнал 4: в описании — N признаков агента
    "agent_signal_per_match": -0.5,
    # Сигнал 5: в описании — N признаков собственника
    "owner_signal_per_match": 0.5,
    # Слабый сигнал: длинное описание (обычно собственники подробнее)
    "long_description_threshold": 100,
    "long_description_weight": 0.1,
    # Нормировка confidence: |score| >= этой величины ⇒ confidence = 1.0
    # (раньше делили на 2.0, что давало неадекватно низкую уверенность
    #  для пограничных случаев — score=0.5 → confidence=0.25).
    "confidence_score_norm": 3.0,
}

# D2: ансамбль heuristic + LLM.
# Если эвристика не уверена в результате (confidence < этого порога),
# зовём LLM для финального решения. Раньше LLM звался только при
# classification == "uncertain", из-за чего низкоконфидентные
# 'owner'/'agent' проходили мимо проверки.
LLM_FALLBACK_CONFIDENCE_THRESHOLD = 0.5
