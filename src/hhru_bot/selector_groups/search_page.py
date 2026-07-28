"""Страница поиска вакансий (/search/vacancy) — подтверждено curl-дампом."""

from __future__ import annotations

VACANCY_CARD = "[data-qa='vacancy-serp__vacancy']"
VACANCY_CARD_TITLE_LINK = "[data-qa='serp-item__title']"
VACANCY_CARD_COMPANY = "[data-qa='vacancy-serp__vacancy-employer']"
# Зарплата и дата публикации в карточке списка (issue #14).
# VACANCY_CARD_COMPENSATION: НЕ работает на живом hh.ru с 2025 — hh.ru
# перешёл на magritte-разметку, блок ЗП рендерится без этого data-qa (#73).
# search.py использует regex-fallback по innerHTML карточки когда селектор
# пуст. Селектор оставлен для обратной совместимости (старые кэши/curl-дампы).
VACANCY_CARD_COMPENSATION = "[data-qa='vacancy-serp__vacancy-compensation']"
VACANCY_CARD_DATE = "[data-qa='vacancy-serp__vacancy-date']"
# Инфо о работодателе из карточки поиска (issue #74, Этап 1). Подтверждено в
# DOM-дампе (read-only, 2026-07-28): rating/trusted рендерятся в карточке
# списка для работодателей с отзывами. Блоки опциональны.
COMPANY_RATING_VALUE = "[data-qa='company-review-rating-value']"
COMPANY_RATING_REVIEWS_COUNT = "[data-qa='company-review-rating-reviews-count']"
TRUSTED_EMPLOYER_LINK = "[data-qa='trusted-employer-link']"
# Кнопка отклика прямо в карточке списка (ведёт на
# /applicant/vacancy_response?vacancyId=...&employerId=...)
VACANCY_CARD_RESPONSE_BUTTON = "[data-qa='vacancy-serp__vacancy_response']"
PAGINATION_NEXT = "[data-qa='pager-next']"

# Анонимному curl-запросу hh.ru не показывает маркер "уже откликались" в
# разметке — этот статус виден только залогиненному пользователю. Дедупликация
# в этом проекте не полагается на разметку hh.ru, а делается через локальную
# историю (history.py), поэтому отсутствие проверенного селектора не критично.
VACANCY_CARD_RESPONSE_STATUS = (
    "[data-qa='vacancy-serp__vacancy_response_status']"  # НЕ подтверждено
)
