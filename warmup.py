"""
T4: Big warmup — мульти-сайтовый прогрев истории браузера.

Раньше прогрев = ya.ru → 2 query → click. Это плохо:
1. Один сайт → один referer в истории → unrealistic для живого пользователя.
2. ya.ru на коммерческих RU-прокси часто отдаёт капчу.
3. Avito видит, что ВСЕ заходы строго через Yandex search — тоже паттерн.

Big warmup: 8-15 минутный сеанс по 3-5 нейтральным сайтам пула:
- ya.ru, mail.ru, dzen.ru, lenta.ru, vk.com — общие (новости/соцсети).
- dom.click, cian.ru — тематический шум для коммерческой недвижимости.

На каждом сайте: 30-90s dwell, 2-4 скролла, ~50% chance кликнуть случайную ссылку.

Использование:
    from warmup import big_warmup
    big_warmup(driver, account_name="acc1", log_func=log)

Или через AvitoClient.big_warmup() (фасад над этой функцией).

API:
- BIG_WARMUP_SITES: list[dict] — пул сайтов с поведенческими профилями.
- _pick_warmup_sites(num: int) -> list[dict] — random.sample без повторов.
- big_warmup(driver, account_name, *, num_sites=None, log_func=None,
             with_yandex_search=True) -> dict — главная функция.
  Возвращает dict со статистикой: {"sites_visited": int, "errors": int,
  "duration_seconds": float, "details": list[dict]}.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

from selenium.webdriver.common.by import By

from human_mouse import human_click as _human_click

logger = logging.getLogger(__name__)


# Пул нейтральных сайтов с поведенческими профилями.
#
# dwell_range — сколько секунд "читаем" страницу (random.uniform).
# scrolls_range — сколько раз скроллим вниз (random.randint).
# click_chance — вероятность кликнуть случайную ссылку.
# thematic — тематический шум для коммерческой недвижимости (опционально).
BIG_WARMUP_SITES: list[dict[str, Any]] = [
    {
        "name": "ya.ru",
        "url": "https://ya.ru",
        "dwell_range": (30, 70),
        "scrolls_range": (1, 3),
        "click_chance": 0.3,
        "thematic": False,
    },
    {
        "name": "mail.ru",
        "url": "https://mail.ru",
        "dwell_range": (40, 90),
        "scrolls_range": (2, 5),
        "click_chance": 0.6,
        "thematic": False,
    },
    {
        "name": "dzen.ru",
        "url": "https://dzen.ru",
        "dwell_range": (60, 120),  # длинные ленты статей
        "scrolls_range": (3, 6),
        "click_chance": 0.7,
        "thematic": False,
    },
    {
        "name": "lenta.ru",
        "url": "https://lenta.ru",
        "dwell_range": (40, 100),
        "scrolls_range": (3, 5),
        "click_chance": 0.6,
        "thematic": False,
    },
    {
        "name": "vk.com",
        "url": "https://vk.com",
        "dwell_range": (30, 60),
        "scrolls_range": (2, 4),
        "click_chance": 0.3,  # без логина мало кликабельных ссылок
        "thematic": False,
    },
    {
        "name": "dom.click",
        "url": "https://dom.click",
        "dwell_range": (40, 90),
        "scrolls_range": (2, 5),
        "click_chance": 0.5,
        "thematic": True,
    },
    {
        "name": "cian.ru",
        "url": "https://www.cian.ru/commercial/",
        "dwell_range": (50, 120),
        "scrolls_range": (3, 6),
        "click_chance": 0.6,
        "thematic": True,
    },
]


def _pick_warmup_sites(num: int) -> list[dict[str, Any]]:
    """Случайно выбирает `num` сайтов из пула без повторов.

    Гарантирует mix:
    - Хотя бы 1 тематический (если num >= 2 и доступен).
    - Остальные — равновероятно из всего оставшегося пула (включая
      ещё не выбранные тематические).

    Параметр `num` ограничивается до len(BIG_WARMUP_SITES).
    """
    n = max(1, min(num, len(BIG_WARMUP_SITES)))
    thematic = [s for s in BIG_WARMUP_SITES if s.get("thematic")]

    chosen: list[dict[str, Any]] = []
    if n >= 2 and thematic:
        chosen.append(random.choice(thematic))

    # Pool = всё кроме уже выбранного, чтобы _pick(N=len(BIG_WARMUP_SITES))
    # возвращал ВЕСЬ пул (включая остальные тематические).
    pool = [s for s in BIG_WARMUP_SITES if s not in chosen]
    remaining = n - len(chosen)
    chosen.extend(random.sample(pool, min(remaining, len(pool))))
    random.shuffle(chosen)  # перемешиваем порядок посещения
    return chosen


def _scroll_page(driver, iters: int) -> None:
    """Простой скролл вниз с человеко-подобными паузами.

    Не использует bot.human_scroll, чтобы избежать круговых импортов
    (warmup → bot → ... ; warmup должен быть низкоуровневым).
    """
    for _ in range(iters):
        amount = random.randint(150, 500)
        try:
            driver.execute_script(f"window.scrollBy(0, {amount});")
        except Exception:
            pass
        time.sleep(random.uniform(0.4, 1.4))


def _click_random_link(driver, account_name: str, log_func: Callable) -> bool:
    """С шансом ~click_chance кликает на случайную ВИДИМУЮ ссылку на текущей
    странице. Возвращает True если клик удался.

    После клика возвращается через driver.back() — мы хотим расширить
    history depth, а не уйти в бесконечную глубину.
    """
    try:
        links = driver.find_elements(By.TAG_NAME, "a")[:60]
    except Exception:
        return False
    if not links:
        return False

    # Фильтруем только видимые с непустым href, не якоря и не #.
    candidates = []
    for a in links:
        try:
            href = (a.get_attribute("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            if not a.is_displayed():
                continue
            candidates.append(a)
        except Exception:
            continue

    if not candidates:
        return False

    target = random.choice(candidates)
    try:
        target_url = target.get_attribute("href") or "<no-href>"
        log_func(account_name, f"    big_warmup click: {target_url[:80]}")
        # T6: human_click сам делает scrollIntoView + Bezier-движение +
        # fallback'и (native → JS click).
        _human_click(driver, target)
        time.sleep(random.uniform(2.0, 5.0))  # немного "прочитали"
        # Возвращаемся обратно — иначе следующий сайт безответно перегрузит state.
        try:
            driver.back()
            time.sleep(random.uniform(1.5, 3.0))
        except Exception:
            pass
        return True
    except Exception as exc:
        log_func(account_name, f"    big_warmup click failed: {type(exc).__name__}: {exc!s:.80}")
        return False


def _visit_site(
    driver,
    site: dict[str, Any],
    account_name: str,
    log_func: Callable,
) -> dict[str, Any]:
    """Посещает один сайт по конфигу. Возвращает per-site stats.

    Stats:
      {"name": str, "ok": bool, "duration_s": float, "scrolled": int, "clicked": bool}
    """
    name = site["name"]
    url = site["url"]
    dwell_lo, dwell_hi = site["dwell_range"]
    scrolls_lo, scrolls_hi = site["scrolls_range"]
    click_chance = float(site.get("click_chance", 0.0))

    log_func(account_name, f"  big_warmup site: {name} ({url})")
    started = time.time()

    # Lazy import bot.safe_get — снижает риск циклов.
    from bot import safe_get

    try:
        ok = safe_get(driver, url, account_name, retries=1)
    except Exception as exc:
        log_func(account_name, f"    {name}: safe_get exception: {type(exc).__name__}: {exc}")
        return {
            "name": name,
            "ok": False,
            "duration_s": time.time() - started,
            "scrolled": 0,
            "clicked": False,
        }

    if not ok:
        log_func(account_name, f"    {name}: navigation failed (block/empty/timeout).")
        return {
            "name": name,
            "ok": False,
            "duration_s": time.time() - started,
            "scrolled": 0,
            "clicked": False,
        }

    # T4-fix: обработка Yandex SmartCaptcha в warmup
    try:
        cur_url = (driver.current_url or "").lower()
    except Exception:
        cur_url = ""
    if "/showcaptcha" in cur_url or "captcha=" in cur_url:
        log_func(account_name, f"    {name}: Yandex captcha detected, solving...")
        try:
            from captcha_solver import solve_yandex_smartcaptcha

            solved = solve_yandex_smartcaptcha(driver, account_name, log_func=log_func)
            if solved:
                log_func(account_name, f"    {name}: captcha solved!")
            else:
                log_func(account_name, f"    {name}: captcha solve failed, skipping.")
                return {
                    "name": name,
                    "ok": False,
                    "duration_s": time.time() - started,
                    "scrolled": 0,
                    "clicked": False,
                }
        except Exception as exc:
            log_func(account_name, f"    {name}: captcha solver error: {type(exc).__name__}: {exc}")
            return {
                "name": name,
                "ok": False,
                "duration_s": time.time() - started,
                "scrolled": 0,
                "clicked": False,
            }

    # Скроллим (имитация чтения).
    iters = random.randint(scrolls_lo, scrolls_hi)
    _scroll_page(driver, iters)

    # Random dwell (середина потраченного времени — сидим и "читаем").
    dwell = random.uniform(dwell_lo, dwell_hi)
    # Часть dwell — равномерно между скроллами; часть — после.
    # Чтобы не дублировать, scroll уже потратил ~iters*1s; добавим остаток.
    leftover = max(0.0, dwell - iters * 1.0)
    if leftover > 0:
        time.sleep(leftover)

    # Опциональный клик.
    clicked = False
    if random.random() < click_chance:
        clicked = _click_random_link(driver, account_name, log_func)

    duration = time.time() - started
    log_func(
        account_name,
        f"    {name}: ok, duration={duration:.1f}s, scrolls={iters}, clicked={clicked}",
    )
    return {
        "name": name,
        "ok": True,
        "duration_s": duration,
        "scrolled": iters,
        "clicked": clicked,
    }


def big_warmup(
    driver,
    account_name: str,
    *,
    num_sites: int | None = None,
    log_func: Callable | None = None,
    with_yandex_search: bool = True,
    yandex_queries: int = 1,
) -> dict[str, Any]:
    """T4: 8-15 минутный мульти-сайтовый прогрев истории браузера.

    Args:
        driver: selenium WebDriver.
        account_name: для логов.
        num_sites: сколько сайтов посетить. None → random.randint(3, 5).
        log_func: callable(account_name, msg). None → no-op.
        with_yandex_search: если True, дополнительно делает 1-2 query
            через yandex_warmup (с обновлёнными T2 селекторами).
        yandex_queries: число поисковых запросов внутри yandex_warmup.

    Returns:
        dict со статистикой:
        {
            "sites_visited": int,         # сколько сайтов УСПЕШНО посетили
            "sites_failed": int,
            "duration_seconds": float,
            "yandex_ok": bool | None,     # None если with_yandex_search=False
            "details": list[dict],        # per-site stats (см. _visit_site)
        }
    """
    log = log_func or (lambda *_a, **_kw: None)
    started = time.time()

    log(account_name, "=== Big warmup 2.0 (multi-site) ===")
    n = num_sites if num_sites is not None else random.randint(3, 5)
    sites = _pick_warmup_sites(n)
    log(account_name, f"  План: {len(sites)} сайтов: " + ", ".join(s["name"] for s in sites))

    details: list[dict[str, Any]] = []
    for site in sites:
        try:
            stats = _visit_site(driver, site, account_name, log)
        except Exception as exc:
            # Никогда не падаем целиком — это анти-fingerprint, не критическая часть.
            log(
                account_name,
                f"  big_warmup site {site['name']} crashed: {type(exc).__name__}: {exc!s:.120}",
            )
            stats = {
                "name": site["name"],
                "ok": False,
                "duration_s": 0.0,
                "scrolled": 0,
                "clicked": False,
                "error": f"{type(exc).__name__}: {exc!s:.80}",
            }
        details.append(stats)

    yandex_ok: bool | None = None
    if with_yandex_search:
        try:
            from bot import yandex_warmup

            yandex_ok = bool(yandex_warmup(driver, None, account_name, num_queries=yandex_queries))
        except Exception as exc:
            log(account_name, f"  big_warmup yandex part failed: {type(exc).__name__}: {exc}")
            yandex_ok = False

    duration = time.time() - started
    sites_ok = sum(1 for d in details if d.get("ok"))
    sites_fail = sum(1 for d in details if not d.get("ok"))

    log(
        account_name,
        f"=== Big warmup done: {sites_ok}/{len(sites)} sites ok, "
        f"yandex={yandex_ok}, total={duration:.1f}s ===",
    )

    return {
        "sites_visited": sites_ok,
        "sites_failed": sites_fail,
        "duration_seconds": duration,
        "yandex_ok": yandex_ok,
        "details": details,
    }
