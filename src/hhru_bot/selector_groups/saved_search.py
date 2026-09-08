"""Автопоиски hh.ru: кнопка сохранения на /search/vacancy и страница списка.

Живые замеры #1052 (census + разведочный клик 2026-09-08, залогиненная
сессия основного аккаунта):

- Кнопка «Сохранить поиск» рядом с «Найти» на /search/vacancy; клик НЕ
  мутирует hh.ru (контроль: список /applicant/autosearch остался пуст) и
  открывает Magritte-tooltip «Куда присылать новые вакансии по этому
  поиску?» с выбором канала: «На почту» / «В мессенджер». Поля имени в UI
  НЕТ — hh.ru именует автопоиск сам.
- Escape tooltip НЕ закрывает (замер 2026-09-08); следующий шаг после
  выбора канала не исследован — клик по каналу считается мутирующей
  границей и без боевого разрешения не выполняется.
- Страница списка автопоисков — /applicant/autosearch («Избранное →
  Поиски»). Подтверждено только пустое состояние; строки непустого списка
  живым замером не наблюдались.
"""

from __future__ import annotations

from ._generated import selector as _selector

# Кнопка «Сохранить поиск» в строке поиска /search/vacancy. Census 2026-09-08:
# tag=button, innerText=«Сохранить поиск», видима. Tooltip с выбором канала
# рендерится по клику (React), в покоящемся DOM отсутствует.
SEARCH_SAVE_BUTTON = _selector("saved_search.SEARCH_SAVE_BUTTON")
# Tooltip выбора канала уведомлений (magritte-tooltip + drop-base, role=tooltip).
# Маркер того, что клик по SEARCH_SAVE_BUTTON открыл именно попап сохранения.
SEARCH_SAVE_DROPDOWN = _selector("saved_search.SEARCH_SAVE_DROPDOWN")
# Кнопка «На почту» в tooltip: выбирает email-канал для автопоиска.
SEARCH_SAVE_CHANNEL_EMAIL = _selector("saved_search.SEARCH_SAVE_CHANNEL_EMAIL")
# Кнопка «В мессенджер» в tooltip: выбирает мессенджер-канал (путь мессенджера
# боевым прогоном не исследовался; сохранение через email подтверждено).
SEARCH_SAVE_CHANNEL_MESSENGERS = _selector("saved_search.SEARCH_SAVE_CHANNEL_MESSENGERS")
# Кнопка «Сохранён» — состояние строки поиска СРАЗУ после успешного сохранения
# (замена vacancy-saved-search-create; позитивный маркер успеха в той же
# сессии). НЕ персистентный маркер дубля: живой census 2026-09-08 показал, что
# в новой сессии на выдаче с точно сохранённой параметрикой кнопка снова
# «Сохранить поиск». Надёжный дубль-детект — по списку автопоисков.
SEARCH_SAVE_CREATED = _selector("saved_search.SEARCH_SAVE_CREATED")
# Вкладка «Поиски» на странице «Избранное» (/applicant/autosearch открывает её
# сразу; селектор оставлен как маркер того, что открылась нужная вкладка).
FAVORITES_SEARCHES_TAB = _selector("saved_search.FAVORITES_SEARCHES_TAB")
# Пустое состояние списка автопоисков («Ничего нет. В поиске выберите фильтры
# и нажмите на значок поиска с сердечком...»).
AUTOSEARCH_EMPTY = _selector("saved_search.AUTOSEARCH_EMPTY")
# Карточка одного автопоиска в списке /applicant/autosearch. Боевой readback
# 2026-09-08: текст вида «8 сентября python Москва 999+ вакансий».
AUTOSEARCH_ITEM = _selector("saved_search.AUTOSEARCH_ITEM")
# Чекбокс строки автопоиска: aria-label = имя автопоиска, назначенное hh.ru
# (совпадает с text запроса — UI имени не запрашивает).
AUTOSEARCH_NAME_CHECKBOX = _selector("saved_search.AUTOSEARCH_NAME_CHECKBOX")
# Ссылка «N вакансий» строки автопоиска: href = выдача с параметрами
# автопоиска (/search/vacancy?text=…&area=…&saved_search_id=…).
AUTOSEARCH_URL_LINK = _selector("saved_search.AUTOSEARCH_URL_LINK")
