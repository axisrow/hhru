"""Страница вакансии (/vacancy/{id}) — подтверждено curl-дампом."""

from __future__ import annotations

from ._generated import selector as _selector

VACANCY_APPLY_BUTTON = _selector("vacancy_page.VACANCY_APPLY_BUTTON")
# Status controls shown instead of the apply button after an existing response.
# The "again" control opens a modal with a separate repeat-submit action.
VACANCY_ALREADY_RESPONDED_AGAIN = _selector("vacancy_page.VACANCY_ALREADY_RESPONDED_AGAIN")
VACANCY_ALREADY_RESPONDED_CHAT = _selector("vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT")
# Rendered in the response modal when the selected resume is not visible to
# client companies.  This can appear while the URL remains the vacancy URL.
VACANCY_HIDDEN_RESUME_WARNING = _selector("vacancy_page.VACANCY_HIDDEN_RESUME_WARNING")
VACANCY_RELOCATION_CONFIRM = _selector("vacancy_page.VACANCY_RELOCATION_CONFIRM")
VACANCY_SIMILAR_VACANCIES_CLOSE = _selector("vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE")
VACANCY_DIRECT_APPLICATION_CANCEL = _selector("vacancy_page.VACANCY_DIRECT_APPLICATION_CANCEL")
VACANCY_DIRECT_APPLICATION_ALERT = _selector("vacancy_page.VACANCY_DIRECT_APPLICATION_ALERT")
VACANCY_LIMIT_ERROR = _selector("vacancy_page.VACANCY_LIMIT_ERROR")
VACANCY_RESPONSE_REJECT_WARNING = _selector("vacancy_page.VACANCY_RESPONSE_REJECT_WARNING")
VACANCY_RESPONSE_ERROR = _selector("vacancy_page.VACANCY_RESPONSE_ERROR")
VACANCY_TITLE = _selector("vacancy_page.VACANCY_TITLE")
VACANCY_COMPANY_NAME = _selector("vacancy_page.VACANCY_COMPANY_NAME")

# VACANCY_APPLY_BUTTON — это ссылка
# (href="/applicant/vacancy_response?vacancyId=..&employerId=..&hhtmFrom=vacancy"),
# а НЕ кнопка, открывающая модалку на этой же странице. Переход по ней ведёт
# на отдельную страницу/попап с формой отклика — apply должен это учитывать
# (перейти по href, а не искать форму сразу после клика на той же странице).
