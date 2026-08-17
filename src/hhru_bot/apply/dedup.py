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

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..search import VacancyCard

logger = logging.getLogger("hhru_bot.apply.dedup")

# #226 cycle-review round 3 (codex): короткий таймаут для проверки видимости —
# к моменту вызова маркер уже attached (wait_apply_button его дождался общим
# wait_for), здесь только фильтруем скрытые/устаревшие SPA-копии.
_VISIBILITY_CHECK_TIMEOUT_MS = 1_500


def check_already_responded(
    page: Page, vacancy: VacancyCard, *, blocking: bool = True
) -> str | None:
    """Возвращает причину отказа, если вакансия уже откликнута.

    Дедупликация идёт через history.has_applied() в filter_candidates() (см.
    search.py) ещё до попадания в apply_to_vacancy. Эта проверка дополнительно
    распознаёт подтверждённые live-DOM маркеры, чтобы отсутствие обычной кнопки
    не выглядело ошибкой селектора. Ошибки Playwright намеренно не скрываются:
    это fail-closed граница для неизвестного состояния страницы.

    #226 cycle-review round 3: проверяем ВИДИМОСТЬ маркера (state="visible"),
    а не только присутствие в DOM (count() > 0). Persisted skip (ALREADY_APPLIED)
    навсегда исключает вакансию из будущих прогонов через is_skipped() — скрытая
    или устаревшая SPA-копия маркера, засчитанная как attached, дала бы
    ложноположительный и практически необратимый пропуск валидной вакансии.

    #241 cycle-review round 1 (codex): blocking=True (по умолчанию, ветка
    "кнопка не найдена" в pipeline._run) ждёт видимость с полным таймаутом —
    транзитное SPA-состояние ещё может дорендерить маркер. blocking=False
    (ветка "кнопка уже подтверждена wait_apply_button") проверяет видимость
    БЕЗ ожидания: комбинированный wait_for в wait_apply_button уже дождался
    отрисовки DOM, повторный полный таймаут на каждой обычной вакансии (до
    2 x _VISIBILITY_CHECK_TIMEOUT_MS) был чистым оверхедом на happy path.
    """
    from ..selector_groups import vacancy_page

    timeout = _VISIBILITY_CHECK_TIMEOUT_MS if blocking else 0
    for selector in (
        vacancy_page.VACANCY_ALREADY_RESPONDED_AGAIN,
        vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT,
    ):
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        except PlaywrightError:
            continue
        reason = f"уже откликались по вакансии {vacancy.vacancy_id}, пропуск"
        logger.info("%s — %s", vacancy.title, reason)
        return reason
    logger.debug("Вакансия '%s': маркеры уже отклика не найдены", vacancy.title)
    return None
