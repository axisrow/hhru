"""
Централизованное место для всех CSS/data-qa селекторов hh.ru.

ВНИМАНИЕ: эти селекторы НЕ проверены вживую (окружение, в котором писался
этот код, не имело доступа к hh.ru — сайт блокировал браузер через
DDoS-Guard). Они основаны на исторически стабильных data-qa атрибутах
hh.ru, но structure сайта могла измениться. Перед первым реальным запуском
(`login`, затем `search --dry-run`) нужно открыть hh.ru в обычном браузере,
инструментами разработчика свериться с этими селекторами и поправить их
здесь при расхождении — остальной код их не дублирует.
"""

from __future__ import annotations

# --- Страница поиска вакансий (/search/vacancy) ---
VACANCY_CARD = "[data-qa='vacancy-serp__vacancy']"
VACANCY_CARD_TITLE_LINK = "[data-qa='serp-item__title']"
VACANCY_CARD_COMPANY = "[data-qa='vacancy-serp__vacancy-employer']"
VACANCY_CARD_RESPONSE_STATUS = "[data-qa='vacancy-serp__vacancy_response_status']"
PAGINATION_NEXT = "[data-qa='pager-next']"

# --- Страница вакансии (/vacancy/{id}) ---
VACANCY_APPLY_BUTTON = "[data-qa='vacancy-response-link-top']"
VACANCY_TITLE = "[data-qa='vacancy-title']"
VACANCY_COMPANY_NAME = "[data-qa='vacancy-company-name']"

# --- Модальное окно/страница отклика ---
APPLY_RESUME_SELECT = "[data-qa='resume-topic-title']"
APPLY_COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
APPLY_COVER_LETTER_TEXTAREA = "textarea[data-qa='vacancy-response-popup-form-letter-input']"
APPLY_SUBMIT_BUTTON = "[data-qa='vacancy-response-submit-popup']"
APPLY_SUCCESS_MARKER = "[data-qa='vacancy-response-sent-message']"
APPLY_ALREADY_RESPONDED_MARKER = "[data-qa='vacancy-serp__vacancy_response_status']"

# --- Страница резюме (/resume/{id}) ---
RESUME_BUMP_BUTTON = "[data-qa='resume-update-button']"
RESUME_BUMP_DISABLED_HINT = "[data-qa='resume-update-button-disabled']"

# --- Логин ---
LOGIN_URL_MARKER = "account/login"
