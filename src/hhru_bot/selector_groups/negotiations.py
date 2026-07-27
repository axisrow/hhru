"""Страница откликов и переписки (/applicant/negotiations) — НЕ подтверждено.

Владелец: #12 (мониторинг ответов работодателей).

Статус проверки: НЕ подтверждено. В отличие от страниц поиска/вакансии
(подтверждённых curl-дампом без логина), /applicant/negotiations рендерится
ТОЛЬКО залогиненному пользователю через JS — анонимный curl её не отдаёт.
Селекторы ниже построены по общему паттерну hh.ru data-qa (страница откликов
оформлена тем же компонентным стилем, что serp: ``negotiations-item__...``) и
ОБЯЗАТЕЛЬНО сверяются вручную перед первым боевым запуском (F12 → Elements на
живой залогиненной сессии), как и непроверенные селекторы apply_form/resume_page.
Первый подозреваемый при пустом/кривом сборе responses — устаревший селектор
здесь, а не логика responses.py.

Маркер статуса (invitation/discard/response/read) на этой странице hh.ru
представляет собой бейдж в шапке карточки переписки; ``NEGOTIATION_STATUS``
указывает на контейнер бейджа, текст которого нормализуется в responses.py.
"""

from __future__ import annotations

# Карточка одной переписки в списке откликов/приглашений.
NEGOTIATION_ITEM = "[data-qa='negotiations-item']"
# Ссылка на вакансию внутри карточки — из её href достаём vacancy_id и chat_url.
NEGOTIATION_VACANCY_LINK = "[data-qa='negotiations-item__vacancy-link']"
# Название компании-работодода. Опционально (hh.ru иногда прячет для анонимных
# вакансий) — пустая строка, если элемента нет.
NEGOTIATION_EMPLOYER = "[data-qa='negotiations-item__employer']"
# Бейдж текущего статуса переписки (текст нормализуется: Приглашение→invitation
# и т.д.). Опциональный: для свежего отклика без ответа статуса-бейджа может не быть.
NEGOTIATION_STATUS = "[data-qa='negotiations-item__state']"
# Ссылка «перейти в чат» с работодателем — chat_url (опциональна: у части статусов
# чата нет, напр. discard). Берётся из href, как fallback — href ссылки вакансии.
NEGOTIATION_CHAT_LINK = "[data-qa='negotiations-item__messages-link']"
# Кнопка «следующая страница» пагинации списка (та же data-qa, что и в поиске,
# но вынесена сюда, чтобы responses.py не зависел от search_page).
NEGOTIATIONS_PAGINATION_NEXT = "[data-qa='pager-next']"
