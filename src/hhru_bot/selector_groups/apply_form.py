"""Форма отклика на /applicant/vacancy_response — подтверждённые shared-селекторы.

Смягчение #3↔#7: здесь только shared-селекторы формы (resume-select, letter
toggle/textarea, submit), которые #3 и #7 не трогают. Селектор успешной отправки
живёт у владельца — apply/success.py (#7). «Уже откликались» (#3) селектора не
имеет вовсе: дедупликация идёт через history.has_applied() в filter_candidates(),
см. apply/dedup.py.
"""

from __future__ import annotations

# сверено живым DOM (F12, /applicant/vacancy_response,
# multi-resume аккаунт, 2026-08-20). APPLY_RESUME_SELECT был непроверенной
# заглушкой и не существует на реальной форме (`resume-topic-title` не
# найден вообще). APPLY_COVER_LETTER_TOGGLE проверен и совпал с уже бывшим
# значением (сверено на вакансии с опциональным письмом на ПОЛНОЙ форме —
# см. предупреждение ниже про то, какая форма имеется в виду).
#
# Реальная структура выбора резюме (multi-resume): свёрнутый триггер —
# первый `[data-qa='resume-title']` внутри `role=button`-контейнера без
# собственного data-qa; клик по нему раскрывает список опций, каждая —
# `<label data-qa="magritte-select-option-{resume_id}" role="option">`
# (resume_id — «голый» id резюме, тот же хвост, что в resume_url, БЕЗ href
# у самого label — атрибута href на форме нет вовсе, старое сопоставление
# по href было в принципе нерабочим). APPLY_RESUME_SELECT ниже — это
# ТРИГГЕР (для открытия), а не коллекция опций — см. новую сигнатуру
# _select_resume_in_form() в apply/steps.py.
#
# ВАЖНО (пересмотрено 2026-08-20 по боевым дампам). hh.ru рендерит форму отклика
# в ДВУХ shape с похожим, но не идентичным DOM:
#   * МОДАЛКА на самой странице вакансии — `form#RESPONSE_MODAL_FORM_ID`,
#     letter-toggle `add-cover-letter`, textarea `…-popup-form-letter-input`;
#   * полная страница `/applicant/vacancy_response` — letter-toggle
#     `vacancy-response-letter-toggle`, textarea `vacancy-response-form-letter-input`.
#
# Прежнее утверждение «бот использует ВТОРУЮ» ОПРОВЕРГНУТО: во всех дампах
# `data/logs/apply_*` (начиная с 2026-08-16) `<link rel="canonical">` остаётся
# `/vacancy/{id}` — навигации не происходит, работает МОДАЛКА. Кнопка отклика
# по-прежнему `<a href="/applicant/vacancy_response…">`, но hh.ru перехватывает
# клик JS. Надёжный маркер shape в дампе — `form="RESPONSE_MODAL_FORM_ID"`;
# `add-cover-letter` маркером НЕ является (зависит от состояния письма: если
# hh.ru отрендерил textarea уже развёрнутой, тоггла в DOM нет вовсе).
#
# Обе ветки поддержаны в steps.fill_response_form через Locator.or_. Full-page
# селекторы НЕ удалять: оба shape наблюдались в дампах одного дня (08-16).
APPLY_RESUME_SELECT = "[data-qa='resume-title']"
APPLY_RESUME_OPTION_PREFIX = "magritte-select-option-"
APPLY_COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
# Тоггл письма МОДАЛКИ. Подтверждён дампами 2026-08-20 (apply_136190065/136190066):
#   <button type="button" data-qa="add-cover-letter"> внутри data-qa="actions-container"
# Живёт ВНЕ <form>, а раскрываемая им textarea (APPLY_COVER_LETTER_TEXTAREA,
# `…-popup-form-letter-input`) — ВНУТРИ формы, поэтому скоупить тоггл формой нельзя.
# Критично: до добавления этой константы адресовался только APPLY_COVER_LETTER_TOGGLE
# выше, и письмо молча терялось — измерено по SSR topicList[].hasResponseLetter:
# из 18 откликов аккаунта с письмом ушло 2, без — 16.
#
# УТОЧНЕНО по всем 95 дампам data/logs/: прежняя формулировка «full-page тоггл в
# модалке не совпадает НИ РАЗУ» неверна. `vacancy-response-letter-toggle` встречается
# настоящим DOM-элементом в 5 дампах, несущих маркер модалки RESPONSE_MODAL_FORM_ID
# (probe_135721455, 136067340, 136230349/50/51), а `add-cover-letter` — в 13. То есть
# hh.ru рендерит в модалке ОБА варианта тоггла, и `Locator.or_` нужен именно поэтому —
# ни один из двух селекторов по отдельности не покрывает все наблюдавшиеся случаи.
#
# Тоггл и раскрытая textarea НЕ СОСУЩЕСТВУЮТ: пересечение по 95 дампам пустое
# (hh.ru ЗАМЕНЯЕТ тоггл на textarea — probe_136190065: initial тоггл=1/ta=0,
# form тоггл=0/ta=1). Поэтому безусловный клик по видимому тогглу не может свернуть
# уже развёрнутое поле.
APPLY_COVER_LETTER_TOGGLE_POPUP = "[data-qa='add-cover-letter']"
# Всплывающая панель со списком резюме (Magritte drop-base). Источник подтверждения —
# боевой лог 2026-08-20 (`data/logs/hhru_bot.log`): в сообщении Playwright об
# интерсепте напечатан РОВНО ОДИН элемент
# `<div id=":r15:" role="listbox" data-qa="drop-base" …> subtree intercepts pointer
# events` — то есть локатор single-match и панель НЕ закрывается сама после выбора.
# ВАЖНО, чтобы не повторить прежнюю ошибку: в probe-HTML-дампах атрибута
# `data-qa="drop-base"` НЕТ вовсе (0 вхождений) — встречающиеся там `drop-base`
# это CSS-классы/атрибуты Magritte (`data-magritte-drop-base-direction`), они
# доказательством не являются. Ссылаться на дампы в этом вопросе нельзя.
# Панель позиционирована абсолютно (z-index 2250, height ~281px) и физически
# перекрывает submit в футере модалки — из-за неё Locator.click по submit
# ретраил 30с с `subtree intercepts pointer events`.
#
# Список резюме внутри панели — постоянно видимые карточки-опции
# (`magritte-select-option-{resume_id}`, выбранная несёт aria-selected="true"),
# поэтому ждать СКРЫТИЯ ОПЦИИ нельзя: она остаётся visible, пока панель открыта.
# Закрывать нужно саму панель, а её исчезновение — по этому селектору.
APPLY_RESUME_DROPDOWN = "[data-qa='drop-base']"
APPLY_COVER_LETTER_TEXTAREA = "textarea[data-qa='vacancy-response-popup-form-letter-input']"
APPLY_SUBMIT_BUTTON = "[data-qa='vacancy-response-submit-popup']"

# --- #95: детекция тест-вопросов/анкет в форме отклика (detect-only, NO auto-answer) ---
# Подтверждено konard reference (hh-selectors.mjs / qa.mjs, production hh.ru automation).
# task-body — контейнер вопроса; task-question — текст вопроса внутри него. На нашем
# аккаунте НЕ сверялись живым дампом, но konard использует их в боевом коде.
APPLY_QUESTION_BODY = "[data-qa='task-body']"  # подтверждено (konard)
APPLY_QUESTION_TEXT = "[data-qa='task-question']"  # подтверждено (konard), внутри task-body
APPLY_QUESTION_FORM_BODY = "form[name='vacancy_response'] [data-qa='task-body']"

# Второй (full-page) вариант textarea сопроводительного письма — нужен heuristic-фильтру,
# чтобы не принять cover-letter textarea за ответ на вопрос. konard: coverLetterTextareaForm.
APPLY_COVER_LETTER_TEXTAREA_FORM = "textarea[data-qa='vacancy-response-form-letter-input']"

# Heuristic-селекторы (НЕ data-qa, поэтому живут в apply/questions.py, а не в selector_groups):
# input[type='radio'], input[type='checkbox'], голый textarea — они используются
# detect_questions для fallback-эвристики, когда task-body переименован hh.ru.
