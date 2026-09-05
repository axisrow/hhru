"""Общие браузерные примитивы и инвариант подтверждённого состояния.

Пустой результат браузерного пути нельзя выдавать за достоверный, если
состояние страницы не удалось подтвердить: timeout, сетевой сбой, анти-бот или
дрейф селектора требуют ошибки либо явной пометки неопределённости.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Browser, BrowserContext, Locator, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("hhru_bot.browser")


class BrowserLaunchError(RuntimeError):
    """Actionable browser-start failure for the CLI/agent boundary."""


def launch_browser(playwright, *, headless: bool) -> Browser:
    """Launch Chromium and classify macOS sandbox failures for the CLI."""
    try:
        return playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
    except PlaywrightError as exc:
        details = str(exc)
        sandbox_markers = (
            "Operation not permitted",
            "Crashpad",
            "NSApplication",
            "NSMenuBarPresentationInstance",
        )
        apple_mach_port_failure = "Permission denied" in details and any(
            marker in details
            for marker in (
                "mach_port_rendezvous_mac",
                "MachPortRendezvousServer",
            )
        )
        if apple_mach_port_failure or any(marker in details for marker in sandbox_markers):
            raise BrowserLaunchError(
                "CODEX_SANDBOX_BROWSER_FAILURE: Chromium заблокирован sandbox "
                "среды выполнения (macOS/Crashpad permission). Это ограничение "
                "среды, а не hh.ru или сессии. Повторите ту же команду вне sandbox "
                "с системным разрешением (elevated); переключение headed/headless "
                "причину не меняет."
            ) from exc
        raise


HH_BASE_URL = "https://hh.ru"
# /applicant/resumes redirects to the profile shell; the dedicated list at
# /applicant/my_resumes is the stable read-only surface used by both
# create_resume.py and delete_resume.py for identity checks and post-action proof.
RESUMES_FULL_LIST_URL = f"{HH_BASE_URL}/applicant/my_resumes"

# Общий словарь состояний браузерной страницы. Эти значения описывают
# подтверждённость результата, а DTO сохраняют исторические bool-поля.
PAGE_STATE = {
    "confirmed": "confirmed",
    "indeterminate": "indeterminate",
    "unreachable": "unreachable",
    "unauthenticated": "unauthenticated",
    "placeholder": "placeholder",
}


class PageStateIndeterminate(RuntimeError):
    """Состояние страницы не подтверждено.

    Пустой результат нельзя выдавать за достоверный, если DOM не подтверждён
    из-за timeout, сетевой ошибки, анти-бота или дрейфа селектора.
    """


class NotAuthenticated(PageStateIndeterminate):
    """The current page does not prove that the hh.ru session is valid."""


class ResumeUnavailable(RuntimeError):
    """hh.ru показал сбойный экран на /resume/{id} (#972).

    Подтверждённое состояние страницы, а не неопределённость: сессия жива
    (страница отрендерена под логином), но запрошенное резюме недоступно.
    Pre-mutation терминальный сигнал (обычный failed/retry): клика не было,
    поэтому исход принципиально не ``uncertain`` (#176) и не
    ``NotAuthenticated`` — сессия действительна.
    """


class ThrottledChannelDetected(PlaywrightTimeoutError):
    """goto_hh timed out AFTER the server answered the navigation request.

    #749: a distinct third class of goto_hh failure, next to net::ERR_* (#748,
    connection never established) and "чистый" TimeoutError with no other
    signal (rendering/anti-bot, stays intentionally unclassified). Here the
    main-document response was observed with an OK status (TCP connect + TLS
    handshake + HTTP headers all succeeded — server is alive and reachable),
    yet the body never finished downloading before GOTO_TIMEOUT_MS. This is
    the typical signature of a throttled/degraded network channel (e.g. a
    dying VPN tunnel).

    Known limit (code-review round 1): it is not a proof. hh.ru's DDoS-Guard
    can serve a challenge interstitial as a 200 document on the same path,
    producing the same signature. The verdict is therefore diagnostic only —
    it never changes control flow, and goto_hh still raises (CLAUDE.md §5,
    fail-closed).

    Subclasses PlaywrightTimeoutError so it keeps flowing through every
    existing `except PlaywrightError`/`except PlaywrightTimeoutError` call
    site unchanged; only cli.py's classification branch inspects it directly.
    """


def _is_navigation_response(response, url: str) -> bool:
    """Whether ``response`` answers the navigation (document) request for ``url``.

    Navigation-ness is taken from Playwright's own structural signal
    (``request.is_navigation_request()`` + ``resource_type == "document"``),
    not guessed from the URL (code-review round 1): hh.ru's SPA issues
    xhr/fetch/prefetch requests against the very same path, and counting
    those as "the server answered the document" would misclassify an
    unanswered navigation as a live channel.

    The path is still compared (without the query string: hh.ru's own
    redirects/tracking params can differ between the requested URL and the
    response URL that reports them), so a document response for a
    *different* page does not count either.
    """
    try:
        request = response.request
        if not request.is_navigation_request():
            return False
        if request.resource_type != "document":
            return False
        response_url = response.url
        status = response.status
    except (PlaywrightError, AttributeError):
        return False
    return urlsplit(response_url).path == urlsplit(url).path and status < 400


def open_hydrated_resume_editor(
    page: Page,
    *,
    trigger_selector: str | Locator,
    editor_selector: str,
    profile_path: str,
    edit_path: str | re.Pattern[str] | None = None,
    click_trigger: bool = False,
    timeout: int = 30_000,
    trigger_error: str = "кнопка редактирования не подтверждена",
    open_error: str = "форма редактирования не открылась",
    wrong_route_error: str = "форма редактирования открыта не для того резюме",
    expected_query: dict[str, str] | None = None,
):
    """Open a resume editor only after its hydrated DOM marker appears.

    Resume pages render edit triggers before React attaches their handlers. A
    click can therefore succeed at the DOM level while doing nothing. Retry
    only while still on the profile page and require the editor marker as the
    positive result; callers may additionally bind the editor to a dedicated
    edit route and/or expected query parameters.
    """
    editor = page.locator(editor_selector)

    # Lightweight unit fakes do not always provide a concrete URL.  Real
    # Playwright pages always expose ``url`` as a string; retain the route
    # guard whenever that production signal is available.
    def current_page_path() -> str | None:
        page_url = page.url
        return urlsplit(page_url).path.rstrip("/") if isinstance(page_url, str) else None

    if click_trigger or editor.count() == 0:
        for attempt in range(2):
            if isinstance(trigger_selector, str):
                trigger = page.locator(trigger_selector)
                if trigger.count() != 1:
                    raise RuntimeError(trigger_error)
            else:
                # Callers passing a row locator have already checked the
                # collection count and selected one row.
                trigger = trigger_selector
            try:
                trigger.click()
                editor.wait_for(state="visible", timeout=timeout)
                break
            except PlaywrightError as exc:
                current_path = current_page_path()
                if attempt or (current_path is not None and current_path != profile_path):
                    raise RuntimeError(open_error) from exc
    expected_path = edit_path or profile_path
    current_path = current_page_path()
    route_matches = current_path is None or (
        bool(expected_path.fullmatch(current_path))
        if isinstance(expected_path, re.Pattern)
        else current_path == expected_path
    )
    if not route_matches:
        raise RuntimeError(wrong_route_error)
    if expected_query is not None:
        page_url = page.url
        if not isinstance(page_url, str):
            raise RuntimeError(wrong_route_error)
        current_query = parse_qs(urlsplit(page_url).query)
        for key, expected_value in expected_query.items():
            actual_values = current_query.get(key, [])
            if expected_value not in actual_values:
                raise RuntimeError(wrong_route_error)
    return editor


def require_authenticated_page(
    page: Page,
    *,
    auth_cookie_check=None,
    login_form_check=None,
) -> None:
    """Fail closed when hh.ru did not return an authenticated page.

    This must be called after navigation: the login form is a server-rendered
    positive signal, while a cookie alone only proves that it remains in the jar.

    ``NotAuthenticated`` is a terminal pre-action signal for callers that may
    otherwise continue through a batch.  It must not be converted into the
    post-click ``uncertain`` state: no irreversible action is possible before
    this check succeeds.
    """
    if not (auth_cookie_check or has_auth_cookie)(page):
        raise NotAuthenticated(
            "cookie hhtoken не найден — сессия истекла (запустите `login`, затем повторите)"
        )
    if (login_form_check or has_login_form)(page):
        raise NotAuthenticated(
            "страница содержит форму входа при наличии hhtoken — сессия отвергнута "
            "сервером (запустите `login`, затем повторите)"
        )


def open_confirmed_resume(page: Page, resume_id: str) -> None:
    """Navigate to and strictly confirm the requested resume identity."""
    if not resume_id:
        raise ValueError("resume_id is required")
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
    require_authenticated_page(page)
    # #972: до identity-чека — сбойный экран держит URL /resume/{id}, поэтому
    # resume_identity_matches его пропускает, а мутация падает таймаутом.
    require_available_resume(page)
    if not resume_identity_matches(page, resume_id):
        raise ValueError("identity резюме не подтверждён")


def labelled_field(page: Page, label: str) -> Locator:
    """Address a form field by its visible label, requiring one exact match.

    hh.ru's Magritte forms render some editors with no ``data-qa`` on the
    ``<input>`` at all — the additional-education editor
    (``/resume/edit/<id>/additionalEducation``) binds its four fields only
    through ``aria-labelledby`` plus a ``<label>`` (live probe 2026-08-30).
    Playwright's ``get_by_label`` resolves exactly that binding, so the visible
    label is the only stable handle those fields have.

    The label is matched exactly and must resolve to a single element: a
    partial match would silently address a different field, which on a write
    form means writing a value into the wrong place.  Ambiguity therefore
    fails closed the same way an unconfirmed ``data-qa`` does.
    """
    field = page.get_by_label(label, exact=True)
    count = field.count()
    if count != 1:
        raise PageStateIndeterminate(f"поле {label!r} не найдено однозначно (совпадений: {count})")
    return field


def resume_identity_matches(page: Page, resume_id: str) -> bool:
    """Return whether the current URL is exactly the requested resume page."""
    path = urlsplit(page.url).path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    return len(parts) >= 2 and parts[-2] == "resume" and parts[-1] == resume_id


# Потолок ожидания навигации по hh.ru. Дефолт Playwright 30с — hh.ru под
# DDoS-Guard/нагрузкой грузится 33с+ (см. #80), и goto падает. Ставим
# context-wide через set_default_navigation_timeout — покрывает ВСЕ goto/
# wait_for_url одним источником (включая двухшаговую навигацию формы отклика,
# CLAUDE.md п.4, #179), без явного timeout в каждом вызове. 90с (не 120с как в
# auth.py раньше): достаточно для медленного hh.ru, но не слепое зависание на
# 1.5 мин при реально упавшем запросе.
GOTO_TIMEOUT_MS = 90_000

# Retry goto: DDoS-Guard регулярно пропускает запрос со 2-й попытки. 3 попытки,
# линейный backoff 2с → 4с.
_GOTO_MAX_ATTEMPTS = 3
_GOTO_BACKOFF_SECONDS = 2.0


def goto_hh(page: Page, url: str, *, ready_selector: str | None = None) -> None:
    """page.goto с retry и готовностью страницы hh.ru (#80).

    Базовый потолок ожидания задаёт context-wide set_default_navigation_timeout
    (GOTO_TIMEOUT_MS), поэтому тут timeout не дублируем — DRY.

    Поведение:
    1. До ``_GOTO_MAX_ATTEMPTS`` попыток goto(wait_until='domcontentloaded').
       Ловим PlaywrightTimeoutError/PlaywrightError — hh.ru под DDoS-Guard
       часто отвечает со 2-й попытки; на последней попытке ошибку пробрасываем
       (как обычный goto без retry).
    2. ``ready_selector`` (опц.) — после удачного goto дождаться конкретного
       ``data-qa`` маркера страницы (короткий GOTO_TIMEOUT_MS, если не задан
       context-timeout), вместо ненадёжного networkidle. None — не ждать.
    3. (#749) Если сам ``page.goto`` (не ``ready_selector``) провалился
       таймаутом, но за это время пришёл response навигационного document-
       запроса со статусом < 400 — сервер живой, узкое место в скорости
       передачи тела. Такую ошибку оборачиваем в ``ThrottledChannelDetected``
       вместо проброса как есть. ``ready_selector`` — отдельная проверка ПОСЛЕ
       успешного goto; её таймаут classification'у не подлежит (code-review
       round 1: канал там уже доказано живой, причина — дрейф селектора или
       анти-бот интерстишл, и должна остаться неопределённой, см. #748).

    domcontentloaded НЕ меняем (рекомендация референсов #80: DDoS-Guard держит
    сеть активной, networkidle может не сработать).
    """
    last_error: Exception | None = None
    for attempt in range(1, _GOTO_MAX_ATTEMPTS + 1):
        # #749: слушаем response ТОЛЬКО чтобы отличить "сервер ответил, но
        # тело не докачалось" (throttled-канал) от "goto не увидел ответ
        # вообще" (анти-бот/дрейф селектора/сеть недоступна) — сигнал уже
        # идущего page.goto(), НЕ отдельный сетевой запрос (запрет CLAUDE.md/#747).
        response_observed = False

        def _on_response(response, _url=url) -> None:
            nonlocal response_observed
            if _is_navigation_response(response, _url):
                response_observed = True

        # Best-effort: unit test doubles for Page (bump/apply/etc.) commonly
        # implement only goto/locator, not the full event-emitter surface.
        # The throttled-channel signal is a diagnostic nicety layered on top
        # of retry — its absence must not break goto_hh for callers whose
        # fake Page has no .on/.remove_listener. Require BOTH methods: a
        # double with .on but no .remove_listener (test_copy_resume_browser.py
        # StubPage, code-review round 1) would register the listener fine and
        # then blow up in `finally` with an uncaught AttributeError — on the
        # success path too, not just on error, since `return` still runs it.
        listener_attached = hasattr(page, "on") and hasattr(page, "remove_listener")
        if listener_attached:
            page.on("response", _on_response)
        try:
            try:
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except (PlaywrightTimeoutError, PlaywrightError) as exc:
                    # #749 code-review round 1: throttled-классификация
                    # применима ТОЛЬКО к самому goto() (докачке тела
                    # документа). ready_selector — отдельная проверка ниже,
                    # её таймаут НЕ должен попадать в эту ветку: это дрейф
                    # селектора/анти-бот на уже докачанной странице, канал
                    # тут ни при чём.
                    #
                    # Cycle-review PR #760, Codex finding: ThrottledChannelDetected
                    # документирован как "timed out AFTER" (docstring выше) — узкое
                    # место строго в скорости докачки тела. Не-timeout PlaywrightError
                    # (напр. "Navigation interrupted by another navigation" —
                    # конкурирующая JS-навигация) с наблюдённым response — не
                    # throttling, а другая причина; переквалификация тут была бы
                    # недостоверной диагностикой. Поэтому isinstance проверяем ДО
                    # response_observed, а не полагаемся на общий PlaywrightError.
                    if (
                        isinstance(exc, PlaywrightTimeoutError)
                        and response_observed
                        and "net::ERR_" not in str(exc)
                    ):
                        raise ThrottledChannelDetected(str(exc)) from exc
                    raise
                if ready_selector:
                    page.locator(ready_selector).wait_for(timeout=GOTO_TIMEOUT_MS)
                return
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                last_error = exc
                if attempt < _GOTO_MAX_ATTEMPTS:
                    wait = _GOTO_BACKOFF_SECONDS * attempt
                    logger.warning(
                        "goto не прошёл (попытка %d/%d): %s — повтор через %.1fs",
                        attempt,
                        _GOTO_MAX_ATTEMPTS,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
        finally:
            if listener_attached:
                try:
                    page.remove_listener("response", _on_response)
                except (PlaywrightError, AttributeError):
                    pass
    # Последняя попытка провалилась — пробрасываем, как обычный goto.
    assert last_error is not None
    raise last_error


@contextmanager
def launch_context(
    storage_state_file: Path | None,
    headless: bool = False,
    user_agent: str | None = None,
    viewport: dict | None = None,
):
    """Контекст браузера с сохранённой сессией.

    user_agent: None (по умолчанию) — пусть Playwright ставит свой родной UA;
    строка — переопределить UA (если требует hh.ru). Хардкода Chrome/xxx здесь
    намеренно нет.

    viewport: None (по умолчанию) — дефолтные 1366x900; dict — переопределить
    для конкретного потока (например, высокий viewport в upload-photo: шапка
    модалки назначения стабильно вне дефолтного вьюпорта, см. resume_photo).
    """
    with sync_playwright() as p:
        # --disable-blink-features=AutomationControlled убирает главный флаг, по
        # которому hh.ru (DDoS-Guard) держит кнопку входа disabled в Playwright.
        # Приём из YAMAKAYAMACO/hh-autoresponder (рабочий против hh.ru).
        browser: Browser = launch_browser(p, headless=headless)
        context_kwargs: dict = {
            "viewport": viewport or {"width": 1366, "height": 900},
            "locale": "ru-RU",
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        if storage_state_file is None:
            logger.info("Запущен чистый браузерный контекст без сохранённой сессии")
        elif storage_state_file.exists():
            context_kwargs["storage_state"] = str(storage_state_file)
            logger.info("Загружена сохранённая сессия: %s", storage_state_file)
        else:
            logger.warning(
                "Файл сессии не найден (%s) — потребуется вход в аккаунт", storage_state_file
            )

        context: BrowserContext = browser.new_context(**context_kwargs)
        # #80: потолок навигации context-wide — покрывает ВСЕ goto/wait_for_url
        # единым источником (включая двухшаговую навигацию формы отклика, #179).
        context.set_default_navigation_timeout(GOTO_TIMEOUT_MS)
        # Убираем navigator.webdriver и подделываем window.chrome — без этого
        # hh.ru детектит Playwright и не активирует кнопку входа. Приём из
        # YAMAKAYAMACO/hh-autoresponder/manual_login.py.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = {runtime: {}};"
        )
        # Must be present before the first navigation/click: transient UI is
        # often unmounted before a post-action DOM snapshot can inspect it.
        from .transient_overlays import _SCRIPT

        context.add_init_script(_SCRIPT)
        try:
            yield context
        finally:
            cleanup_error = None
            for resource in (context, browser):
                try:
                    resource.close()
                except Exception as exc:
                    message = str(exc)
                    target_closed = (
                        "TargetClosedError" in message
                        or "Target page, context or browser has been closed" in message
                        or "Connection closed while reading from the driver" in message
                    )
                    if not target_closed and cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                raise cleanup_error


def has_auth_cookie(page: Page) -> bool:
    """Проверка авторизации по единственному надёжному маркеру: ``hhtoken``."""
    cookies = page.context.cookies()
    return any(c.get("name") == "hhtoken" for c in cookies)


# Подтверждено анонимным curl-дампом https://hh.ru/account/login (2026-08-12).
# URL не используем: валидная сессия может сохранить /account/login в URL
# после навигации (регрессия #140/#145). Серверная страница входа содержит
# этот маркер формы, авторизованная страница — нет. Намеренно только
# позитивный сигнал: отсутствие формы НЕ подтверждает авторизацию, если
# страница ещё рендерится или селектор устарел.
LOGIN_FORM = "[data-qa='account-login-form']"


def has_login_form(page: Page) -> bool:
    """Отдал ли сервер серверную форму входа (устаревший/отозванный hhtoken).

    ``has_auth_cookie`` подтверждает только наличие cookie в браузерном jar —
    не то, что сервер принял сессию на текущей странице (issue #147: истёкший/
    отозванный токен может остаться в jar без явного Set-Cookie на очистку).
    Комбинация ``has_auth_cookie() and not has_login_form(page)`` — надёжный
    маркер: cookie есть, а сервер не отверг сессию формой входа.
    """
    return page.locator(LOGIN_FORM).count() > 0


# Подтверждено живым DOM за логином 2026-09-05 (issue #972, read-only live-замер
# tests/test_resume_error_banner_live.py, дамп
# data/logs/972_resume_error_banner_000000.html): несуществующее/удалённое
# резюме по прямому URL /resume/{id} рендерит сбойный экран
# <div class="attention attention_bad">Произошла ошибка. Возникли неполадки,
# но мы уже работаем над их устранением.</div> — data-qa на баннере НЕТ (как у
# error boundary «Problem fetching content», #802), поэтому стабильный
# bloko-класс + точный текст — двойной признак, отсекающий другие
# attention_bad-баннеры на этой же странице. Экран тот же и для полностью
# несуществующего id, и для удалённого резюме (скриншот владельца из #972).
RESUME_ERROR_BANNER = "div.attention.attention_bad"
RESUME_ERROR_BANNER_TEXT = "Произошла ошибка"
# Формулировка по #972: констатирует наблюдаемый факт (баннер) и называет
# вероятную причину, НЕ утверждая «удалено» — баннер является общим сбойным
# экраном hh.ru. Без сверки со списком резюме.
RESUME_UNAVAILABLE_REASON = (
    "резюме недоступно: hh.ru показал баннер ошибки "
    "(наиболее вероятная причина — резюме удалено владельцем; "
    "баннер — общий сбойный экран hh.ru, удаление не доказано)"
)


def has_resume_error_banner(page: Page) -> bool:
    """Сбойный экран hh.ru на /resume/{id}: видимый баннер ошибки (#972).

    Ошибка чтения селектора — не доказательство отсутствия баннера: детектор
    молчит, вызывающий путь продолжает свой обычный fail-closed (его
    «кнопка/форма не найдена» останется прежним отказом). Намеренно
    позитивный сигнал, как у ``has_login_form``. AttributeError — для
    unit-факов без ``filter`` (паттерн ``detect_antibot_on_page``).
    """
    try:
        return (
            page.locator(RESUME_ERROR_BANNER).filter(has_text=RESUME_ERROR_BANNER_TEXT).count() > 0
        )
    except (AttributeError, PlaywrightError):
        return False


def require_available_resume(page: Page) -> None:
    """Терминальный pre-mutation отказ, если страница резюме — сбойный экран.

    Вызывается сразу после навигации на /resume/{id} и проверки сессии, до
    любого клика/поиска формы: URL при баннере остаётся /resume/{id}, поэтому
    identity-проверки его не отсекают.
    """
    if has_resume_error_banner(page):
        raise ResumeUnavailable(RESUME_UNAVAILABLE_REASON)


# #825: hh.ru рендерит информер о cookie-политике на каждой свежей навигации
# (нет флага согласия в storage_state) с кнопкой-акцептом, позиционированной
# fixed внизу экрана. Живой прогон подтвердил (2026-08-30, headless и headed):
# баннер может оставаться в DOM 40+ секунд после загрузки страницы — время
# исчезновения не привязано ни к прошедшему времени с goto, ни к какому-либо
# взаимодействию со страницей, кроме собственного клика/таймера hh.ru. Пока он
# виден, он физически перекрывает элементы в нижней части вьюпорта (в частности
# кнопку Save формы образования, live-подтверждено скриншотом) — клик
# Playwright по перекрытому элементу тем не менее не бросает исключение (сам
# DOM-узел actionable), поэтому это молчаливый no-op, а не видимая ошибка.
# data-qa кнопки подтверждён живым DOM-дампом (``el.outerHTML`` на кнопке с
# текстом "Понятно"). Дисмисс — best-effort и не бросает исключение, если
# баннер уже закрыт или не появился вовсе: сам факт его отсутствия не является
# ошибкой ни на одной странице hh.ru.
COOKIE_BANNER_ACCEPT = "[data-qa='cookies-policy-informer-accept']"


def dismiss_cookie_banner(page: Page, *, timeout: int = 2_000) -> None:
    """Закрыть информер cookie-политики, если он сейчас показан.

    Вызывается перед любым кликом, который может быть перекрыт баннером
    (см. комментарий у ``COOKIE_BANNER_ACCEPT``). Отсутствие баннера — штатный
    случай, а не ошибка: большинство страниц открываются с уже принятым
    согласием (флаг живёт в localStorage/cookie, сохранённых в storage_state).
    """
    button = page.locator(COOKIE_BANNER_ACCEPT)
    # count() == 0 -- быстрая синхронная проверка без ожидания. Без неё общий
    # случай (баннера никогда не было -- большинство страниц) платил бы полный
    # `timeout` на каждый вызов: click() на пустом локаторе ждёт появления
    # элемента вплоть до timeout, а не завершается сразу. Гонка между этой
    # проверкой и click() ниже (баннер исчез сам между ними) не опасна --
    # click() на уже отсутствующем/detached узле просто попадёт в тот же
    # except ниже.
    if button.count() == 0:
        return
    try:
        button.first.click(timeout=timeout)
    except PlaywrightError:
        # Не мешаем основному потоку диагностикой ради необязательного шага —
        # если клик не удался, дальнейший код всё равно проверит реальный
        # результат своего действия (fail-closed на своём собственном уровне).
        pass


def wait_for_react_hydration(page: Page, selector: str, *, timeout_ms: int) -> bool:
    """Ждать появления React-привязки (__reactFiber$/__reactProps$) на элементе.

    Инвариант «visible != гидратирован» (#858): SSR-разметка видима и
    доступна ДО гидратации, а клики/change-события в этом окне молча
    теряются — обработчиков ещё нет. Наличие instance-ключей —
    детерминированный сигнал готовности. Возвращает False по таймауту
    (включая «элемента нет вовсе»), не бросает: решение об отказе — за
    вызывающим. Чтение ``Object.keys`` — чтение DOM, не внутренний API.

    Точечный вариант с локатором и доменной ошибкой —
    ``experience._wait_for_react_hydration``; общий хелпер здесь — для
    новых экранов, чтобы детектор не плодился в копиях.
    """
    try:
        page.wait_for_function(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const keys = Object.keys(el);
                return keys.some((k) => k.startsWith('__reactFiber'))
                    && keys.some((k) => k.startsWith('__reactProps'));
            }""",
            arg=selector,
            timeout=timeout_ms,
        )
    except PlaywrightError:
        return False
    return True


def dump_page_html(page: Page, stem: str) -> Path | None:
    """Best-effort дамп HTML страницы в LOG_DIR для разбора инцидента.

    Общий хелпер семейства ``_dump_*_failure`` (resume_education/
    resume_position): дубли выключенной диагностики недопустимы, но и
    падать из-за неё нельзя — любая OSError глушится, возвращается None.
    """
    from .logging_setup import LOG_DIR

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}.html"
        path.write_text(page.content(), encoding="utf-8")
        return path
    except OSError:
        return None
