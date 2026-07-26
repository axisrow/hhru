"""Шаг: проверка «уже откликались» по маркеру на странице.

Владелец: #3. Селектор маркера живёт здесь, а не в selector_groups/apply_form —
так #3 изолирован от #7 (success-маркер в apply/success.py) и от shared-формы.

Важно: основная дедупликация в проекте — через локальную SQLite-историю
(history.has_applied), а не через этот DOM-маркер (анонимному запросу hh.ru
его не показывает). Этот шаг — лишь страховка на случай, если маркер всё же
появится у залогиненного пользователя.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

from ..search import VacancyCard

logger = logging.getLogger("hhru_bot.apply.dedup")

# Маркер «уже откликались» на странице вакансии/формы — НЕ подтверждено (требует логина).
APPLY_ALREADY_RESPONDED_MARKER = (
    "[data-qa='vacancy-serp__vacancy_response_status']"  # НЕ подтверждено
)


def check_already_responded(page: Page, vacancy: VacancyCard) -> str | None:
    """Возвращает причину отказа, если на странице виден маркер «уже откликались»."""
    if page.locator(APPLY_ALREADY_RESPONDED_MARKER).count() > 0:
        logger.info("Вакансия '%s': маркер 'уже откликались' найден на странице", vacancy.title)
        return "уже есть отклик (обнаружено на странице)"
    return None
