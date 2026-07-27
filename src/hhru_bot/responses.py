"""Мониторинг ответов работодателей (#12, Этап 2).

Владелец: #12. Не трогает apply/ и search.py — отдельный поток данных:
/applicant/negotiations → fetch_responses → history.upsert_response.

Поток: команда responses открывает страницу откликов/переписки, fetch_responses
собирает карточки переписок в ResponseItem (vacancy_id/работодатель/статус/
дата/ссылка на чат), команда upsert'ит их в историю и печатает ASCII-сводку
«что нового» (new_responses_since прошлой отметки).

Read-only по отношению к hh.ru: страница откликов только читается, никаких
кликов «ответить»/навигации в чат. Анти-фрод принцип CLAUDE.md сохранён: между
страницами списка — случайная пауза throttle.wait (как и в apply/search).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from playwright.sync_api import Page

from .browser import HH_BASE_URL
from .selector_groups import negotiations as ns

logger = logging.getLogger("hhru_bot.responses")

NEGOTIATIONS_URL = f"{HH_BASE_URL}/applicant/negotiations"


# --- статусы ответов работодателя -------------------------------------------
# Нормализуем текст бейджа hh.ru в стабильный маркер. Это источник правды для
# storage (history.responses.status) и вывода команды. Чистая функция — ради
# тестируемости без браузера.
#
# Подмножество переходов (соответствует состояниям переписки hh.ru):
#   invitation — «Приглашение на собеседование» (работодатель позвал).
#   response   — «Ответ работодателя» / новое сообщение без приглашения.
#   discard    — «Отказ» (vacancy закрыта / отказали).
#   read       — «Прочитано» / нет активного действия (отклик просмотрен).
#   unknown    — незнакомый бейдж; храним как есть (не падаем) — пользователь
#                увидит сырой текст в выводе, БД хранит читаемую строку-ключ.


class ResponseStatus:
    """Стабильные строковые ключи статуса ответа работодателя."""

    INVITATION = "invitation"
    RESPONSE = "response"
    DISCARD = "discard"
    READ = "read"
    UNKNOWN = "unknown"


# Карта: подстрока текста бейджа (нижний регистр) → ключ статуса. Порядок важен:
# более специфичные («приглашени») раньше общих («прочитан»). Синонимы covers
# реальные формулировки hh.ru в шапке карточки переписки.
_STATUS_MAP: list[tuple[str, str]] = [
    ("приглашени", ResponseStatus.INVITATION),  # Приглашение / Приглашен(а)
    ("собеседован", ResponseStatus.INVITATION),
    ("отказ", ResponseStatus.DISCARD),  # Отказ / Отклонено
    ("отклонен", ResponseStatus.DISCARD),
    ("закрыт", ResponseStatus.DISCARD),  # «вакансия закрыта»
    ("новое сообщен", ResponseStatus.RESPONSE),  # новое сообщение от работодателя
    ("ответил", ResponseStatus.RESPONSE),
    ("ответ от", ResponseStatus.RESPONSE),
    ("непрочитан", ResponseStatus.RESPONSE),  # есть непрочитанное — значит ответили
    ("прочитан", ResponseStatus.READ),  # Прочитано / прочитан(а)
    ("просмотрен", ResponseStatus.READ),
]


def normalize_status(text: str | None) -> str:
    """Текст бейджа hh.ru → стабильный ключ статуса (ResponseStatus.*) или ``unknown``.

    Чистая функция. None/пусто → ``read`` (свежий отклик без явного бейджа
    трактуем как «прочитан / ждёт ответа» — нейтральное состояние, не «новый
    ответ работодателя»). Незнакомый текст → ``unknown`` с сохранением исходной
    строки в логах (через caller), сам ключ короткий для storage.
    """
    if not text:
        return ResponseStatus.READ
    lower = text.strip().lower()
    if not lower:
        return ResponseStatus.READ
    for needle, key in _STATUS_MAP:
        if needle in lower:
            return key
    return ResponseStatus.UNKNOWN


@dataclass
class ResponseItem:
    """Один ответ работодателя из /applicant/negotiations.

    status — стабильный ключ (ResponseStatus.*), не сырой текст hh.ru;
    employer/chat_url могут быть None/пустыми (hh.ru прячет компанию для части
    вакансий, чата нет при отказе). raw_status — оригинальный текст бейджа
    для вывода/диагностики.
    """

    vacancy_id: str
    status: str
    employer: str = ""
    chat_url: str | None = None
    raw_status: str = ""


def _extract_vacancy_id(href: str) -> str | None:
    """Достаёт числовой vacancy_id из href ссылки вакансии/чата.

    Ссылки hh.ru на этой странице: ``/vacancy/12345?...``,
    ``/applicant/vacancy/12345?...``, чат ``/applicant/negotiations?...&vacancyId=12345``.
    Числовой tail пути — приоритет; иначе query-параметр vacancyId.
    """
    if not href:
        return None
    path, _, query = href.partition("?")
    tail = path.rstrip("/").split("/")[-1]
    if tail.isdigit():
        return tail
    # Fallback: vacancyId в query (чат-ссылки).
    m = re.search(r"(?:^|&)vacancyId=(\d+)", query)
    if m:
        return m.group(1)
    return None


def _absolute_url(href: str) -> str:
    """Делает href абсолютным (как в search.py): http... иначе prepend HH_BASE_URL."""
    if href.startswith("http"):
        return href.split("?")[0]
    return f"{HH_BASE_URL}{href.split('?')[0]}"


def _optional_text(item, selector: str) -> str:
    """Текст первого элемента карточки по selector, либо пустая строка.

    Опциональные поля (работодатель, статус). Как search._optional_text, но без
    None → пустая строка (для responses пустота нормальна и удобнее в dataclass).
    """
    loc = item.locator(selector).first
    if not loc.count():
        return ""
    text = loc.inner_text().strip()
    return text or ""


def parse_response_card(item) -> ResponseItem | None:
    """Парсит один locator карточки переписки в ResponseItem, либо None.

    None — если из ссылки не удалось достать vacancy_id (битая/пустая карточка).
    Чистая относительно Playwright-locator'а: импортирует только типы селекторов.
    """
    link = item.locator(ns.NEGOTIATION_VACANCY_LINK).first
    vacancy_href = link.get_attribute("href") or "" if link.count() else ""
    vacancy_id = _extract_vacancy_id(vacancy_href)
    if not vacancy_id:
        return None

    raw_status = _optional_text(item, ns.NEGOTIATION_STATUS)
    employer = _optional_text(item, ns.NEGOTIATION_EMPLOYER)

    chat_link = item.locator(ns.NEGOTIATION_CHAT_LINK).first
    chat_href = chat_link.get_attribute("href") or "" if chat_link.count() else ""
    # chat_url — на страницу чата; если отдельной ссылки нет, fallback на карточку
    # вакансии (минимум даёт точку входа).
    chat_url = _absolute_url(chat_href) if chat_href else _absolute_url(vacancy_href)

    return ResponseItem(
        vacancy_id=vacancy_id,
        status=normalize_status(raw_status),
        employer=employer,
        chat_url=chat_url,
        raw_status=raw_status,
    )


def fetch_responses(page: Page, max_pages: int = 5) -> list[ResponseItem]:
    """Собирает ответы работодателей с /applicant/negotiations.

    Возвращает список ResponseItem (без дедупликации — upsert в истории её сделает
    по UNIQUE (resume_id, vacancy_id); resume_id добавляет вызывающий, тут только
    карточки). Пагинация: до ``max_pages``, стоп на первой пустой/без «далее».
    Read-only по hh.ru: только goto + чтение, никаких кликов действий.
    """
    results: list[ResponseItem] = []

    for page_num in range(max_pages):
        url = NEGOTIATIONS_URL if page_num == 0 else f"{NEGOTIATIONS_URL}?page={page_num}"
        logger.info("Загрузка страницы откликов: %s", url)
        page.goto(url, wait_until="domcontentloaded")

        cards = page.locator(ns.NEGOTIATION_ITEM)
        count = cards.count()
        if count == 0:
            logger.info("Страница %d: ответов не найдено, останавливаюсь", page_num)
            break

        for i in range(count):
            item = parse_response_card(cards.nth(i))
            if item is None:
                logger.debug(
                    "Страница %d, карточка %d: vacancy_id не извлечён, пропуск", page_num, i
                )
                continue
            results.append(item)

        if page.locator(ns.NEGOTIATIONS_PAGINATION_NEXT).count() == 0:
            logger.info("Достигнута последняя страница откликов (%d)", page_num)
            break

    logger.info("Собрано ответов работодателей всего: %d", len(results))
    return results
