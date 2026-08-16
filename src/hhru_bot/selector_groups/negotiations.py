"""Страница откликов и переписки (/applicant/negotiations), сверенная 2026-08-16.

Владелец: #12 (мониторинг ответов работодателей).

Список переговоров подтверждён на живой залогиненной сессии. Сообщения доступны
отдельно через read-only маршрут ``https://chatik.hh.ru/chat/<chatId>``; SSR
``topicList`` связывает ``topic id`` с ``chatId``.

Маркер статуса (invitation/discard/response/read) на этой странице hh.ru
представляет собой бейдж в шапке карточки переписки; ``NEGOTIATION_STATUS``
указывает на контейнер бейджа, текст которого нормализуется в responses.py.
"""

from __future__ import annotations

# Карточка одной переписки в списке откликов/приглашений.
NEGOTIATION_ITEM = "[data-qa='negotiations-item']"
# Ссылка на вакансию внутри карточки — из её href достаём vacancy_id и chat_url.
NEGOTIATION_VACANCY_LINK = "[data-qa='negotiations-item-vacancy']"
# Название компании-работодода. Опционально (hh.ru иногда прячет для анонимных
# вакансий) — пустая строка, если элемента нет.
NEGOTIATION_EMPLOYER = "[data-qa='negotiations-item-company']"
# Бейдж текущего статуса переписки (текст нормализуется: Приглашение→invitation
# и т.д.). Опциональный: для свежего отклика без ответа статуса-бейджа может не быть.
# Точное совпадение data-qa со значением, содержащим пробел ("negotiations-tag
# negotiations-item-not-viewed"), никогда не матчится реальной разметке —
# подтверждено префиксным селектором ниже (используется рабочим probe.py).
# Статус определяется по нормализации ТЕКСТА бейджа (normalize_status в
# responses.py), а не по различению viewed/not-viewed на уровне CSS, поэтому
# один префиксный селектор покрывает оба состояния.
NEGOTIATION_STATUS = "[data-qa^='negotiations-tag']"
# Дата ответа/последнего сообщения в карточке (как текст; парсинг конкретной
# даты не делается — форматы hh.ru зависят от локали).
# Опциональна: hh.ru не всегда рендерит блок даты.
NEGOTIATION_DATE = "[data-qa='negotiations-item-date']"
# Ссылка «перейти в чат» с работодателем — chat_url (опциональна: у части статусов
# чата нет, напр. discard). Берётся из href, как fallback — href ссылки вакансии.
NEGOTIATION_CHAT_LINK = "[data-qa='open_chat']"
# Кнопка «следующая страница» пагинации списка (та же data-qa, что и в поиске,
# но вынесена сюда, чтобы responses.py не зависел от search_page).
NEGOTIATIONS_PAGINATION_NEXT = "[data-qa='pager-next']"
# Номера страниц — fallback для A/B-варианта hh.ru без pager-next.
NEGOTIATIONS_PAGINATION_PAGE = "[data-qa='pager-page']"
# Контейнер пагинации общего компонента hh.ru; нет на честной единственной странице.
NEGOTIATIONS_PAGINATION_BLOCK = "[data-qa='pager-block']"

# Compatibility with old saved markup fixtures during selector migration.
LEGACY_NEGOTIATION_VACANCY_LINK = "[data-qa='negotiations-item__vacancy-link']"
LEGACY_NEGOTIATION_EMPLOYER = "[data-qa='negotiations-item__employer']"
LEGACY_NEGOTIATION_STATUS = "[data-qa='negotiations-item__state']"
LEGACY_NEGOTIATION_DATE = "[data-qa='negotiations-item__date']"
LEGACY_NEGOTIATION_CHAT_LINK = "[data-qa='negotiations-item__messages-link']"

# Chat route (chatik.hh.ru/chat/<chatId>), confirmed by probe --negotiations
# --topic (#107). The text node is message-specific; its ancestor carries
# message_my/message_other, so callers can distinguish our own messages from
# employer messages without clicking or posting anything. The applicant-action
# system message (e.g. "отклик отправлен") is excluded — it carries no
# message_my/message_other marker, so without this exclusion it would be
# treated as an employer message by any :not(message_my) check.
CHAT_MESSAGE_TEXT = (
    '[data-qa^="chatik-chat-message-"][data-qa$="-text"]'
    ':not([data-qa="chatik-chat-message-applicant-action-text"])'
)
CHAT_MESSAGE_MY_MARKER = "message_my"
