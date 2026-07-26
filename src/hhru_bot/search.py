from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode

from playwright.sync_api import Page

from . import selectors as sel
from .browser import HH_BASE_URL
from .config import SearchFilters

logger = logging.getLogger("hhru_bot.search")


@dataclass
class VacancyCard:
    vacancy_id: str
    title: str
    company: str
    url: str


def build_search_url(filters: SearchFilters, page_num: int = 0) -> str:
    params = {"text": filters.text, "page": page_num}
    if filters.area is not None:
        params["area"] = filters.area
    if filters.salary_from is not None:
        params["salary"] = filters.salary_from
    if filters.experience is not None:
        params["experience"] = filters.experience
    if filters.schedule is not None:
        params["schedule"] = filters.schedule
    return f"{HH_BASE_URL}/search/vacancy?{urlencode(params)}"


def _matches_exclusions(card: VacancyCard, filters: SearchFilters) -> str | None:
    """Возвращает причину исключения вакансии, либо None если она подходит."""
    company_lower = card.company.lower()
    for excluded in filters.exclude_employers:
        if excluded.lower() in company_lower:
            return f"компания в стоп-списке: {excluded}"

    title_lower = card.title.lower()
    for keyword in filters.exclude_keywords:
        if keyword.lower() in title_lower:
            return f"стоп-слово в названии: {keyword}"

    return None


def search_vacancies(
    page: Page,
    filters: SearchFilters,
    max_pages: int = 5,
) -> list[VacancyCard]:
    """
    Ищет вакансии по фильтрам, возвращает список карточек БЕЗ учёта
    exclude_employers/exclude_keywords и БЕЗ учёта истории откликов —
    эта фильтрация делается отдельно через filter_candidates(), чтобы
    её можно было протестировать и залогировать причины исключения.
    """
    results: list[VacancyCard] = []

    for page_num in range(max_pages):
        url = build_search_url(filters, page_num)
        logger.info("Загрузка страницы поиска: %s", url)
        page.goto(url, wait_until="domcontentloaded")

        cards = page.locator(sel.VACANCY_CARD)
        count = cards.count()
        if count == 0:
            logger.info("Страница %d: вакансий не найдено, останавливаюсь", page_num)
            break

        for i in range(count):
            card = cards.nth(i)
            title_link = card.locator(sel.VACANCY_CARD_TITLE_LINK).first
            title = title_link.inner_text().strip()
            href = title_link.get_attribute("href") or ""
            vacancy_id = _extract_vacancy_id(href)

            company_locator = card.locator(sel.VACANCY_CARD_COMPANY).first
            company = company_locator.inner_text().strip() if company_locator.count() else ""

            if not vacancy_id:
                logger.warning("Не удалось извлечь vacancy_id из href='%s', пропуск", href)
                continue

            results.append(
                VacancyCard(
                    vacancy_id=vacancy_id,
                    title=title,
                    company=company,
                    url=(
                        href.split("?")[0]
                        if href.startswith("http")
                        else f"{HH_BASE_URL}{href.split('?')[0]}"
                    ),
                )
            )

        next_button = page.locator(sel.PAGINATION_NEXT)
        if next_button.count() == 0:
            logger.info("Достигнута последняя страница поиска (%d)", page_num)
            break

    logger.info("Найдено вакансий всего: %d", len(results))
    return results


def _extract_vacancy_id(href: str) -> str | None:
    if not href:
        return None
    path = href.split("?")[0].rstrip("/")
    parts = path.split("/")
    return parts[-1] if parts and parts[-1].isdigit() else None


def filter_candidates(
    cards: list[VacancyCard],
    filters: SearchFilters,
    resume_id: str,
    history,
) -> tuple[list[VacancyCard], list[tuple[VacancyCard, str]]]:
    """
    Разделяет карточки на (подходящие, исключённые с причиной).
    Причины исключения: уже откликались ранее ИЛИ попадание в стоп-лист.
    """
    candidates: list[VacancyCard] = []
    skipped: list[tuple[VacancyCard, str]] = []

    for card in cards:
        if history.has_applied(resume_id, card.vacancy_id):
            skipped.append((card, "уже откликались ранее"))
            continue

        reason = _matches_exclusions(card, filters)
        if reason:
            skipped.append((card, reason))
            continue

        candidates.append(card)

    return candidates, skipped
