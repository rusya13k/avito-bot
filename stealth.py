"""
T7 + T8 + T9 + T10: Stealth-инъекции через Chrome DevTools Protocol.

Скрываем 4 вектора детекта автоматизации:

1. T7 — ``navigator.webdriver``: маскируем через defineProperty на прототипе.

2. T8 — ``cdc_*`` глобалы: chromedriver инжектит их при старте. Чистим
   сразу + через setTimeout(0/50/200) — ловим инжект до и после.

3. T9 — WebGL renderer: на VDS нет физической видеокарты, возвращается
   SwiftShader/Mesa, что мгновенный триггер для Avito. Подменяем
   UNMASKED_RENDERER_WEBGL (0x9246) на Intel Iris Xe.

4. T10 — window.chrome: при --remote-debugging-port объект chrome.runtime
   пустой/битый. Эмулируем нормальную структуру.

Все скрипты регистрируются через Page.addScriptToEvaluateOnNewDocument —
выполняются до любого JS загружаемой страницы.

Public API:

    apply_stealth(driver) -> bool

    Вернёт True если CDP-скрипт успешно зарегистрирован, False если
    что-то пошло не так (driver не Chrome, CDP недоступен, ...). Не
    raise — это «бы хорошо чтобы было», но не блокирует логин.

    verify_stealth(driver) -> dict[str, Any]

    Диагностика: возвращает {"webdriver": ..., "cdc_keys": [...]}
    после запроса соответствующих свойств у текущей страницы.
    Полезно из тестового скрипта или /diag-команды.
"""

from __future__ import annotations

import logging
from typing import Any

from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)


# T7 + T8: сводный stealth-скрипт. Запускается до любого JS страницы.
#
# 1. ``Object.defineProperty(navigator, 'webdriver', ...)`` — переопределяем
#    геттер, чтобы он всегда возвращал ``undefined`` (по спеке именно так
#    выглядит свойство в обычном Chrome БЕЗ webdriver). Использовать
#    ``configurable: true`` — иначе на повторных страницах уже defined
#    свойство нельзя переопределить.
#
# 2. Удаление ``cdc_*`` ключей: chromedriver их инжектит при инициализации
#    окна. Поскольку наш стелс-скрипт запускается ДО chromedriver-инжекта,
#    мы вешаем «трамплин»: в момент первого тика после загрузки скрипта
#    делаем cleanup. Также делаем это в IIFE сразу — на случай, если
#    скрипт прилетел уже после chromedriver.
_STEALTH_JS = r"""
(() => {
  // ── T7: navigator.webdriver → undefined ───────────────────────────────
  try {
    if (Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')) {
      Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined,
        configurable: true,
      });
    } else {
      Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
      });
    }
  } catch (e) { /* свойство уже неконфигурируемо — терпимо */ }

  // ── T8: чистим cdc_* глобалы ──────────────────────────────────────────
  const wipeCdc = () => {
    try {
      const keys = Object.getOwnPropertyNames(window);
      for (const k of keys) {
        if (k.startsWith('cdc_')) {
          try { delete window[k]; } catch (_) { /* readonly — пропускаем */ }
        }
      }
    } catch (_) { /* SecurityError на некоторых iframe — пропускаем */ }
  };
  // Сразу — если chromedriver уже успел.
  wipeCdc();
  // И ещё один тик — на случай, если chromedriver инжектит ПОЗЖЕ нашего скрипта.
  try {
    setTimeout(wipeCdc, 0);
    setTimeout(wipeCdc, 50);
    setTimeout(wipeCdc, 200);
  } catch (_) {}

  // ── T9: маскировка WebGL (скрываем программный рендерер VDS) ────────
  try {
    const origGetParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
      if (parameter === 37446) return 'Intel(R) Iris(R) Xe Graphics';
      if (parameter === 37445) return 'Intel Open Source Technology Center';
      return origGetParam.apply(this, arguments);
    };
  } catch (_) {}

  // ── T10: эмуляция window.chrome (remote-debugging-port маскировка) ──
  try {
    if (!window.chrome) {
      window.chrome = {
        app: {
          isInstalled: false,
          InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
          RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
        },
        runtime: {
          OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
          OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
        },
      };
    }
  } catch (_) {}
})();
"""


def apply_stealth(driver: Any) -> bool:
    """T7 + T8: зарегистрировать stealth-скрипт через CDP.

    Скрипт выполняется ДО любого JS каждой загружаемой страницы.
    Идемпотентно: можно звать несколько раз — каждый вызов добавит
    один identifier (Chrome их умеет дедуплицировать по тексту? нет,
    но дубликаты не ломают логику, просто два раза выполнится).

    Args:
        driver: Selenium WebDriver. Должен быть Chrome (или Chromium-based,
            например AdsPower). Для других драйверов — no-op + False.

    Returns:
        True — скрипт успешно зарегистрирован.
        False — драйвер не поддерживает CDP / вызов упал.
    """
    if not hasattr(driver, "execute_cdp_cmd"):
        logger.debug("apply_stealth: driver не поддерживает CDP, skip.")
        return False

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_JS})
        logger.debug("apply_stealth: stealth-скрипт зарегистрирован.")
        return True
    except WebDriverException as exc:
        logger.warning("apply_stealth: CDP-вызов упал — %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 — лучше False, чем падение запуска
        logger.warning("apply_stealth: неожиданная ошибка — %s", exc)
        return False


def verify_stealth(driver: Any) -> dict[str, Any]:
    """Диагностика: проверить, видит ли страница признаки бота.

    Возвращает словарь:
        {
            "webdriver": True/False/None,  # значение navigator.webdriver
            "cdc_keys": ["cdc_..."],       # cdc_*-ключи в window
            "user_agent": "Mozilla/...",   # для контекста
        }

    None в "webdriver" → undefined (то что нам нужно).
    Пустой список в "cdc_keys" → T8 сработал.
    """
    result: dict[str, Any] = {"webdriver": None, "cdc_keys": [], "user_agent": None}
    try:
        result["webdriver"] = driver.execute_script("return navigator.webdriver;")
    except WebDriverException as exc:
        logger.debug("verify_stealth: не смогли прочитать navigator.webdriver — %s", exc)
    try:
        result["cdc_keys"] = (
            driver.execute_script(
                "return Object.getOwnPropertyNames(window).filter(k => k.startsWith('cdc_'));"
            )
            or []
        )
    except WebDriverException as exc:
        logger.debug("verify_stealth: не смогли прочитать cdc_* — %s", exc)
    try:
        result["user_agent"] = driver.execute_script("return navigator.userAgent;")
    except WebDriverException:
        pass
    return result
