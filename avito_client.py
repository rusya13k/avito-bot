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
        search_filters=None,
        favorite_rate: float = 0.08,
        call_rate: float = 0.05,
        max_listings_per_search: int = 7,
        max_categories_per_browse: int = 4,
        max_listings_per_browse: int = 4,
        messenger_config: dict | None = None,
        outbound_config: dict | None = None,
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
            search_filters: dict с per-account фильтрами поиска (E2).
                Допустимые ключи: cities (list[str]), deal_type (str),
                deal_types (list[str]), price_min (int), price_max (int).
                None / {} → глобальные defaults (random-поведение).
            favorite_rate: F1 — вероятность «Добавить в избранное» при просмотре
                листинга в browse. Конфигурируется через config.json /
                accounts.json (ключ view_listing_favorite_rate). Default: 0.08.
            call_rate: F1 — вероятность нажать «Позвонить» при просмотре.
                Конфигурируется через view_listing_call_rate. Default: 0.05.
            max_listings_per_search: F2 — верхняя граница весового распределения
                числа листингов за find_and_view. Default: 7.
            max_categories_per_browse: F2 — верхняя граница числа категорий
                за browse. Default: 4.
            max_listings_per_browse: F2 — верхняя граница числа листингов
                за одну категорию в browse. Default: 4.
            messenger_config: F5/T5 — kwargs для AvitoMessenger (min_reply_age_min,
                max_reply_age_min, reply_delay_mu, reply_delay_sigma,
                ignore_new_dialog_chance, persona). Прокидывается через **dict
                в process_messages → AvitoMessenger.__init__. None / {} —
                использовать дефолты AvitoMessenger.
            outbound_config: H1 — kwargs для OutboundMessenger (max_per_cycle,
                listing_min_age_hours, between_messages_min_sec,
                between_messages_max_sec). Прокидывается через **dict.
        """
        self.driver = driver
        self.wait = wait
        self.account_name = account_name
        self.log = log_func or (lambda *_args, **_kwargs: None)
        self.db = db_manager
        self.llm = llm_classifier
        self.search_filters: dict = search_filters or {}
        self.favorite_rate: float = favorite_rate
        self.call_rate: float = call_rate
        self.max_listings_per_search: int = max_listings_per_search
        self.max_categories_per_browse: int = max_categories_per_browse
        self.max_listings_per_browse: int = max_listings_per_browse
        self.messenger_config: dict = messenger_config or {}
        self.outbound_config: dict = outbound_config or {}

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

    def big_warmup(
        self,
        *,
        num_sites: int | None = None,
        with_yandex_search: bool = True,
        yandex_queries: int = 1,
    ) -> dict:
        """T4: мульти-сайтовый прогрев (3-5 нейтральных сайтов + опц. Yandex).

        Wrapper над warmup.big_warmup. См. документацию там же.

        Полезно:
        - После долгих простоев / смены прокси (сбрасываем "холодный" history).
        - При создании нового аккаунта (B1 warmup-mode).
        - По кнопке /warmup в TG (T12).

        Возвращает stats dict, см. warmup.big_warmup.
        """
        from warmup import big_warmup

        return big_warmup(
            self.driver,
            self.account_name,
            num_sites=num_sites,
            log_func=self.log,
            with_yandex_search=with_yandex_search,
            yandex_queries=yandex_queries,
        )

    def browse_commercial_categories(self, *args, **kwargs) -> Any:
        """
        Открытие коммерческих категорий + ввод keyword'а.
        Wrapper над bot.browse_commercial_categories.
        E2: search_filters (self.search_filters) прокидывается автоматически,
        если явно не передан через kwargs.
        F1: favorite_rate / call_rate из self прокидываются автоматически.
        F12: дневной бюджет на "listings" проверяется ДО входа в browse —
        раньше браузер открывал 3 категории × 3 листинга = 9 листингов
        мимо A2-счётчика, и реальное превышение лимита было ~10-15%.
        """
        # F12: budget guard — если на сегодня лимит листингов исчерпан,
        # пропускаем browse (он всё равно листает листинги). find_and_view
        # уже умеет это проверять — здесь повторяем для browse, иначе цикл
        # «browse → find_and_view» превышает лимит за счёт browse-листингов.
        if self.db is not None:
            from account_state import account_state as _astate

            if not _astate.check_daily_budget(self.account_name, "listings", self.db):
                self.log(
                    self.account_name,
                    "F12: Дневной лимит листингов исчерпан — пропускаем browse.",
                )
                return None

        from bot import browse_commercial_categories

        kwargs.setdefault("search_filters", self.search_filters or None)
        kwargs.setdefault("favorite_rate", self.favorite_rate)
        kwargs.setdefault("call_rate", self.call_rate)
        kwargs.setdefault("max_categories_per_browse", self.max_categories_per_browse)
        kwargs.setdefault("max_listings_per_browse", self.max_listings_per_browse)
        # T20: db_manager прокидывается, чтобы view_listing мог записывать
        # dwell_sec sample'ы.
        kwargs.setdefault("db_manager", self.db)
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

        A2: перед стартом проверяет дневной бюджет на "listings". Если лимит
        исчерпан — возвращает (0, 0, 0) и логирует INFO.
        """
        if self.db is None:
            raise RuntimeError("AvitoClient.find_and_view_commercial_listings requires db_manager")

        # A2 + C2: проверяем дневной бюджет листингов и шлём алерты при 80%/100%
        from account_state import account_state as _astate

        _used_listings = _astate._get_daily_total_from_db(self.account_name, "listings", self.db)
        _alert = _astate.check_budget_alert(self.account_name, "listings", _used_listings)
        _lim = _astate.get_effective_limit(self.account_name, "listings")
        if _alert == "80":
            self.log(
                self.account_name,
                f"C2: листинги {_used_listings}/{_lim} (≥80%) — скоро лимит.",
            )
        elif _alert == "100":
            # C2: лимит только что исчерпан — однократный WARNING (de-dup по дате).
            logger.warning(
                "[%s] C2: дневной лимит листингов исчерпан (%d/%d). Поиск остановлен до завтра.",
                self.account_name,
                _used_listings,
                _lim,
            )
        if not _astate.check_daily_budget(self.account_name, "listings", self.db):
            remaining = _astate.remaining_budget(self.account_name, "listings", self.db)
            self.log(
                self.account_name,
                f"A2: Дневной лимит листингов исчерпан (осталось {remaining}). "
                "Пропускаем поиск до завтра.",
            )
            return 0, 0, 0

        from bot import find_and_view_commercial_listings

        return find_and_view_commercial_listings(
            self.driver,
            self.wait,
            self.account_name,
            self.db,
            search_filters=self.search_filters or None,
            max_listings_per_search=self.max_listings_per_search,
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

        B1: в warmup-режиме (первые N дней нового аккаунта) — пропускаем
        отправку сообщений полностью (только просмотр листингов без LLM).
        A2: перед стартом проверяет дневной бюджет на "messages". Если лимит
        исчерпан — логирует INFO и выходит без работы.

        Внутренне создаёт AvitoMessenger — он держит cohesive state на время
        одной итерации обхода.
        """
        if self.db is None or self.llm is None:
            raise RuntimeError("AvitoClient.process_messages requires db_manager и llm_classifier")

        from account_state import account_state as _astate

        # B1: warmup — не отправляем сообщения (только browse)
        if _astate.is_in_warmup(self.account_name):
            self.log(
                self.account_name,
                "B1: warmup-режим — пропускаем отправку сообщений.",
            )
            return

        # A2 + C2: дневной лимит сообщений + 80%/100%-алерт
        _used_msgs = _astate._get_daily_total_from_db(self.account_name, "messages", self.db)
        _msg_alert = _astate.check_budget_alert(self.account_name, "messages", _used_msgs)
        _msg_lim = _astate.get_effective_limit(self.account_name, "messages")
        if _msg_alert == "80":
            self.log(
                self.account_name,
                f"C2: сообщения {_used_msgs}/{_msg_lim} (≥80%) — скоро лимит.",
            )
        elif _msg_alert == "100":
            # C2: лимит только что исчерпан — однократный WARNING (de-dup по дате).
            logger.warning(
                "[%s] C2: дневной лимит сообщений исчерпан (%d/%d). "
                "Мессенджер остановлен до завтра.",
                self.account_name,
                _used_msgs,
                _msg_lim,
            )
        if not _astate.check_daily_budget(self.account_name, "messages", self.db):
            remaining = _astate.remaining_budget(self.account_name, "messages", self.db)
            self.log(
                self.account_name,
                f"A2: Дневной лимит сообщений исчерпан (осталось {remaining}). "
                "Пропускаем мессенджер до завтра.",
            )
            return

        from avito_messenger import AvitoMessenger

        # F5: messenger_config (dict) прокидывает kwargs реалистичных
        # задержек ответа в AvitoMessenger. Если конфиг пустой — используются
        # дефолты внутри __init__ AvitoMessenger.
        messenger = AvitoMessenger(
            self.driver,
            self.wait,
            self.db,
            self.llm,
            self.account_name,
            **self.messenger_config,
        )
        messenger.process_messages(self.log)

    # ──────────────────────────────────────────────────────────────────────
    # H1: Outbound (proactive контакты к собственникам)
    # ──────────────────────────────────────────────────────────────────────

    def run_outbound_cycle(self, account: dict | None = None) -> int:
        """H1: один цикл proactive-outreach по собственникам коммерческой
        недвижимости. Бот сам идёт по уже-распарсенным листингам класса
        'owner' и пишет ИХ собственникам первое сообщение (LLM-сгенерированное
        с учётом персоны аккаунта).

        Защиты:
        - Глобальный dedup в БД (UNIQUE profile_id в outbound_contacts):
          никогда не пишем одному собственнику дважды, даже разными аккаунтами.
        - В warmup outbound отключён (cycle_dispatch не выпадает + budget=0).
        - При degraded/critical health бюджет режется ×0.5 (как messages).
        - LLM-sanitizer вырезает phone/email/url/messenger перед отправкой.
        - Pre/post-click captcha checks → mark_captcha при попадании.

        Параметры outbound (per-account через accounts.json, fallback config):
            outbound_max_per_cycle (default 2)
            outbound_listing_min_age_hours (default 1.0)
            outbound_between_messages_min_sec / max_sec
        """
        if self.db is None:
            self.log(self.account_name, "H1: outbound пропущен — нет db_manager.")
            return 0

        from account_state import account_state as _astate

        if _astate.is_in_warmup(self.account_name):
            self.log(self.account_name, "B1: warmup — outbound пропускаем.")
            return 0

        # A2 budget на outbound (отдельный от messages).
        used = self.db.get_outbound_count_today(self.account_name)
        try:
            limit = _astate.get_effective_limit(self.account_name, "outbound")
        except Exception:
            limit = 10
        # C2-алерт через стандартный механизм
        try:
            _astate.check_budget_alert(self.account_name, "outbound", used)
        except Exception:
            pass
        if used >= limit:
            self.log(
                self.account_name,
                f"A2/H1: outbound {used}/{limit} — лимит исчерпан, пропускаем.",
            )
            return 0

        from outbound_messenger import OutboundMessenger

        acc = account or {"name": self.account_name}
        kwargs = {}
        if self.outbound_config:
            for k, v in self.outbound_config.items():
                if k.startswith("outbound_"):
                    kwargs[k[9:]] = v
                else:
                    kwargs[k] = v

        m = OutboundMessenger(
            self.driver,
            self.wait,
            self.account_name,
            account=acc,
            db_manager=self.db,
            llm_classifier=self.llm,
            **kwargs,
        )
        return m.run_one_cycle(self.log)
