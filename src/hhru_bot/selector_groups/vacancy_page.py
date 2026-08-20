"""Страница вакансии (/vacancy/{id}) — подтверждено curl-дампом."""

from __future__ import annotations

VACANCY_APPLY_BUTTON = "[data-qa='vacancy-response-link-top']"
# Status controls shown instead of the apply button after an existing response.
# The "again" control opens a modal with a separate repeat-submit action.
VACANCY_ALREADY_RESPONDED_AGAIN = "[data-qa='vacancy-response-link-top-again']"
VACANCY_ALREADY_RESPONDED_CHAT = "[data-qa='vacancy-response-link-view-topic']"
# Rendered in the response modal when the selected resume is not visible to
# client companies.  This can appear while the URL remains the vacancy URL.
VACANCY_HIDDEN_RESUME_WARNING = "[data-qa='hidden-resume-warning']"
VACANCY_RELOCATION_CONFIRM = '[data-qa="relocation-warning-confirm"]'
VACANCY_SIMILAR_VACANCIES_CLOSE = '[data-qa="vacancy-response-similar-vacancies-close"]'
VACANCY_DIRECT_APPLICATION_CANCEL = '[data-qa="vacancy-response-link-advertising-cancel"]'
VACANCY_DIRECT_APPLICATION_ALERT = '[data-qa="magritte-alert"]'
VACANCY_LIMIT_ERROR = '[data-qa-popup-error-code="negotiations-limit-exceeded"]'
VACANCY_RESPONSE_REJECT_WARNING = '[data-qa="response-reject-warning"]'
VACANCY_RESPONSE_ERROR = '[data-qa="vacancy-response-error-notification"]'
VACANCY_TITLE = "[data-qa='vacancy-title']"
VACANCY_COMPANY_NAME = "[data-qa='vacancy-company-name']"

# VACANCY_APPLY_BUTTON — это ссылка
# (href="/applicant/vacancy_response?vacancyId=..&employerId=..&hhtmFrom=vacancy"),
# а НЕ кнопка, открывающая модалку на этой же странице. Переход по ней ведёт
# на отдельную страницу/попап с формой отклика — apply должен это учитывать
# (перейти по href, а не искать форму сразу после клика на той же странице).
