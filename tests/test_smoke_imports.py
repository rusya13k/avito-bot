"""
F1 + H4 (CI smoke): импорт всех модулей не падает.
Один из самых полезных тестов — ловит сломанные синтаксис/имена/циклы.
"""


def test_all_modules_import():
    import account_state  # noqa: F401
    import avito_messenger  # noqa: F401
    import bot  # noqa: F401
    import captcha_detect  # noqa: F401
    import classification_config  # noqa: F401
    import commercial_parser  # noqa: F401
    import database  # noqa: F401
    import heuristic_scorer  # noqa: F401
    import human_delay  # noqa: F401
    import listing_classifier  # noqa: F401
    import llm_classifier  # noqa: F401
    import logging_setup  # noqa: F401
    import tg_bot  # noqa: F401
