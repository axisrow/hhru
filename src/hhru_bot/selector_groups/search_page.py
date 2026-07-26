"""Страница поиска вакансий (/search/vacancy) — подтверждено curl-дампом."""

from __future__ import annotations

VACANCY_CARD = "[data-qa='vacancy-serp__vacancy']"
VACANCY_CARD_TITLE_LINK = "[data-qa='serp-item__title']"
VACANCY_CARD_COMPANY = "[data-qa='vacancy-serp__vacancy-employer']"
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
