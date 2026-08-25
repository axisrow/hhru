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

from ._generated import optional_selector as _optional_selector
from ._generated import selector as _selector

# Карточка одной переписки в списке откликов/приглашений.
NEGOTIATION_ITEM = _selector("negotiations.NEGOTIATION_ITEM")
# Вакансия внутри карточки. Живая разметка (#44) — <span> без href, вложенный
# в <a href="/vacancy/...">; vacancy_id/chat_url достаёт _href_or_ancestor_href()
# в responses.py, поднимаясь до предка <a>, если у самого узла href нет.
NEGOTIATION_VACANCY_LINK = _selector("negotiations.NEGOTIATION_VACANCY_LINK")
# Название компании-работодода. Опционально (hh.ru иногда прячет для анонимных
# вакансий) — пустая строка, если элемента нет.
NEGOTIATION_EMPLOYER = _selector("negotiations.NEGOTIATION_EMPLOYER")
# Бейдж текущего статуса переписки (текст нормализуется: Приглашение→invitation
# и т.д.). Опциональный: для свежего отклика без ответа статуса-бейджа может не быть.
# Точное совпадение data-qa со значением, содержащим пробел ("negotiations-tag
# negotiations-item-not-viewed"), никогда не матчится реальной разметке —
# подтверждено префиксным селектором ниже (используется рабочим probe.py).
# Статус определяется по нормализации ТЕКСТА бейджа (normalize_status в
# responses.py), а не по различению viewed/not-viewed на уровне CSS, поэтому
# один префиксный селектор покрывает оба состояния.
NEGOTIATION_STATUS = _selector("negotiations.NEGOTIATION_STATUS")
# Дата ответа/последнего сообщения в карточке (как текст; парсинг конкретной
# даты не делается — форматы hh.ru зависят от локали).
# Опциональна: hh.ru не всегда рендерит блок даты.
NEGOTIATION_DATE = _selector("negotiations.NEGOTIATION_DATE")
# Ссылка «перейти в чат» с работодателем — chat_url (опциональна: у части статусов
# чата нет, напр. discard). Берётся из href, как fallback — href ссылки вакансии.
NEGOTIATION_CHAT_LINK = _selector("negotiations.NEGOTIATION_CHAT_LINK")
# Кнопка «следующая страница» пагинации списка (та же data-qa, что и в поиске,
# но вынесена сюда, чтобы responses.py не зависел от search_page).
NEGOTIATIONS_PAGINATION_NEXT = _selector("negotiations.NEGOTIATIONS_PAGINATION_NEXT")
# Номера страниц — fallback для A/B-варианта hh.ru без pager-next.
NEGOTIATIONS_PAGINATION_PAGE = _selector("negotiations.NEGOTIATIONS_PAGINATION_PAGE")
# Контейнер пагинации общего компонента hh.ru; нет на честной единственной странице.
NEGOTIATIONS_PAGINATION_BLOCK = _selector("negotiations.NEGOTIATIONS_PAGINATION_BLOCK")

# Write controls are intentionally kept separate from the read selectors above.
# The exact data-qa names below must be rechecked against a logged-in DOM before
# the first real withdrawal; an unknown or changed selector is a refusal, never
# a reason to fall back to a guessed button or a direct HTTP request.
NEGOTIATION_WITHDRAW = _optional_selector("negotiations.NEGOTIATION_WITHDRAW")
NEGOTIATION_WITHDRAW_CONFIRM = _optional_selector("negotiations.NEGOTIATION_WITHDRAW_CONFIRM")
NEGOTIATION_WITHDRAW_SUCCESS = _optional_selector("negotiations.NEGOTIATION_WITHDRAW_SUCCESS")

# Compatibility with old saved markup fixtures during selector migration.
LEGACY_NEGOTIATION_VACANCY_LINK = _selector("negotiations.LEGACY_NEGOTIATION_VACANCY_LINK")
LEGACY_NEGOTIATION_EMPLOYER = _selector("negotiations.LEGACY_NEGOTIATION_EMPLOYER")
LEGACY_NEGOTIATION_STATUS = _selector("negotiations.LEGACY_NEGOTIATION_STATUS")
LEGACY_NEGOTIATION_DATE = _selector("negotiations.LEGACY_NEGOTIATION_DATE")
LEGACY_NEGOTIATION_CHAT_LINK = _selector("negotiations.LEGACY_NEGOTIATION_CHAT_LINK")

# Chat route (chatik.hh.ru/chat/<chatId>), confirmed by probe --negotiations
# --topic (#107). The text node is message-specific; its ancestor carries
# message_my/message_other, so callers can distinguish our own messages from
# employer messages without clicking or posting anything. The applicant-action
# system message (e.g. "отклик отправлен") is excluded — it carries no
# message_my/message_other marker, so without this exclusion it would be
# treated as an employer message by any :not(message_my) check.
CHAT_MESSAGE_TEXT = _selector("negotiations.CHAT_MESSAGE_TEXT")
CHAT_MESSAGE_ROOT = _selector("negotiations.CHAT_MESSAGE_ROOT")
CHAT_AUTHOR_HINT = _selector("negotiations.CHAT_AUTHOR_HINT")
CHAT_MESSAGE_MY_MARKER = "message_my"
CHAT_MESSAGE_OTHER_MARKER = "message_other"

# Composer controls on chatik.hh.ru.  Keep these here with the read selectors so
# a markup change cannot leave the write path with a private, stale selector.
CHAT_MESSAGE_INPUT = _selector("negotiations.CHAT_MESSAGE_INPUT")
CHAT_MESSAGE_SEND = _selector("negotiations.CHAT_MESSAGE_SEND")
