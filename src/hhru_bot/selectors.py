"""
Централизованное место для всех CSS/data-qa селекторов hh.ru.

Статус проверки: браузерное окружение агента блокируется DDoS-Guard, но
разметку удалось получить напрямую через curl (анонимно, без логина) для
страницы поиска (/search/vacancy) и страницы вакансии (/vacancy/{id}).
Селекторы ниже с пометкой "подтверждено" взяты из реального HTML этих
страниц. Всё, что рендерится только авторизованному пользователю через JS
(форма отклика после перехода на /applicant/vacancy_response, страница
резюме с кнопкой поднятия) — НЕ подтверждено, т.к. для этого нужна live
сессия с логином. Перед первым реальным запуском `apply`/`bump` нужно
пройти login, затем открыть эти страницы в обычном браузере и свериться —
остальной код селекторы не дублирует.
"""

from __future__ import annotations

# --- Страница поиска вакансий (/search/vacancy) — подтверждено curl-дампом ---
VACANCY_CARD = "[data-qa='vacancy-serp__vacancy']"
VACANCY_CARD_TITLE_LINK = "[data-qa='serp-item__title']"
VACANCY_CARD_COMPANY = "[data-qa='vacancy-serp__vacancy-employer']"
# Кнопка отклика прямо в карточке списка (ведёт на /applicant/vacancy_response?vacancyId=...&employerId=...)
VACANCY_CARD_RESPONSE_BUTTON = "[data-qa='vacancy-serp__vacancy_response']"
PAGINATION_NEXT = "[data-qa='pager-next']"

# Анонимному curl-запросу hh.ru не показывает маркер "уже откликались" в
# разметке — этот статус виден только залогиненному пользователю. Дедупликация
# в этом проекте не полагается на разметку hh.ru, а делается через локальную
# историю (history.py), поэтому отсутствие проверенного селектора не критично.
VACANCY_CARD_RESPONSE_STATUS = "[data-qa='vacancy-serp__vacancy_response_status']"  # НЕ подтверждено

# --- Страница вакансии (/vacancy/{id}) — подтверждено curl-дампом ---
VACANCY_APPLY_BUTTON = "[data-qa='vacancy-response-link-top']"
VACANCY_TITLE = "[data-qa='vacancy-title']"
VACANCY_COMPANY_NAME = "[data-qa='vacancy-company-name']"

# VACANCY_APPLY_BUTTON — это ссылка (href="/applicant/vacancy_response?vacancyId=..&employerId=..&hhtmFrom=vacancy"),
# а НЕ кнопка, открывающая модалку на этой же странице. Переход по ней ведёт
# на отдельную страницу/попап с формой отклика — apply.py должен это учитывать
# (перейти по href, а не искать форму сразу после клика на той же странице).

# --- Форма отклика на /applicant/vacancy_response — НЕ подтверждено (требует логина) ---
APPLY_RESUME_SELECT = "[data-qa='resume-topic-title']"
APPLY_COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
APPLY_COVER_LETTER_TEXTAREA = "textarea[data-qa='vacancy-response-popup-form-letter-input']"
APPLY_SUBMIT_BUTTON = "[data-qa='vacancy-response-submit-popup']"
APPLY_SUCCESS_MARKER = "[data-qa='vacancy-response-sent-message']"
APPLY_ALREADY_RESPONDED_MARKER = "[data-qa='vacancy-serp__vacancy_response_status']"  # НЕ подтверждено

# --- Страница резюме (/resume/{id}) — НЕ подтверждено (требует логина) ---
RESUME_BUMP_BUTTON = "[data-qa='resume-update-button']"
RESUME_BUMP_DISABLED_HINT = "[data-qa='resume-update-button-disabled']"

# --- Логин ---
LOGIN_URL_MARKER = "account/login"
