"""Шаг: проверка «уже откликались».

Владелец: #3. Раньше здесь был DOM-маркер APPLY_ALREADY_RESPONDED_MARKER со
селектором `[data-qa='vacancy-serp__vacancy_response_status']` — но это селектор
страницы ПОИСКА (serp), а не страницы вакансии. На странице вакансии такого узла
нет, поэтому маркер никогда не срабатывал и при неблагоприятном раскладе мог дать
ложное «уже откликались». Селектор убран (мёртвый код).

Дедупликация «уже откликались» в проекте делается ТОЛЬКО через локальную
SQLite-историю — history.has_applied(), см. search.filter_candidates() — и она
отсекает повторные вакансии ДО apply_to_vacancy. К моменту, когда этот шаг
выполняется на странице вакансии, повторов уже нет.

Шаг check_already_responded оставлен как точка расширения pipeline (вдруг у
залогинённого пользователя hh.ru всё же покажет какой-то статус на странице
вакансии — тогда сюда вернутся с подтверждённым селектором). Пока он не отсекает
ничего.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

from ..search import VacancyCard

logger = logging.getLogger("hhru_bot.apply.dedup")


def check_already_responded(page: Page, vacancy: VacancyCard) -> str | None:  # noqa: ARG001
    """Возвращает причину отказа, если вакансия уже откликнута.

    Дедупликация идёт через history.has_applied() в filter_candidates() (см.
    search.py) ещё до попадания в apply_to_vacancy — поэтому здесь повторов уже
    нет и DOM-маркер не нужен. Шаг оставлен как точка расширения на случай
    подтверждённого селектора статуса на странице вакансии.
    """
    logger.debug(
        "Вакансия '%s': DOM-проверка 'уже откликались' пропущена — "
        "дедуп через history.has_applied()",
        vacancy.title,
    )
    return None
