"""
G1: AvitoClient — единая точка доступа ко всему Selenium-флоу Avito-бота.

ПРОБЛЕМА (до G1):
    Selenium-логика была размазана по трём модулям:
        bot.py                  — login, навигация, search/browse loop
        commercial_parser.py    — parsing объявления
        avito_messenger.py      — диалоги и отправка сообщений
    При любом изменении (новый селектор, новая антибот-проверка) приходилось
    править в 3 разных местах. Не было единого "контракта" того, что бот
    умеет делать с Avito.

РЕШЕНИЕ (G1):
    AvitoClient(driver, wait, account_name, ...) — фасадный класс,
    привязанный к конкретному driver-у и аккаунту. Все Selenium-операции
    идут через его методы. Реализации этих методов остаются в исходных
    модулях (постепенная миграция, обратная совместимость) — клиент пока
    что тонкая обёртка с единым API. Когда придёт время менять реализацию
    (например, при больших правках селекторов), правишь её внутри
    соответствующего метода — внешние вызывающие не страдают.

ПРИМЕР:
    client = AvitoClient(
        driver, wait, account_name="acc1",
        log_func=log, db_manager=db, llm_classifier=llm,
    )

    # composite login: native -> cookies -> manual, с детальным логированием.
    if not client.login(
        cookies_path=account.get("cookies_path"),
        phone=account.get("phone"),
        password=account.get("password"),
    ):
        return

    client.warmup_yandex()
    client.browse_commercial_categories()
    processed, new_listings, errors = client.find_and_view_commercial_listings()
    client.process_messages()

ЧТО НЕ ДЕЛАЕТ AvitoClient:
    - Не управляет AdsPower (start/stop профиля) — это уровень выше,
      делает run_thread в bot.py.
    - Не работает с прокси (update_profile_proxy там же).
    - Не открывает соединение с БД — БД и LLMClassifier передаются снаружи.
    - Не управляет жизненным циклом потока (stop_event и т.п.).

Это сознательное разделение: AvitoClient — только то, что DOES selenium
для Avito.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AvitoClient:
    """
    Selenium-фасад, привязанный к driver + аккаунту.

    Lifecycle:
        client = AvitoClient(driver, wait, account_name, log_func=log, ...)
        # ... use it
        # driver.quit() делает вызывающий, AvitoClient своих ресурсов не имеет.

    Thread-safety:
        Один экземпляр на один поток (driver не shared). Параллелить
        работу с одним аккаунтом — архитектурно неверно (антифрод заметит).
    """

    def __init__(
        self,
        driver,
        wait,
        account_name: str,
        *,
        log_func=None,
        db_manager=None,
        llm_classifier=None,
    ):
        """
        Args:
            driver: selenium WebDriver (instance).
            wait: WebDriverWait (instance) с уже настроенным таймаутом.
            account_name: имя аккаунта; используется в логах и БД.
            log_func: callable(account_name, msg) -> None. По умолчанию no-op.
                Совместимо с bot.log из bot.py.
            db_manager: DatabaseManager (нужен для save_listing /
                process_messages; для login / warmup можно не передавать).
            llm_classifier: LLMClassifier (нужен для process_messages).
        """
        self.driver = driver
        self.wait = wait
        self.account_name = account_name
        self.log = log_func or (lambda *_args, **_kwargs: None)
        self.db = db_manager
        self.llm = llm_classifier

    # ──────────────────────────────────────────────────────────────────────
    # Navigation
    # ──────────────────────────────────────────────────────────────────────

    def safe_get(self, url: str, retries: int = 2) -> bool:
        """
        Safe page navigation: timeout/retry-aware. True при успешном открытии.
        Wrapper над bot.safe_get.
        """
        from bot import safe_get

        return safe_get(self.driver, url, self.account_name, retries=retries)

    def goto_listing(self, url: str, retries: int = 2) -> bool:
        """
        G1: открыть листинг по URL с проверкой блокировки/капчи.

        Возвращает True, если страница открылась И не выглядит как
        IP-block / captcha-screen. False — иначе (вызывающий сам решит,
        ставить ли cooldown или просто пропустить).
        """
        if not self.safe_get(url, retries=retries):
            return False
        if self.check_block():
            self.log(self.account_name, f"goto_listing: block/captcha at {url}")
            return False
        return True

    def check_block(self) -> bool:
        """
        Проверка, что текущая страница — не block/captcha.
        Wrapper над bot.check_block. Возвращает True, если ЕСТЬ блок.
        """
        from bot import check_block

        return check_block(self.driver, self.account_name)

    # ──────────────────────────────────────────────────────────────────────
    # Login
    # ──────────────────────────────────────────────────────────────────────

    def is_session_authenticated(self) -> bool:
        """Залогинен ли текущий driver. Wrapper над bot.is_session_authenticated."""
        from bot import is_session_authenticated

        return is_session_authenticated(self.driver, self.account_name)

    def load_cookies(self, cookies_path: str | Path, domain: str = "avito.ru") -> None:
        """Cookies-injection из файла. Wrapper над bot.load_cookies."""
        from bot import load_cookies

        load_cookies(self.driver, str(cookies_path), domain)

    def perform_login(self, phone: str, password: str) -> bool:
        """
        Manual login через phone+password (с B1-обработкой SMS/captcha).
        Wrapper над bot.perform_login.
        """
        from bot import perform_login

        return perform_login(self.driver, self.wait, self.account_name, phone, password)

    def login(
        self,
        *,
        cookies_path: str | Path | None = None,
        phone: str | None = None,
        password: str | None = None,
        thinking_delay: bool = True,
    ) -> bool:
        """
        G1: composite-логин. Раньше эту 3-уровневую логику дублировал run_thread.

        Порядок попыток:
          1. Native AdsPower-сессия (просто проверка) — самый "тихий" путь
             с точки зрения антифрода.
          2. Cookies-injection из файла, если cookies_path задан и существует.
          3. Manual phone/password login (с B1-обработкой SMS/captcha).

        Возвращает True при любой успешной аутентификации, False — если
        все попытки провалились ИЛИ недоступна авито в принципе.

        thinking_delay=True: добавляет human-like паузу 3-14s ПОСЛЕ открытия
        главной — мы должны выглядеть как живой пользователь, который только
        что зашёл и оглядывается. False — для тестов / ускоренного флоу.
        """
        # B4: native first.
        self.log(self.account_name, "AvitoClient.login: opening avito.ru...")
        if not self.safe_get("https://www.avito.ru"):
            self.log(self.account_name, "WARNING: Could not reach Avito.")
            return False

        if thinking_delay:
            from bot import hp

            self.log(self.account_name, "  Thinking (3-14s)...")
            hp(3, 14)

        if self.is_session_authenticated():
            self.log(
                self.account_name,
                "  SUCCESS: Native AdsPower session is live — skipping login.",
            )
            return True

        # Cookies injection.
        if cookies_path:
            cookies_path = Path(cookies_path)
            if cookies_path.exists():
                self.log(
                    self.account_name,
                    "  Native session missing. Trying cookies injection...",
                )
                try:
                    self.load_cookies(cookies_path, "avito.ru")
                except Exception as exc:
                    self.log(
                        self.account_name,
                        f"  Cookies injection error: {exc}",
                    )
                else:
                    if thinking_delay:
                        from bot import hp

                        hp(3, 6)
                    if self.is_session_authenticated():
                        self.log(
                            self.account_name,
                            "  SUCCESS: Logged in via Cookie Injection.",
                        )
                        return True
            else:
                self.log(
                    self.account_name,
                    f"  Cookies path {cookies_path} не существует — пропускаю.",
                )

        # Manual login.
        if phone and password:
            self.log(
                self.account_name,
                "  Cookies path unavailable. Starting manual login...",
            )
            if self.perform_login(phone, password):
                self.log(
                    self.account_name,
                    "  SUCCESS: Logged in via Phone/Password.",
                )
                return True
            self.log(self.account_name, "  CRITICAL: Manual login failed.")
            return False

        self.log(
            self.account_name,
            "  CRITICAL: All login methods exhausted — нет credentials для manual.",
        )
        return False

    # ──────────────────────────────────────────────────────────────────────
    # Search & listings
    # ──────────────────────────────────────────────────────────────────────

    def warmup_yandex(self, num_queries: int = 2) -> bool:
        """Yandex-warmup перед основной работой. Wrapper над bot.yandex_warmup."""
        from bot import yandex_warmup

        return yandex_warmup(
            self.driver,
            self.wait,
            self.account_name,
            num_queries=num_queries,
        )

    def browse_commercial_categories(self, *args, **kwargs) -> Any:
        """
        Открытие коммерческих категорий + ввод keyword'а.
        Wrapper над bot.browse_commercial_categories.
        """
        from bot import browse_commercial_categories

        return browse_commercial_categories(
            self.driver,
            self.wait,
            self.account_name,
            *args,
            **kwargs,
        )

    def find_and_view_commercial_listings(self) -> tuple[int, int, int]:
        """
        Главный scraping-loop: листает листинги в SERP, парсит каждый.
        Возвращает (processed, new_listings, errors).
        Wrapper над bot.find_and_view_commercial_listings.
        """
        if self.db is None:
            raise RuntimeError("AvitoClient.find_and_view_commercial_listings requires db_manager")
        from bot import find_and_view_commercial_listings

        return find_and_view_commercial_listings(
            self.driver,
            self.wait,
            self.account_name,
            self.db,
        )

    def extract_listing_data(self) -> dict[str, Any]:
        """
        Парсит ОТКРЫТЫЙ сейчас в driver-е листинг и возвращает dict.
        Wrapper над commercial_parser.extract_listing_data.

        Использование (ручной флоу, обходящий find_and_view_..._listings):
            client.goto_listing(url)
            data = client.extract_listing_data()
            client.save_listing(data)
        """
        from commercial_parser import extract_listing_data

        return extract_listing_data(
            self.driver,
            self.wait,
            self.account_name,
            self.log,
        )

    def save_listing(self, listing_data: dict[str, Any]) -> int | None:
        """Сохранение листинга в БД + инкремент E2-метрик. Wrapper."""
        if self.db is None:
            raise RuntimeError("AvitoClient.save_listing requires db_manager")
        from commercial_parser import save_listing_to_db

        return save_listing_to_db(
            listing_data,
            self.db,
            self.log,
            self.account_name,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Messenger
    # ──────────────────────────────────────────────────────────────────────

    def process_messages(self) -> None:
        """
        Главный messenger-loop: открывает /profile/messenger, проходит по
        диалогам, отвечает через LLM на необработанные in-сообщения.

        Внутренне создаёт AvitoMessenger — он держит cohesive state на время
        одной итерации обхода.
        """
        if self.db is None or self.llm is None:
            raise RuntimeError("AvitoClient.process_messages requires db_manager и llm_classifier")
        from avito_messenger import AvitoMessenger

        messenger = AvitoMessenger(
            self.driver,
            self.wait,
            self.db,
            self.llm,
            self.account_name,
        )
        messenger.process_messages(self.log)
