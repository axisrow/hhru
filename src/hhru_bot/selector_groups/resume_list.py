"""Список резюме (/applicant/resumes) — селекторы для copy-resume (#116)
и list-resumes (#135).

Статус проверки (live-дамп под залогиненной сессией, 2026-08-01):

- RESUME_LIST_CARD, RESUME_LIST_CARD_LINK_TPL, RESUME_LIST_ACTION_MORE —
  ПОДТВЕРЖДЕНО: присутствуют в статичном DOM дампа /applicant/resumes.
- RESUME_DUPLICATE_MENU_ITEM, RESUME_DUPLICATE_INLINE — ПОДТВЕРЖДЕНО
  КОСВЕННО: в статичном DOM их нет (рендерятся по клику на «...»), значения
  извлечены из живых JS-бандлов hh.ru (i.hh.ru/build/373.*.js,
  components-common-js.*.js: DuplicateActionComponent /
  DuplicateOperationComponent). Клик по любому из них отправляет
  POST /applicant/resumes/clone?resume=<hash>; при лимите резюме
  (resumeLimits.remaining == 0, «Более 20 резюме создать нельзя») кнопки
  НЕ рендерятся вовсе.
- RESUME_LIST_CARD_TITLE — НЕ ПОДТВЕРЖДЕНО: значение по аналогии с
  ``resume-topic-title`` (селект формы отклика, apply_form.py) на живом
  дампе /applicant/resumes не проверялся. list_resume_cards() поэтому
  трактует его отсутствие как «заголовок неизвестен» (title=""), а не
  как ошибку — падать из-за непроверенного селектора недопустимо.

«dublicate» в RESUME_DUPLICATE_INLINE — опечатка самого hh.ru, не наша.
"""

from __future__ import annotations

# Карточка одного резюме в списке.
RESUME_LIST_CARD = "[data-qa='resume']"

# Ссылка на резюме внутри карточки; hash — resume_id из URL резюме.
# Используется для привязки карточки к конкретному резюме (identity-bound, #33).
RESUME_LIST_CARD_LINK_TPL = "[data-qa='resume-card-link-{resume_id}']"

# Кнопка «...» (меню действий) на карточке резюме.
RESUME_LIST_ACTION_MORE = "[data-qa='resume-list-action-more']"

# Дополнительный (не обязательный) маркер завершённого client render. Карточка
# приходит из SSR раньше, но actions ещё не подключены: на наблюдавшемся живом
# hh.ru кнопка «...» начинала работать после появления этого текста. Истиной
# для WRITE всё равно остаётся единственное фактическое «Дублировать».
RESUME_PROFILE_READY = "button:has-text('подходящие вакансии')"

# Пункт «Дублировать» в открытом меню «...».
RESUME_DUPLICATE_MENU_ITEM = "[data-qa='operations-list-duplicate-resume']"

# Инлайн-кнопка «Дублировать» (второй вариант рендера того же действия).
RESUME_DUPLICATE_INLINE = "[data-qa='resume-dublicate']"

# Заголовок (название) резюме внутри карточки — НЕ ПОДТВЕРЖДЕНО (см. докстринг).
RESUME_LIST_CARD_TITLE = "[data-qa='resume-title']"
