"""Копирование резюме на hh.ru («Дублировать» в терминах hh.ru, #116).

Браузерный шаг команды copy-resume. Клик по пункту «Дублировать» отправляет
POST /applicant/resumes/clone?resume=<hash>, после чего фронт hh.ru переходит
на страницу нового резюме. Кнопка НЕ рендерится, когда достигнут лимит резюме
hh.ru (~20) — это единственный видимый признак лимита, поэтому её отсутствие
трактуем как отказ, а не как «селектор устарел».

Fail-closed (#33): карточка резюме привязывается к resume_id через
resume-card-link-<hash> (identity-bound); новый resume_id обязан отличаться от
исходного и определяться однозначно, иначе — неуспех без угадывания.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import (
    HH_BASE_URL,
    RESUMES_FULL_LIST_URL,
    PageStateIndeterminate,
    goto_hh,
    has_auth_cookie,
    has_login_form,
)
from .config import ResumeConfig
from .negotiations_probe import parse_initial_state

# Reuse the established auth-state exception rather than defining a second
# copy-resume-only variant; the command layer already treats this shared
# PageStateIndeterminate subtype as an expired-session failure.
from .responses import NotAuthenticated
from .selector_groups.resume_list import (
    RESUME_DUPLICATE_INLINE,
    RESUME_DUPLICATE_MENU_ITEM,
    RESUME_LIST_ACTION_MORE,
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_TPL,
    RESUME_LIST_CARD_TITLE,
    RESUME_PROFILE_READY,
)

logger = logging.getLogger("hhru_bot.copy_resume")

# /applicant/resumes now redirects to the profile shell.  Keep the historical
# name as the module-level seam used by callers/tests, but point every list
# operation at the dedicated, stable list surface (#311).
RESUMES_LIST_URL = RESUMES_FULL_LIST_URL
COPY_TIMEOUT_MS = 30_000
PROFILE_STALL_SECONDS = 15.0
PROFILE_ABSOLUTE_TIMEOUT_SECONDS = 300.0
PROFILE_POLL_MS = 250
MENU_CLICK_TIMEOUT_MS = 1_000

_RESUME_HASH_RE = re.compile(r"/resume/([0-9a-f]{32,40})")
_CARD_LINK_PREFIX = "resume-card-link-"


def _monotonic() -> float:
    """Тестируемая обёртка над monotonic clock."""
    return time.monotonic()


@dataclass
class CopyResumeResult:
    resume_id: str
    success: bool
    new_resume_id: str = ""
    reason: str = ""
    # True: копия могла быть создана, но это не подтверждено (post-WRITE, #176/#207).
    # Записывается в actions как `uncertain`, а не `failed`, чтобы повторный запуск
    # не выглядел безопасным (зеркалит DeleteResumeResult.uncertain, #293).
    uncertain: bool = False


@dataclass
class ResumeCard:
    resume_id: str
    title: str
    url: str
    status: str | None = None
    ssr_unavailable: bool = False  # True если SSR данные недоступны или некорректны


class ResumeListIndeterminate(PageStateIndeterminate):
    """Не удалось подтвердить состояние /applicant/resumes (#135, Codex review).

    Ни одна карточка не появилась за COPY_TIMEOUT_MS после навигации. Без
    подтверждённого маркера «список действительно пуст» это неотличимо от
    честно пустого аккаунта, анти-бот/интерстишл-страницы, деградировавшей
    загрузки или дрейфа RESUME_LIST_CARD. list_resume_cards поэтому не молчит
    и не выдаёт пустой список за подтверждённый факт — поднимает это
    исключение, чтобы вызывающий код (list_resumes.py live-дефолт, #320) сообщил
    «состояние не подтверждено», а не соврал «резюме не найдено»."""


class ResumeProfileReadinessObserver:
    """Наблюдает сигналы микрофронтенда профиля только до WRITE-клика.

    SSR-карточка сама по себе не подтверждает готовность: React может упасть на
    hydration (#418/#423) и дорисовывать профиль на клиенте. Observer отличает
    восстановившуюся страницу от зависания/сетевого отказа. URL в диагностике
    всегда очищается от query и fragment.
    """

    _HYDRATION_MARKERS = (
        "react error #418",
        "react error #423",
        "remoteentryexports is undefined",
    )

    def __init__(self, page: Page) -> None:
        self.page = page
        self.last_progress_at = _monotonic()
        self.hydration_error = ""
        self.request_failure = ""
        self._attached = False

    def attach(self) -> None:
        if self._attached:
            return
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)
        self.page.on("request", self._on_request_progress)
        self.page.on("response", self._on_response)
        self.page.on("requestfinished", self._on_request_progress)
        self.page.on("requestfailed", self._on_request_failed)
        self._attached = True

    def reset_attempt(self) -> None:
        self.last_progress_at = _monotonic()
        self.hydration_error = ""
        self.request_failure = ""

    def confirm_ready(self) -> None:
        """Readiness сильнее исторических ошибок: client-render восстановился."""
        self.hydration_error = ""
        self.request_failure = ""
        self._touch()

    @staticmethod
    def _is_profile_resource(url: str) -> bool:
        lowered = url.lower()
        return "resume-profile-front" in lowered or "remote.resume_profile_front" in lowered

    @staticmethod
    def _safe_url(url: str) -> str:
        try:
            parts = urlsplit(url)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except ValueError:
            return "<invalid-url>"

    def _touch(self) -> None:
        self.last_progress_at = _monotonic()

    def _record_hydration_error(self, value) -> None:
        text = str(value)
        lowered = text.lower()
        for marker in self._HYDRATION_MARKERS:
            if marker in lowered:
                # Стабильный код вместо сырого console text: там могут быть URL
                # с query, cookies и прочая не предназначенная для audit строка.
                self.hydration_error = marker
                self._touch()
                return

    def _on_console(self, message) -> None:
        self._record_hydration_error(getattr(message, "text", message))

    def _on_page_error(self, error) -> None:
        self._record_hydration_error(error)

    def _on_request_progress(self, request) -> None:
        url = str(getattr(request, "url", ""))
        if self._is_profile_resource(url):
            self._touch()

    def _on_response(self, response) -> None:
        url = str(getattr(response, "url", ""))
        if not self._is_profile_resource(url):
            return
        self._touch()
        status = int(getattr(response, "status", 0) or 0)
        if status >= 400:
            self.request_failure = f"{self._safe_url(url)} (HTTP {status})"

    def _on_request_failed(self, request) -> None:
        url = str(getattr(request, "url", ""))
        if self._is_profile_resource(url):
            self.request_failure = self._safe_url(url)
            self._touch()

    def failure_reason(self, *, stalled: bool = False, absolute: bool = False) -> str:
        if self.request_failure:
            return f"profile_front_request_failed: {self.request_failure}; копия не создавалась"
        if self.hydration_error:
            return f"hydration_error: {self.hydration_error}; копия не создавалась"
        if absolute:
            return (
                "profile_stalled: достигнут аварийный предел ожидания профиля; копия не создавалась"
            )
        if stalled:
            return "profile_stalled: профиль hh.ru перестал прогружаться; копия не создавалась"
        return "duplicate_action_missing: действие «Дублировать» не найдено; копия не создавалась"


def _wait_duplicate_action(
    page: Page,
    card,
    observer: ResumeProfileReadinessObserver,
    *,
    absolute_deadline: float,
) -> tuple[Locator | None, str]:
    """Ждёт фактическое действие ``Дублировать`` для целевой карточки.

    Текст «N подходящих вакансий» — лишь дополнительное подтверждение того,
    что client render завершился. Он не является обязательным условием:
    единственный разрешающий WRITE маркер — ровно одно действие
    ``Дублировать`` после безопасного открытия identity-bound меню ``...``.
    Меню hh.ru рендерится через portal вне карточки, поэтому его действие
    ищется глобально, а inline-вариант — внутри карточки; их объединение
    обязано дать ровно одно совпадение.
    """
    more = card.locator(RESUME_LIST_ACTION_MORE)
    # Both menu implementations are rendered outside the target card: the
    # opened action sheet is a portal, including the live ``resume-dublicate``
    # variant on /applicant/my_resumes.  The target card remains identity-bound
    # through ``more``; after that safe menu open, require exactly one global
    # duplicate action instead of incorrectly scoping the portal to the card.
    duplicate = page.locator(f"{RESUME_DUPLICATE_MENU_ITEM}, {RESUME_DUPLICATE_INLINE}")
    profile_ready = page.locator(RESUME_PROFILE_READY)
    menu_opened = False
    ready_seen = False

    while True:
        if observer.request_failure:
            return None, observer.failure_reason()

        more_count = more.count()
        if more_count > 1:
            return None, (
                "duplicate_action_missing: меню действий определяется неоднозначно; "
                "копия не создавалась"
            )

        ready_count = profile_ready.count()
        if ready_count >= 1 and not ready_seen:
            ready_seen = True
            # Этот маркер подтверждает, что React сумел завершить client render,
            # поэтому историческая #418/#423 больше не описывает текущий state.
            observer.confirm_ready()
        # Кликаем по «...» ровно один раз за попытку, как только кнопка стала
        # однозначной. Привязка повтора к сетевому progress-тику (как было
        # раньше) кликала снова при КАЖДОМ фоновом запросе профиля — а не
        # только при событиях, относящихся к рендеру самого меню — и на
        # реальном toggle-дропдауне hh.ru закрывала уже открытое меню.
        if more_count == 1 and not menu_opened:
            try:
                more.first.click(timeout=MENU_CLICK_TIMEOUT_MS)
                menu_opened = True
            except PlaywrightError:
                # SSR-кнопка может уже быть видна, но ещё не быть кликабельной.
                # Watchdog ниже отличит восстановление от окончательного stall.
                pass

        duplicate_count = duplicate.count()
        if duplicate_count == 1:
            observer.confirm_ready()
            return duplicate.first, ""
        if duplicate_count > 1:
            return None, (
                f"кнопка «Дублировать» определяется неоднозначно "
                f"({duplicate_count} совпадений на странице) — "
                "останавливаюсь (fail-closed)"
            )

        now = _monotonic()
        if now >= absolute_deadline:
            return None, observer.failure_reason(absolute=True)
        if now - observer.last_progress_at >= PROFILE_STALL_SECONDS:
            if ready_seen:
                return None, observer.failure_reason()
            return None, observer.failure_reason(stalled=True)
        page.wait_for_timeout(PROFILE_POLL_MS)


def _reload_resumes_list(page: Page) -> str:
    """Единственный разрешённый recovery reload, всегда до WRITE-клика."""
    try:
        page.reload(wait_until="domcontentloaded")
    except (PlaywrightTimeoutError, PlaywrightError):
        return "profile_stalled: recovery reload hh.ru не завершился; копия не создавалась"
    if has_login_form(page):
        raise NotAuthenticated(
            "страница содержит форму входа после recovery reload — сессия отвергнута"
        )
    return ""


def _card_hashes(page: Page) -> set[str]:
    """Хэши всех резюме в списке /applicant/resumes (для diff до/после)."""
    hashes: set[str] = set()
    for link in page.locator(f"[data-qa^='{_CARD_LINK_PREFIX}']").all():
        qa = link.get_attribute("data-qa") or ""
        if qa.startswith(_CARD_LINK_PREFIX):
            hashes.add(qa[len(_CARD_LINK_PREFIX) :])
    return hashes


def list_resume_cards(
    page: Page, *, navigate: bool = True, url: str | None = None
) -> list[ResumeCard]:
    """Список резюме аккаунта: хэш + название + URL + статус (#135, #315).

    READ-only: только goto + чтение DOM/SSR, ничего не кликается и не отправляется.
    ``url`` — целевая страница списка резюме (по умолчанию ``RESUMES_LIST_URL``;
    для полного списка, включая черновики, используйте ``RESUMES_FULL_LIST_URL``).
    """
    target_url = url if url is not None else RESUMES_LIST_URL
    if navigate:
        logger.info("Открываю список резюме: %s", target_url)
        goto_hh(page, target_url)

    cards_locator = page.locator(RESUME_LIST_CARD)
    if cards_locator.count() == 0:
        try:
            cards_locator.first.wait_for(timeout=COPY_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise ResumeListIndeterminate(
                "карточки резюме не появились за отведённое время — состояние "
                "/applicant/resumes не подтверждено (timeout, анти-бот/"
                "интерстишл-страница или дрейф селектора RESUME_LIST_CARD)"
            ) from None

    status_by_hash: dict[str, str] = {}
    ssr_unavailable = False  # Флаг: SSR данные недоступны или некорректны
    try:
        state = parse_initial_state(page.content())
    except (ValueError, AttributeError, PlaywrightError, PlaywrightTimeoutError):
        # PlaywrightError/PlaywrightTimeoutError могут возникнуть при закрытии страницы,
        # сбое renderer или деградации браузера. Помечаем SSR как недоступный.
        state = None
        ssr_unavailable = True

    if not ssr_unavailable and isinstance(state, dict):
        resumes = state.get("applicantResumes")
        if not isinstance(resumes, list):
            # applicantResumes отсутствует или имеет неправильный тип
            ssr_unavailable = True
        else:
            for item in resumes:
                attrs = item.get("_attributes") if isinstance(item, dict) else None
                if isinstance(attrs, dict):
                    resume_hash = attrs.get("hash")
                    status = attrs.get("status")
                    if resume_hash and status is not None:
                        status_by_hash[str(resume_hash)] = str(status)

    cards: list[ResumeCard] = []
    for card in cards_locator.all():
        resume_id = ""
        for link in card.locator(f"[data-qa^='{_CARD_LINK_PREFIX}']").all():
            qa = link.get_attribute("data-qa") or ""
            if qa.startswith(_CARD_LINK_PREFIX):
                resume_id = qa[len(_CARD_LINK_PREFIX) :]
                break
        if not resume_id:
            # Codex adversarial review (PR #322): вложенный селектор ссылки-хэша
            # — второй, независимо дрейфующий локатор (RESUME_LIST_CARD уже
            # подтверждён строкой выше, но карточка внутри может не содержать
            # ожидаемую ссылку из-за разметки/дрейфа). Молчаливый skip здесь
            # раньше выдавал частичный список за полный (list_resumes.py #320
            # опирается на полноту cards для orphans/«резюме не найдено» —
            # неотличимо от честно пустого/полного аккаунта). raise вместо
            # continue: неподтверждённое состояние, не тихая потеря карточки.
            raise ResumeListIndeterminate(
                "карточка резюме найдена, но не удалось прочитать её resume_id "
                "(вложенный селектор ссылки-хэша не совпал — дрейф разметки) — "
                "список резюме не подтверждён"
            )

        title = ""
        title_locator = card.locator(RESUME_LIST_CARD_TITLE)
        if title_locator.count() == 1:
            title = (title_locator.first.inner_text() or "").strip()

        url = f"{HH_BASE_URL}/resume/{resume_id}"
        cards.append(
            ResumeCard(
                resume_id=resume_id,
                title=title,
                url=url,
                status=status_by_hash.get(resume_id),
                ssr_unavailable=ssr_unavailable,
            )
        )
    return cards


class ResumeIdMapping(dict[str, str]):
    """Hash-to-numeric-id mapping with the status read from the same SSR."""

    def __init__(self, *args, statuses: dict[str, str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.statuses = statuses


def resolve_numeric_resume_ids(page: Page) -> ResumeIdMapping | None:
    """Маппинг «хэш резюме → числовой id hh.ru» с /applicant/resumes (#212).

    Домены id у hh.ru несовместимы: конфиг адресует резюме хэшем из URL
    (``ResumeConfig.resume_id``), а SSR /applicant/negotiations подписывает
    темы числовым ``topicList[].resumeId``. Прямое сравнение этих строк всегда
    даёт «чужое» — верификатор apply из-за этого не мог вернуть found с
    момента PR #209 (false negative #3, #212). Живая проба 2026-08-16
    (data/logs/probe212_*.json): оба id лежат рядом в SSR списка резюме,
    ``applicantResumes[]._attributes.{hash,id}`` — тот же reader-принцип, что
    у ``list_resume_cards`` (goto + чтение, ничего не кликается).

    None — прочитать не удалось (сессия, рендер, структура): вызывающий код
    обязан трактовать это как «атрибуция резюме недоступна» (fail-closed в
    apply/verify), а не как «резюме нет». Дополнительно warns о резюме в
    статусе not_finished: форма отклика такие не предлагает (SSR формы:
    ``unfinishedResumeIds``), и отклик уходит с другого резюме аккаунта, хотя
    история пишется под конфиг-резюме — это должно быть видно в логах.
    """
    logger.info("Открываю список резюме для маппинга id: %s", RESUMES_LIST_URL)
    try:
        goto_hh(page, RESUMES_LIST_URL)
        if not has_auth_cookie(page) or has_login_form(page):
            logger.warning("[RESUME-ID] сессия не авторизована — маппинг id недоступен")
            return None
        state = parse_initial_state(page.content())
    except (PlaywrightError, ValueError, AttributeError) as exc:
        logger.warning("[RESUME-ID] список резюме не прочитан (%s) — маппинг id недоступен", exc)
        return None
    # parse_initial_state возвращает любой валидный JSON, не только объект:
    # null/массив/строка (schema-drift, интерстишл) — .get() на них бросил бы
    # AttributeError уже вне try и аварийно оборвал бы apply. Нормализуем как
    # «маппинг недоступен» (fail-closed), не давая исключению прервать цикл.
    if not isinstance(state, dict):
        logger.warning(
            "[RESUME-ID] SSR-состояние не объект (%s) — маппинг id недоступен",
            type(state).__name__,
        )
        return None
    resumes = state.get("applicantResumes")
    if not isinstance(resumes, list):
        logger.warning("[RESUME-ID] секция applicantResumes не найдена — маппинг недоступен")
        return None
    mapping: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for item in resumes:
        attrs = item.get("_attributes") if isinstance(item, dict) else None
        if not isinstance(attrs, dict):
            continue
        resume_hash, numeric_id = attrs.get("hash"), attrs.get("id")
        if not resume_hash or numeric_id is None:
            continue
        mapping[str(resume_hash)] = str(numeric_id)
        status = attrs.get("status")
        if status is not None:
            statuses[str(resume_hash)] = str(status)
        if status == "not_finished":
            logger.warning(
                "[RESUME-ID] резюме %s (id=%s) в статусе not_finished — форма отклика "
                "его не предлагает: отклики уходят с другого резюме аккаунта",
                resume_hash,
                numeric_id,
            )
    if not mapping:
        logger.warning("[RESUME-ID] в SSR нет пар hash→id — маппинг недоступен")
        return None
    logger.info("[RESUME-ID] маппинг hash→id получен: %d резюме", len(mapping))
    return ResumeIdMapping(mapping, statuses=statuses)


def _goto_resumes_list(page: Page, *, post_write: bool = False) -> None:
    """Navigate without a readiness retry, then fail fast on the login form.

    ``goto_hh(..., ready_selector=...)`` retries the whole navigation three
    times, so a revoked session would wait minutes before its already-rendered
    login form was inspected. The card readiness wait is deliberately split
    out and called by the fallback immediately before ``_card_hashes``.

    ``post_write`` (Codex adversarial review, PR #158): this helper is called
    twice — once before any click (safe to report as an ordinary pre-write
    auth failure), and once from the fallback diff path AFTER
    ``duplicate.click()`` has already fired the clone POST. A session revoked
    server-side in that window must not be reported with pre-write wording —
    the operator needs to know the clone may already exist before retrying
    (mirrors the ``except Exception`` handler's caveat in
    ``commands/copy_resume.py``, and the same "state not confirmed" pattern as
    ``ResumeListIndeterminate`` above).
    """
    goto_hh(page, RESUMES_LIST_URL)
    if has_login_form(page):
        if post_write:
            raise NotAuthenticated(
                "страница содержит форму входа после отправки запроса на "
                "дублирование — состояние копии НЕ подтверждено, возможно "
                "она уже создана (запустите `login`, затем проверьте список "
                "резюме перед повтором)"
            )
        raise NotAuthenticated(
            "страница содержит форму входа — сессия отвергнута сервером "
            "(запустите `login`, затем повторите)"
        )


def _wait_resume_list_ready(page: Page) -> None:
    """Preserve #142's rendered-list guarantee for the fallback card diff."""
    try:
        page.locator(RESUME_LIST_CARD).first.wait_for(timeout=COPY_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        raise ResumeListIndeterminate(
            "карточки резюме не появились за отведённое время — состояние "
            f"{RESUMES_LIST_URL} не подтверждено"
        ) from None


def _reconcile_created_resume(
    page: Page,
    before: set[str],
    url_candidate: str,
) -> tuple[str, str]:
    """Confirm the clone in the stable list after the WRITE click (#311).

    A profile URL is only a candidate: the clone request may have failed, or
    navigation may have landed on an existing profile.  The list diff is the
    authoritative post-action proof and must contain exactly one new id.
    """
    _goto_resumes_list(page, post_write=True)
    _wait_resume_list_ready(page)

    deadline = _monotonic() + COPY_TIMEOUT_MS / 1000
    created: set[str] = set()
    while True:
        created = _card_hashes(page) - before
        if len(created) == 1:
            break
        if _monotonic() >= deadline:
            break
        page.wait_for_timeout(PROFILE_POLL_MS)

    if len(created) != 1:
        return "", (
            f"hh.ru не подтвердил создание копии (новых резюме в списке: "
            f"{len(created)}) — состояние после WRITE-клика не подтверждено "
            "(fail-closed)"
        )

    created_id = created.pop()
    # Identity-bound success (#311): a bare account-wide list diff is not proof
    # the clone was this click's product.  Without a URL candidate matching the
    # list, the new card could be a concurrent creation (or a pre-existing
    # unrelated resume) — recording it as the clone would write a wrong id into
    # config.  Fail closed and let the user reconcile manually.
    if not url_candidate:
        return "", (
            f"URL после дублирования не подтвердил новый resume_id, а список "
            f"показал {created_id} — связать копию с этим кликом однозначно "
            "нельзя (fail-closed; сверьте список резюме вручную)"
        )
    if url_candidate != created_id:
        return "", (
            f"URL после дублирования указывает на {url_candidate}, а список "
            f"подтвердил {created_id} — состояние копии не подтверждено "
            "(fail-closed)"
        )
    return created_id, ""


def _wait_single_match_count(locator, *, timeout_ms: int) -> int:
    """Идиома ``count → wait_for → count`` для строгого (strict-mode) Playwright.

    Возвращает итоговое число совпадений, либо ``-1`` (элемент не появился за
    ``timeout_ms`` — PlaywrightTimeoutError). Контракт callsite: ``1`` — ок,
    ``0``-после-wait или ``-1`` — не найдено, ``>1`` — неоднозначно (fail-closed).

    Зачем ``count()`` ДО ``wait_for``: Playwright-локаторы строгие — ``wait_for()``
    на локаторе с >1 совпадением кидает ``playwright.sync_api.Error`` ("strict mode
    violation"), НЕ ``TimeoutError``. Проверка ``count() != 1`` первой ловит
    неоднозначность предсказуемо (fail-closed), а не улетает необработанным
    исключением мимо ``cli.main`` (там ловится только ``KeyboardInterrupt``).

    #142: идиома повторялась дважды (карточка резюме + кнопка «Дублировать»),
    вынесена сюда, чтобы правки strict-mode-логики были в одном месте.
    """
    match_count = locator.count()
    if match_count == 0:
        try:
            locator.wait_for(timeout=timeout_ms)
            match_count = locator.count()
        except PlaywrightTimeoutError:
            return -1
    return match_count


def copy_resume_on_hh(page: Page, resume: ResumeConfig, dry_run: bool) -> CopyResumeResult:
    observer = None
    absolute_deadline = _monotonic() + PROFILE_ABSOLUTE_TIMEOUT_SECONDS
    if not dry_run:
        # Подписки обязаны существовать ДО первой навигации: ошибки federation/
        # hydration часто возникают при старте страницы и позже уже не повторяются.
        observer = ResumeProfileReadinessObserver(page)
        observer.attach()
        observer.reset_attempt()

    logger.info("Открываю список резюме: %s", RESUMES_LIST_URL)
    _goto_resumes_list(page)

    link_sel = RESUME_LIST_CARD_LINK_TPL.format(resume_id=resume.resume_id)
    duplicate = None
    for attempt in range(2):
        card_locator = page.locator(f"{RESUME_LIST_CARD}:has({link_sel})")
        match_count = _wait_single_match_count(card_locator, timeout_ms=COPY_TIMEOUT_MS)
        if match_count == -1:
            return CopyResumeResult(
                resume.id,
                False,
                reason=f"резюме {resume.resume_id} не найдено в списке резюме",
            )
        if match_count != 1:
            return CopyResumeResult(
                resume.id,
                False,
                reason=f"карточка резюме {resume.resume_id} определяется неоднозначно "
                f"({match_count} совпадений) — останавливаюсь (fail-closed)",
            )
        card = card_locator.first

        if dry_run:
            logger.info(
                "[DRY-RUN] Скопировал бы резюме '%s' (кнопка меню не нажимается)", resume.id
            )
            return CopyResumeResult(resume.id, True, reason="dry-run")

        assert observer is not None
        duplicate, failure = _wait_duplicate_action(
            page,
            card,
            observer,
            absolute_deadline=absolute_deadline,
        )
        if duplicate is not None:
            break

        # Отсутствие действия при уже завершившемся client render — не stall и
        # не повод перезагружать страницу (например, достигнут лимит резюме).
        if failure.startswith("duplicate_action_missing:") or "неоднозначно" in failure:
            return CopyResumeResult(resume.id, False, reason=failure)

        if attempt == 1:
            return CopyResumeResult(resume.id, False, reason=failure)

        logger.warning("Профиль hh.ru не готов: %s — один recovery reload", failure)
        observer.reset_attempt()
        reload_failure = _reload_resumes_list(page)
        if reload_failure:
            return CopyResumeResult(resume.id, False, reason=reload_failure)

    assert duplicate is not None
    # Снимок для post-WRITE diff делаем только после полной pre-write готовности.
    before = _card_hashes(page)

    try:
        duplicate.click()
        logger.info("Клик по «Дублировать» — жду страницу нового резюме")

        new_id = ""
        try:
            page.wait_for_url(_RESUME_HASH_RE, timeout=COPY_TIMEOUT_MS)
            m = _RESUME_HASH_RE.search(page.url)
            if m:
                new_id = m.group(1)
        except PlaywrightTimeoutError:
            logger.warning("Навигация на новое резюме не дождалась — сверяю список резюме")

        # URL is only a candidate.  Always reconcile against the list because the
        # click may have produced a SPA navigation without creating a resume, and
        # the URL alone does not prove that the clone is visible in the account.
        url_candidate = "" if new_id == resume.resume_id else new_id
        new_id, reconciliation_failure = _reconcile_created_resume(page, before, url_candidate)
    except (NotAuthenticated, ResumeListIndeterminate, PlaywrightError) as exc:
        # WRITE-клик уже отправлен; исключение здесь не доказывает, что копия
        # не создана (таймаут, отзыв сессии, stale DOM) — fail-closed, uncertain.
        return CopyResumeResult(
            resume.id,
            False,
            uncertain=True,
            reason=f"состояние после WRITE-клика не подтверждено: {exc} (fail-closed)",
        )

    if reconciliation_failure:
        return CopyResumeResult(resume.id, False, uncertain=True, reason=reconciliation_failure)

    logger.info("Резюме '%s' скопировано, новый resume_id: %s", resume.id, new_id)
    return CopyResumeResult(resume.id, True, new_resume_id=new_id)


# CI re-trigger after main plugin-name fix
