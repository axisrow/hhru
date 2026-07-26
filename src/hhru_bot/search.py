from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode

from playwright.sync_api import Page

from . import selectors as sel
from .browser import HH_BASE_URL
from .config import ResumeConfig, SearchFilters
from .config_sections.scoring import ScoringConfig, ScoringWeights

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


# --- скоринг/ранжирование (issue #15) ---------------------------------------


def _tokenize(text: str) -> list[str]:
    """Простейшая токенизация в нижний регистр по не-буквенно-цифровым границам.

    Намеренно наивная: скоринг v1 работает по title/конфиг-ключевым словам,
    поэтому лемматизация/морфология не нужны — важна воспроизводимость и
    дешевизна (без внешних зависимостей).
    """
    tokens: list[str] = []
    current = ""
    for ch in text.lower():
        if ch.isalnum():
            current += ch
        elif current:
            tokens.append(current)
            current = ""
    if current:
        tokens.append(current)
    return tokens


def _DEFAULT_WEIGHTS() -> ScoringWeights:
    return ScoringWeights()


def _score_card(
    card: VacancyCard,
    filters: SearchFilters,
    weights: ScoringWeights,
) -> tuple[float, dict[str, float]]:
    """Считает score карточки и разбивку по факторам.

    Факторы v1 (по доступным полям title/конфиг):
      - must_have: +weight за каждое must_have-слово, найденное в title (по токену).
      - nice_to_have: +weight за каждое nice_to_have-слово в title.
      - exclude_keyword: +weight (обычно отрицательный) за каждое стоп-слово в title.
      - text_match: +weight × долю токенов filters.text, найденных в title.
    """
    title_tokens = set(_tokenize(card.title))

    must_have_hits = sum(1 for kw in filters.must_have if _tokenize(kw) and _tokenize(kw)[0] in title_tokens)
    nice_hits = sum(1 for kw in filters.nice_to_have if _tokenize(kw) and _tokenize(kw)[0] in title_tokens)
    exclude_hits = sum(1 for kw in filters.exclude_keywords if _tokenize(kw) and _tokenize(kw)[0] in title_tokens)

    text_tokens = _tokenize(filters.text)
    if text_tokens:
        matched = sum(1 for t in text_tokens if t in title_tokens)
        text_ratio = matched / len(text_tokens)
    else:
        text_ratio = 0.0

    breakdown: dict[str, float] = {
        "must_have": weights.must_have * must_have_hits,
        "nice_to_have": weights.nice_to_have * nice_hits,
        "exclude_keyword": weights.exclude_keyword * exclude_hits,
        "text_match": weights.text_match * text_ratio,
    }
    return sum(breakdown.values()), breakdown


def rank_candidates(
    candidates: list[VacancyCard],
    filters: SearchFilters,
    resume: ResumeConfig,
) -> list[tuple[VacancyCard, float, dict[str, float]]]:
    """Ранжирует кандидатов по убыванию score (issue #15).

    Чистая функция без браузера — ради тестируемости (как filter_candidates).
    Возвращает список (карточка, score, разбивка факторов), отсортированный
    по убыванию score; при равенстве — стабильно по vacancy_id (детерминизм).

    Обратная совместимость: без scoring-конфига и без must_have/nice_to_have
    все score = 0.0, а порядок сохраняется (стабильная сортировка по vacancy_id).
    """
    scoring: ScoringConfig | None = getattr(resume, "scoring", None)
    weights = scoring.weights if scoring is not None else _DEFAULT_WEIGHTS()

    scored: list[tuple[VacancyCard, float, dict[str, float]]] = []
    for card in candidates:
        score, breakdown = _score_card(card, filters, weights)
        scored.append((card, score, breakdown))

    # Стабильно: равные score упорядочиваются по vacancy_id (лексикографически).
    scored.sort(key=lambda item: (-item[1], item[0].vacancy_id))
    return scored
