"""Страница поиска вакансий (/search/vacancy) — подтверждено curl-дампом."""

from __future__ import annotations

from ._generated import optional_selector as _optional_selector
from ._generated import selector as _selector

VACANCY_CARD = _selector("search_page.VACANCY_CARD")
# Подтверждён curl-дампом пустого запроса (2026-08-11). Это единственный
# достоверный случай, в котором отсутствие VACANCY_CARD означает пустую выдачу,
# а не медленный JS-рендер или дрейф карточного селектора.
VACANCY_SEARCH_EMPTY = _selector("search_page.VACANCY_SEARCH_EMPTY")
VACANCY_CARD_TITLE_LINK = _selector("search_page.VACANCY_CARD_TITLE_LINK")
VACANCY_CARD_COMPANY = _selector("search_page.VACANCY_CARD_COMPANY")
# Зарплата в карточке списка (issue #14).
# VACANCY_CARD_COMPENSATION: НЕ работает на живом hh.ru с 2025 — hh.ru
# перешёл на magritte-разметку, блок ЗП рендерится без этого data-qa (#73).
# search.py использует regex-fallback по innerHTML карточки когда селектор
# пуст. Селектор оставлен для обратной совместимости (старые кэши/curl-дампы).
VACANCY_CARD_COMPENSATION = _optional_selector("search_page.VACANCY_CARD_COMPENSATION")
# Подтверждено фикстурами карточки (тексты вида "сегодня"/"вчера"/"N дней
# назад", см. tests/fixtures/vacancy_card_*.html). В анонимном SSR-дампе от
# 2026-08-20 поле не рендерится, поэтому парсер обязан считать его опциональным.
VACANCY_CARD_PUBLICATION_TIME = _selector("search_page.VACANCY_CARD_PUBLICATION_TIME")
# Инфо о работодателе из карточки поиска (issue #74, Этап 1). Подтверждено в
# DOM-дампе (read-only, 2026-07-28): rating/trusted рендерятся в карточке
# списка для работодателей с отзывами. Блоки опциональны.
COMPANY_RATING_VALUE = _selector("search_page.COMPANY_RATING_VALUE")
COMPANY_RATING_REVIEWS_COUNT = _selector("search_page.COMPANY_RATING_REVIEWS_COUNT")
TRUSTED_EMPLOYER_LINK = _selector("search_page.TRUSTED_EMPLOYER_LINK")
# Кнопка отклика прямо в карточке списка (ведёт на
# /applicant/vacancy_response?vacancyId=...&employerId=...)
VACANCY_CARD_RESPONSE_BUTTON = _selector("search_page.VACANCY_CARD_RESPONSE_BUTTON")
# Пагинация (#123). hh.ru отдаёт ДВА варианта разметки пагинации, и это
# подтверждено живым залогиненным дампом (2026-08-01):
#   1. с кнопками навигации — есть [data-qa='pager-next'];
#   2. без них — контейнер несёт класс-модификатор
#      `magritte-number-pages-wrapper-without-navigation-buttons`, и
#      pager-next НЕ рендерится вообще (ожидание его не находит никогда).
# В обоих вариантах присутствуют пронумерованные ссылки pager-page, поэтому
# признак «есть следующая страница» берётся от них, а не от pager-next.
# Вариант вёрстки прилетает независимо от запроса (похоже на A/B-тест), так
# что опираться на один только PAGINATION_NEXT нельзя.
PAGINATION_NEXT = _selector("search_page.PAGINATION_NEXT")
PAGINATION_PAGE = _selector("search_page.PAGINATION_PAGE")
# pager-block присутствует только когда hh.ru действительно рисует пагинацию.
# Его отсутствие после готовой выдачи означает нормальную единственную страницу.
PAGINATION_BLOCK = _selector("search_page.PAGINATION_BLOCK")

# Маркера "уже откликались" на карточке выдачи у hh.ru НЕТ (#703, live 2026-08-30):
# карточка вакансии, на которую отклик реально отправлен (есть и в actions, и в
# /applicant/negotiations), в живой выдаче не несёт ни отдельного data-qa, ни текста
# «Вы откликнулись». Прежний кандидат vacancy-serp__vacancy_response_status удалён как
# опровергнутый; дедупликация здесь и не нужна — она делается по локальной истории
# (history.py), см. «Дедупликация откликов» в CLAUDE.md.

# Доп. признаки карточки для статистики/ML (issue #517, приоритет-1 из #516).
# Подтверждено ВРУЧНУЮ через DevTools/JS в браузере 2026-08-23 (read-only
# просмотр живой выдачи /search/vacancy) — НЕ curl-дамп и НЕ боевой
# Playwright-лог, уровень подтверждения ниже, чем у остальных селекторов
# этого файла. Все блоки опциональны (не у каждой карточки).
VACANCY_CARD_ADDRESS = _selector("search_page.VACANCY_CARD_ADDRESS")
VACANCY_CARD_REMOTE_LABEL = _selector("search_page.VACANCY_CARD_REMOTE_LABEL")
# Категория опыта закодирована в САМОМ суффиксе data-qa, а не в тексте
# элемента: noExperience/between1And3/between3And6/moreThan6.
VACANCY_CARD_EXPERIENCE = _selector("search_page.VACANCY_CARD_EXPERIENCE")
VACANCY_CARD_SNIPPET_REQUIREMENT = _selector("search_page.VACANCY_CARD_SNIPPET_REQUIREMENT")
VACANCY_CARD_SNIPPET_RESPONSIBILITY = _selector("search_page.VACANCY_CARD_SNIPPET_RESPONSIBILITY")
# Приоритет-2 из issue #516: опциональные бейджи типа занятости/отклика.
# Подтверждено вручную через DevTools/JS в браузере 2026-08-23 (read-only,
# живая выдача /search/vacancy); это не curl-дамп и не боевой Playwright-лог.
VACANCY_CARD_SIDE_JOB = _selector("search_page.VACANCY_CARD_SIDE_JOB")
VACANCY_CARD_NO_RESUME = _selector("search_page.VACANCY_CARD_NO_RESUME")
# Приоритет-3 из issue #551. Поля редкие/опциональные; значения сохраняются
# как наблюдены, без попытки угадать их семантику или формат.
VACANCY_CARD_ACTIVITY = _selector("search_page.VACANCY_CARD_ACTIVITY")
VACANCY_CARD_HH_RATING = _selector("search_page.VACANCY_CARD_HH_RATING")
# Exact data-qa value from the issue/live-card schema; class names are not a
# stable contract for this marker.
VACANCY_CARD_HRBRAND_WINNER = _selector("search_page.VACANCY_CARD_HRBRAND_WINNER")
VACANCY_CARD_METRO_STATION = _selector("search_page.VACANCY_CARD_METRO_STATION")
