"""Публикация черновика резюме через кнопку в живом DOM (#219).

HTTP-контракт намеренно не используется: единственное write-действие здесь —
клик по реально отрисованной кнопке hh.ru после всех проверок.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import (
    NotAuthenticated,
    open_confirmed_resume,
    require_authenticated_page,
    resume_identity_matches,
)
from .config import ResumeConfig
from .selector_groups.resume_page import (
    RESUME_PUBLISH_BUTTON,
    RESUME_PUBLISH_BUTTON_DATA_QA,
    RESUME_VISIBILITY_BUTTON,
)

logger = logging.getLogger("hhru_bot.publish_resume")
PUBLISH_TIMEOUT_MS = 30_000


@dataclass
class ResumePublishState:
    status: str | None = None
    is_searchable: bool | None = None
    can_publish_or_update: bool | None = None
    next_incomplete_screen_id: str | None = None


@dataclass
class PublishResumeResult:
    resume_id: str
    success: bool
    reason: str = ""
    status: str | None = None
    is_searchable: bool | None = None
    uncertain: bool = False
    next_incomplete_screen_id: str | None = None


def _is_published(state: ResumePublishState) -> bool:
    """Return the positive live signal for a published hh.ru resume.

    hh.ru currently uses several ``status`` values (including ``new``,
    ``approved`` and ``modified``) for searchable resumes.  The stable
    publication signal is therefore ``isSearchable=True``; ``finished`` is
    retained for compatibility with the original state contract.
    """
    return state.is_searchable is True or state.status == "finished"


def _walk_json(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def parse_resume_state(markup: str, resume_id: str) -> ResumePublishState:
    """Extract state from the structured record for ``resume_id``.

    The page can contain state for more than one resume in embedded bootstrap
    data. Never combine fields from separate records.
    """
    if not resume_id:
        raise ValueError("resume_id is required to parse resume state safely")

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", markup):
        try:
            candidate, _ = decoder.raw_decode(markup[match.start() :])
        except json.JSONDecodeError:
            continue
        for record in _walk_json(candidate):
            if not isinstance(record, dict):
                continue
            identifiers = {str(record.get(key, "")) for key in ("id", "hash", "resumeId")}
            if resume_id not in identifiers:
                continue
            # hh.ru keeps the wizard's ``scheme`` next to the resume
            # record, rather than inside it.  It is still page-scoped and
            # therefore safe to attach only after the target identity was
            # found in this JSON document.
            scheme = candidate.get("scheme") if isinstance(candidate, dict) else None
            return _state_from_mapping(record, scheme)
    return ResumePublishState()


def _state_from_mapping(record: dict, scheme: dict | None = None) -> ResumePublishState:
    """Read only fields belonging to one structured record."""
    next_incomplete = record.get("nextIncompleteScreenId")
    if next_incomplete is None and isinstance(scheme, dict):
        next_incomplete = scheme.get("nextIncompleteScreenId")
    return ResumePublishState(
        status=record.get("status"),
        is_searchable=record.get("isSearchable"),
        can_publish_or_update=record.get("canPublishOrUpdate"),
        next_incomplete_screen_id=next_incomplete,
    )


def _visibility_text(page: Page) -> str:
    """Read visibility control text only; never click the visibility control."""
    locator = page.locator(RESUME_VISIBILITY_BUTTON)
    if locator.count() == 1:
        return (locator.first.inner_text() or "").strip()
    return ""


def publish_resume_on_hh(
    page: Page,
    resume: ResumeConfig,
    dry_run: bool,
    *,
    before_click: Callable[[], None] | None = None,
) -> PublishResumeResult:
    """Inspect one config resume and optionally click its publish button."""
    try:
        open_confirmed_resume(page, resume.resume_id)
    except ValueError:
        return PublishResumeResult(resume.id, False, "identity резюме не подтверждён")

    state = parse_resume_state(page.content(), resume.resume_id)
    if state.status is None or state.can_publish_or_update is None:
        return PublishResumeResult(
            resume.id,
            False,
            "состояние резюме не подтверждено (status/canPublishOrUpdate)",
            state.status,
            state.is_searchable,
        )
    if _is_published(state):
        return PublishResumeResult(
            resume.id,
            False,
            "резюме уже опубликовано",
            state.status,
            state.is_searchable,
        )
    if state.status != "not_finished":
        reason = f"status={state.status}"
        return PublishResumeResult(resume.id, False, reason, state.status, state.is_searchable)
    # fail-closed (#225): незавершённый шаг блокирует клик независимо от
    # canPublishOrUpdate. hh.ru может отдать nextIncompleteScreenId вместе с
    # canPublishOrUpdate=True (или они разойдутся при SPA-гидратации), и тогда
    # обход guard ниже позволил бы клик по неполному резюме. Не угадываем кнопку.
    if state.next_incomplete_screen_id:
        return PublishResumeResult(
            resume.id,
            False,
            f"незавершённый шаг "
            f"nextIncompleteScreenId={state.next_incomplete_screen_id}; клик запрещён",
            state.status,
            state.is_searchable,
            False,
            state.next_incomplete_screen_id,
        )
    if state.can_publish_or_update is not True:
        return PublishResumeResult(
            resume.id,
            False,
            f"canPublishOrUpdate={state.can_publish_or_update}; клик запрещён",
            state.status,
            state.is_searchable,
        )

    publish = page.locator(RESUME_PUBLISH_BUTTON).or_(page.locator(RESUME_PUBLISH_BUTTON_DATA_QA))
    count = publish.count()
    if count == 0:
        try:
            publish.first.wait_for(timeout=PUBLISH_TIMEOUT_MS)
            count = publish.count()
        except PlaywrightTimeoutError:
            count = 0
    if count != 1:
        return PublishResumeResult(
            resume.id,
            False,
            (
                "кнопка «Опубликовать» не найдена"
                if count == 0
                else f"кнопка «Опубликовать» определяется неоднозначно ({count}) — клик запрещён"
            ),
            state.status,
            state.is_searchable,
        )
    visibility = _visibility_text(page)
    if dry_run:
        reason = "dry-run; кнопка не нажата"
        if visibility:
            reason += f"; видимость: {visibility}"
        return PublishResumeResult(resume.id, True, reason, state.status, state.is_searchable)

    try:
        if before_click is not None:
            before_click()
        publish.first.click(timeout=PUBLISH_TIMEOUT_MS)
    except PlaywrightError as exc:
        # #219 (по аналогии с #176/#207): клик мог уйти на hh.ru раньше, чем
        # Playwright поднял исключение (например, обрыв во время ожидания
        # реакции на клик) — обычный failed скрыл бы состоявшуюся публикацию
        # и позволил бы пользователю бездумно повторить --force. Fail-closed:
        # это uncertain, не failed.
        return PublishResumeResult(
            resume.id,
            False,
            f"ошибка клика; результат не подтверждён: {exc}",
            state.status,
            state.is_searchable,
            uncertain=True,
        )
    # Позитивный сигнал обязателен: после клика ждём server/client state, а не
    # выводим успех из исчезновения кнопки или отсутствия ошибки.
    deadline = time.monotonic() + PUBLISH_TIMEOUT_MS / 1000
    after = ResumePublishState()
    while time.monotonic() < deadline:
        try:
            after = parse_resume_state(page.content(), resume.resume_id)
        except PlaywrightError as exc:
            return PublishResumeResult(
                resume.id,
                False,
                f"результат публикации не подтверждён: {exc}",
                uncertain=True,
            )
        if _is_published(after):
            break
        try:
            page.wait_for_timeout(250)
        except PlaywrightError as exc:
            return PublishResumeResult(
                resume.id,
                False,
                f"результат публикации не подтверждён: {exc}",
                uncertain=True,
            )
    if not _is_published(after):
        # The SPA can keep the original SSR bootstrap snapshot after the write.
        # One read-only reload revalidates server state before we report failure.
        try:
            page.reload(wait_until="domcontentloaded")
            require_authenticated_page(page)
            if not resume_identity_matches(page, resume.resume_id):
                return PublishResumeResult(
                    resume.id,
                    False,
                    "identity резюме после публикации не подтверждён",
                    uncertain=True,
                )
            after = parse_resume_state(page.content(), resume.resume_id)
        except (PlaywrightError, NotAuthenticated) as exc:
            # После клика сессия могла быть отвергнута при reload (страница
            # вернула форму входа). Клик уже ушёл, поэтому это серая зона —
            # uncertain, а не утечка NotAuthenticated наружу (иначе команда
            # вышла бы [FAIL] без записи uncertain-аудита и позволила бы
            # бездумный повтор --force поверх уже состоявшейся публикации).
            return PublishResumeResult(
                resume.id,
                False,
                f"результат публикации не подтверждён: {exc}",
                uncertain=True,
            )
    if not _is_published(after):
        # Клик состоялся, сервер опрошен (включая reload), но подтверждения
        # нет — это тоже серая зона, не чистый failed: локальный таймаут не
        # доказывает отсутствие публикации на стороне hh.ru.
        return PublishResumeResult(
            resume.id,
            False,
            "публикация не подтверждена позитивным сигналом",
            after.status,
            after.is_searchable,
            uncertain=True,
        )
    return PublishResumeResult(resume.id, True, "опубликовано", after.status, after.is_searchable)
