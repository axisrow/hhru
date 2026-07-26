"""Страница поиска вакансий (/search/vacancy) — подтверждено curl-дампом."""

from __future__ import annotations

VACANCY_CARD = "[data-qa='vacancy-serp__vacancy']"
VACANCY_CARD_TITLE_LINK = "[data-qa='serp-item__title']"
VACANCY_CARD_COMPANY = "[data-qa='vacancy-serp__vacancy-employer']"
# Зарплата и дата публикации в карточке списка (issue #14).
# Селекторы подтверждены curl-дампом страницы поиска вместе с остальной
# разметкой serp; парсер зарплаты устойчив к «з/п не указана» и отсутствию
# блока (контейнер не рендерится hh.ru, если з/п не задана).
VACANCY_CARD_COMPENSATION = "[data-qa='vacancy-serp__vacancy-compensation']"
VACANCY_CARD_DATE = "[data-qa='vacancy-serp__vacancy-date']"
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
