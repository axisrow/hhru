"""Read-only inspection helpers for the authenticated negotiations page."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html import unescape

from playwright.sync_api import Error as PlaywrightError

logger = logging.getLogger("hhru_bot.negotiations_probe")

_STATE_RE = re.compile(
    r'<template[^>]*id=["\']HH-Lux-InitialState["\'][^>]*>(.*?)</template>', re.DOTALL
)


@dataclass(frozen=True)
class TopicRef:
    topic_id: str
    chat_id: str
    vacancy_id: str | None = None
    #: Резюме, с которого начался отклик (SSR ``topicList[].resumeId``, #200).
    #: Опционален НЕ как «атрибуция недостоверна», а как защита от дрейфа
    #: разметки: отсутствие поля не должно ронять маппинг topic→chat, на
    #: котором держатся responses/reply-employers.
    resume_id: str | None = None


@dataclass(frozen=True)
class RemindableTopicRef:
    """A negotiation for which hh.ru explicitly permits a reminder."""

    topic_id: str
    chat_id: str
    vacancy_id: str
    employer: str
    vacancy: str


def parse_initial_state(html: str) -> dict:
    """Read the SSR state without executing scripts or interacting with the page."""
    match = _STATE_RE.search(html)
    if not match:
        raise ValueError("SSR state template HH-Lux-InitialState not found")
    return json.loads(unescape(match.group(1)))


def topic_refs(html: str) -> list[TopicRef]:
    """Return the topic/chat mapping rendered in the negotiations SSR state.

    A topic entry missing ``id``/``chatId``/``vacancyId`` is dropped (fetch_responses
    can't attach it to a card without a vacancy_id to key on); dropped entries are
    logged so a silently shrinking mapping is diagnosable, matching the warning-log
    contract of the SSR-recovery except-path in responses.py.

    ``resumeId`` (#200) — резюме, с которого ушёл отклик. Проверено на живой
    сессии 2026-08-16: поле присутствует у ВСЕХ 7/7 переписок аккаунта, рядом с
    дублирующим его ``resources[type=RESUME]``. Это опровергает исходную посылку
    #55 §1.3 «/applicant/negotiations не даёт достоверной привязки чата к
    резюме»: посылка опиралась на осмотр видимого DOM карточки, где поля
    действительно нет, и на тест собственной схемы БД — но не на SSR-состояние,
    где оно есть. Отсутствие поля НЕ роняет запись (fail-open): маппинг
    topic→chat нужен responses/reply-employers и не должен зависеть от
    аналитического поля.
    """
    topics = parse_initial_state(html).get("applicantNegotiations", {}).get("topicList", [])
    refs: list[TopicRef] = []
    for topic in topics:
        if topic.get("id") is None or topic.get("chatId") is None or topic.get("vacancyId") is None:
            logger.debug("SSR topic entry missing id/chatId/vacancyId, dropped: %r", topic)
            continue
        resume_id = topic.get("resumeId")
        refs.append(
            TopicRef(
                str(topic["id"]),
                str(topic["chatId"]),
                str(topic["vacancyId"]),
                None if resume_id is None else str(resume_id),
            )
        )
    return refs


def remindable_topic_refs(html: str) -> list[RemindableTopicRef]:
    """Return only SSR topics with ``responseReminderState.allowed == True``.

    This parser is deliberately strict: a missing negotiations list, identity
    field, or reminder state is schema drift, not evidence that there are no
    reminders.  Callers must surface the error instead of silently returning a
    partial list.
    """
    state = parse_initial_state(html)
    negotiations = state.get("applicantNegotiations")
    if not isinstance(negotiations, dict) or not isinstance(negotiations.get("topicList"), list):
        raise ValueError("SSR negotiations topicList schema is not recognised")

    result: list[RemindableTopicRef] = []
    for topic in negotiations["topicList"]:
        if not isinstance(topic, dict):
            raise ValueError("SSR negotiations topic entry is not an object")
        reminder = topic.get("responseReminderState")
        if not isinstance(reminder, dict) or not isinstance(reminder.get("allowed"), bool):
            raise ValueError("SSR responseReminderState.allowed schema is not recognised")
        if not reminder["allowed"]:
            continue

        identity = (topic.get("id"), topic.get("chatId"), topic.get("vacancyId"))
        if any(value is None or str(value) == "" for value in identity):
            raise ValueError("remindable SSR topic is missing identity")
        employer_obj = topic.get("employer")
        vacancy_obj = topic.get("vacancy")
        employer = topic.get("employerName")
        vacancy = topic.get("vacancyName")
        if isinstance(employer_obj, dict):
            employer = employer or employer_obj.get("name")
        if isinstance(vacancy_obj, dict):
            vacancy = vacancy or vacancy_obj.get("name")
        # Older SSR payloads only expose vacancyId. Keep the row useful while
        # still requiring the identity fields above.
        result.append(
            RemindableTopicRef(
                str(identity[0]),
                str(identity[1]),
                str(identity[2]),
                "" if employer is None else str(employer),
                str(vacancy) if vacancy is not None else str(identity[2]),
            )
        )
    return result


def paginated_topic_refs(page, max_pages: int = 5) -> list[TopicRef]:
    """Read SSR topic mappings from every available negotiations page.

    ``topicList`` is paginated with the cards, so reading only page zero makes
    chats on later pages look unavailable. Navigation is GET-only and uses the
    same confirmed pager contract as ``fetch_responses``.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")

    # Lazy imports avoid a module cycle while responses.py recovers topics.
    from .browser import goto_hh, require_authenticated_page
    from .responses import NEGOTIATIONS_URL, _has_next_page

    refs: list[TopicRef] = []
    for page_num in range(max_pages):
        url = NEGOTIATIONS_URL if page_num == 0 else f"{NEGOTIATIONS_URL}?page={page_num}"
        goto_hh(page, url)
        # Codex-ревью (#201, по аналогии с fetch_responses в responses.py):
        # проверяем auth-маркер ДО чтения SSR-состояния. Без этой проверки
        # рендер login-страницы вместо negotiations (истёкшая сессия) даёт
        # topic_refs() ValueError («HH-Lux-InitialState not found») вместо
        # диагностируемого NotAuthenticated — тот же класс ошибки, что и
        # пустой inbox, но с другой причиной, которую вызывающий код не
        # должен путать с завершённой пагинацией.
        require_authenticated_page(page)
        refs.extend(topic_refs(page.content()))
        try:
            has_next = _has_next_page(page, page_num)
        except AttributeError:
            # Lightweight read-only fakes may expose content() but not the
            # pagination locators; they represent a single page.
            has_next = False
        if not has_next:
            break
    return refs


def paginated_remindable_topic_refs(page, max_pages: int = 5) -> list[RemindableTopicRef]:
    """Read the fail-closed reminder state from every negotiations page."""
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    from .browser import goto_hh, require_authenticated_page
    from .responses import (
        NEGOTIATIONS_URL,
        RENDER_TIMEOUT_MS,
        ResponsesIndeterminate,
        _has_next_page,
    )
    from .selector_groups import negotiations as ns

    refs: list[RemindableTopicRef] = []
    for page_num in range(max_pages):
        url = NEGOTIATIONS_URL if page_num == 0 else f"{NEGOTIATIONS_URL}?page={page_num}"
        goto_hh(page, url)
        require_authenticated_page(page)
        refs.extend(remindable_topic_refs(page.content()))
        # SSR arrives before the client-side pager.  Do not interpret a
        # temporarily absent pager as the final page, or later reminders are
        # silently omitted.
        if hasattr(page, "locator"):
            try:
                page.locator(ns.NEGOTIATION_ITEM).first.wait_for(
                    state="attached", timeout=RENDER_TIMEOUT_MS
                )
            except PlaywrightError:
                raise ResponsesIndeterminate(
                    f"страницы {page_num} не подтверждена: карточки переписки "
                    f"не появились за {RENDER_TIMEOUT_MS} мс"
                ) from None
        if not _has_next_page(page, page_num):
            break
    return refs


def chat_url(chat_id: str, chatik_origin: str = "https://chatik.hh.ru") -> str:
    """Build the route used by hh.ru's ``open_chat`` button."""
    return f"{chatik_origin.rstrip('/')}/chat/{chat_id}"
