"""Шаг: проверка «уже откликались».

Владелец: #3. На странице вакансии hh.ru показывает отдельные маркеры
`vacancy-response-link-top-again` и `vacancy-response-link-view-topic`, когда
отклик уже существует. Первый открывает модальное окно с отдельной кнопкой
повторной отправки и поэтому не является заменой кнопке отклика.

Локальная история по-прежнему является основной дедупликацией до открытия
страницы. DOM-проверка нужна для диагностического/прямого запуска probe и для
случая, когда локальная история не знает об отклике.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

from ..search import VacancyCard

logger = logging.getLogger("hhru_bot.apply.dedup")


def check_already_responded(page: Page, vacancy: VacancyCard) -> str | None:
    """Возвращает причину отказа, если вакансия уже откликнута.

    Дедупликация идёт через history.has_applied() в filter_candidates() (см.
    search.py) ещё до попадания в apply_to_vacancy. Эта проверка дополнительно
    распознаёт подтверждённые live-DOM маркеры, чтобы отсутствие обычной кнопки
    не выглядело ошибкой селектора. Ошибки Playwright намеренно не скрываются:
    это fail-closed граница для неизвестного состояния страницы.
    """
    from ..selector_groups import vacancy_page

    if (
        page.locator(vacancy_page.VACANCY_ALREADY_RESPONDED_AGAIN).count() > 0
        or page.locator(vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT).count() > 0
    ):
        reason = f"уже откликались по вакансии {vacancy.vacancy_id}, пропуск"
        logger.info("%s — %s", vacancy.title, reason)
        return reason
    logger.debug("Вакансия '%s': маркеры уже отклика не найдены", vacancy.title)
    return None
