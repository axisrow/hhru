"""Жалоба на вакансию (issue #745) — подтверждено живым DOM залогиненного
пользователя 2026-08-29 на vacancy_id=136672001 (см. PR тела issue/PR для
дампов DOM). Микрофронтенд ``employer-reviews-front`` (`complain_button`,
`complain_button_menu_item`) рендерит два слоя: пункт в дропдауне
"Ещё" (может быть ``disabled`` с текстом "Оставили жалобу", если жалоба на
эту вакансию уже была отправлена этим аккаунтом) и, после клика по нему,
ОТДЕЛЬНУЮ кнопку на самой странице вакансии — именно она открывает форму
жалобы (bloko-modal, не magritte-dropdown).

Форма — трёхшаговый bloko-modal wizard:
  1. Причина (радио) — 9 причин, "Продолжить" ЗАБЛОКИРОВАН, пока причина не
     выбрана.
  2. Комментарий (textarea) — commentRequired=true для ВСЕХ 9 причин без
     исключения (см. ``complainDictionaries.reasons`` в SSR JSON вакансии),
     "Продолжить" на этом шаге ЗАБЛОКИРОВАН, пока комментарий пуст.
  3. Финальное подтверждение — НЕ подтверждено: разведка issue #745 прямо
     запрещает клик по кнопке, ведущей дальше шага 2 (см. CLAUDE.md, "жалобу
     не отправлять ни при каких обстоятельствах"). VACANCY_COMPLAIN_SUBMIT
     ниже НЕ существует как активный селектор — команда report-vacancy
     останавливается на шаге 2 с [FAIL]/needs_live_evidence.
"""

from __future__ import annotations

from ._generated import selector as _selector

# Пункт "Пожаловаться на вакансию" в дропдауне "Ещё" на странице вакансии.
# ВНИМАНИЕ: элемент может быть disabled ("Оставили жалобу") — проверка
# is_disabled() обязательна перед кликом, диздейбл не читается через count().
VACANCY_COMPLAIN_MENU_ITEM = _selector("vacancy_complain.VACANCY_COMPLAIN_MENU_ITEM")

# Кнопка "Ещё" в шапке вакансии, открывающая дропдаун с VACANCY_COMPLAIN_MENU_ITEM.
VACANCY_MORE_ACTIONS = _selector("vacancy_complain.VACANCY_MORE_ACTIONS")

# Реальный триггер формы жалобы. Появляется НА СТРАНИЦЕ (не в дропдауне)
# только после клика по VACANCY_COMPLAIN_MENU_ITEM — это отдельный React-узел
# микрофронтенда employer-reviews-front, а не то же самое, что пункт меню.
VACANCY_COMPLAIN_PAGE_BUTTON = _selector("vacancy_complain.VACANCY_COMPLAIN_PAGE_BUTTON")

# Шаг 1: контейнер модалки (bloko-modal, НЕ magritte-drop).
VACANCY_COMPLAIN_MODAL = _selector("vacancy_complain.VACANCY_COMPLAIN_MODAL")
VACANCY_COMPLAIN_MODAL_CLOSE = _selector("vacancy_complain.VACANCY_COMPLAIN_MODAL_CLOSE")

# Шаг 1: причина — {reason} это ID из VACANCY_COMPLAIN_REASON_IDS (ниже).
# bloko-radio: сам <input> визуально скрыт под своим <label> — клик нужно
# делать по label/родителю, а не по input напрямую (перекрыт своим текстом).
VACANCY_COMPLAIN_REASON_RADIO = _selector("vacancy_complain.VACANCY_COMPLAIN_REASON_RADIO")

# Полный перечень допустимых причин жалобы, подтверждённый живым SSR JSON
# вакансии (``complainDictionaries.reasons``, 2026-08-29). Порядок как на
# hh.ru. Каждая причина требует комментарий (commentRequired=true) — ни одна
# исключения не имеет, включая "Другое".
VACANCY_COMPLAIN_REASON_IDS = (
    "MISLEADING_DESCRIPTION",  # Условия в вакансии отличаются
    "EMPLOYMENT_VIOLATION",  # Нарушают права
    "NO_RESPONSE",  # Игнорируют
    "REJECTION_WITHOUT_REASON",  # Отказали без причины
    "FEE_REQUIRED",  # Просят вложиться или купить
    "FRAUDULENT_LINK",  # Прислали подозрительную ссылку
    "PROHIBITED_ACTIVITY",  # Ведут запрещённую деятельность
    "DOUBTFUL_VACANCY",  # Я просто сомневаюсь в вакансии
    "OTHER",  # Другое
)

# Шаг 1/2 общая кнопка "Продолжить" (тот же data-qa на обоих шагах wizard'а).
# Заблокирована (disabled), пока условие текущего шага не выполнено (причина
# выбрана / комментарий не пуст). ЭТА кнопка на шаге 2 — последняя, что
# подтверждена и допустима к клику; шаг 3 (финальная отправка) не исследован.
VACANCY_COMPLAIN_WIZARD_NEXT = _selector("vacancy_complain.VACANCY_COMPLAIN_WIZARD_NEXT")
VACANCY_COMPLAIN_WIZARD_PREV = _selector("vacancy_complain.VACANCY_COMPLAIN_WIZARD_PREV")
VACANCY_COMPLAIN_WIZARD_CANCEL = _selector("vacancy_complain.VACANCY_COMPLAIN_WIZARD_CANCEL")

# Шаг 2: комментарий (commentRequired=true для всех причин).
VACANCY_COMPLAIN_COMMENT_TEXTAREA = _selector("vacancy_complain.VACANCY_COMPLAIN_COMMENT_TEXTAREA")
