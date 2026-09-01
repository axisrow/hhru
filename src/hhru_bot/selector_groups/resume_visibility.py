"""Selectors for changing resume visibility and the per-employer stop-list.

Подтверждено живым DOM залогиненного пользователя 2026-08-29 (issue #746):
экран `/resume/edit/{resume_id}/visibility` и модалка "Кто видит"/"Кто не
видит" (поиск и выбор работодателя). Разведка велась read-only через
Claude-in-Chrome (`document.querySelectorAll('[data-qa]')`); переключение
radio-режима на клиенте подтверждено скриншотами до/после, боевого клика по
"Сохранить" НЕ выполнялось — состояние резюме на hh.ru не изменено. Дамп
находок: `data/logs/resume_visibility_probe_2026-08-29.md` (локально, папка
`data/` в `.gitignore`; полный текст воспроизведён в PR #746).

Ранее (issue #566) этот модуль был fail-closed заглушкой (`None`-плейсхолдеры)
именно потому, что экран не был подтверждён живым DOM. Значения ниже — первое
подтверждение; RESUME_VISIBILITY_MODE_CONTROL/RESUME_VISIBILITY_SAVE
переименованы в предметные константы, отражающие реальную структуру экрана
(пять независимых radio-labelʼов, а не один "control").
"""

from __future__ import annotations

from ._generated import selector as _selector

# Экран /resume/edit/{resume_id}/visibility: пять radio-режимов видимости.
# Клик делается по внешнему `data-qa="resume-visibility-card-access-type-*"`
# labelʼу, а не по вложенному <input type="radio"> — у всех пяти инпутов
# одинаковый value="on"/пустой name, различить их можно только по внешнему
# data-qa. Подтверждено живым DOM 2026-08-29.
RESUME_VISIBILITY_MODE_EVERYONE = _selector("resume_visibility.RESUME_VISIBILITY_MODE_EVERYONE")
RESUME_VISIBILITY_MODE_WHITELIST = _selector("resume_visibility.RESUME_VISIBILITY_MODE_WHITELIST")
RESUME_VISIBILITY_MODE_BLACKLIST = _selector("resume_visibility.RESUME_VISIBILITY_MODE_BLACKLIST")
RESUME_VISIBILITY_MODE_LINK_ONLY = _selector("resume_visibility.RESUME_VISIBILITY_MODE_LINK_ONLY")
RESUME_VISIBILITY_MODE_NO_ONE = _selector("resume_visibility.RESUME_VISIBILITY_MODE_NO_ONE")

# Внешний radio карточки режима — ПРЯМОЙ дочерний <input type="radio"> label'а.
# Подтверждено живым DOM 2026-09-01 (issue #901, read-only дамп): карточка
# содержит ДВА input[type="radio"] — внешний (прямой дочерний, из атрибутов
# только type) и внутренний Magritte (вложен в span[data-qa="radio-container"]
# внутри вложенного label[data-qa="cell"], class^="magritte-radio-input",
# readonly). Descendant-поиск "input[type='radio']" по карточке находит оба —
# прежняя строгая проверка count()==1 на нём fail-closed ломалась на легитимном
# DOM (команда останавливалась до Save, attempted=0). Оба input'а синхронны
# (React), поэтому читается/проверяется внешний: нативный клик по карточке-label
# активирует именно его (первый labelable-потомок внешнего label — подтверждено
# #901 живым кликом и на фикстуре tests/fixtures/resume_visibility_cards_901.html).
RESUME_VISIBILITY_MODE_RADIO = ":scope > input[type='radio']"

# Активаторы блока "Кто видит"/"Кто не видит" — условно отрендерены, только
# когда активен соответствующий режим (whitelist/blacklist). Клик открывает
# модалку поиска работодателя. Подтверждено живым DOM 2026-08-29.
RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST"
)
RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST"
)

# Модалка "Кто видит"/"Кто не видит" (поиск и выбор работодателя).
# Подтверждено живым DOM 2026-08-29: поиск "Сбер" вернул 7 разных найденных
# работодателей с разными employer_id, включая два похожих одноимённых —
# "СБЕР" (Москва) и "Сбер Банк" (Минск) — подтверждает предупреждение issue
# #746 про неоднозначность поиска по имени.
RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT"
)
# Строка результата поиска: data-qa содержит employer_id
# (`resume-employer-search-result-item-<id>`) — сам селектор point-in-time
# фиксирован через `_selector()` как CSS-префикс (`^=`); id подставляется
# вызывающим кодом через *_PREFIX-константу с f-string, по образцу
# apply_form.APPLY_RESUME_OPTION_PREFIX.
RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX"
)
RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX = (
    "resume-employer-search-result-item-"  # compatibility for fakes/tests
)
RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_NAME = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_NAME"
)
RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_CHECKBOX = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_CHECKBOX"
)
# Уже добавленная в список компания (до ввода текста в поиск); тот же паттерн
# CSS-префикс + отдельная *_DATA_QA_PREFIX-константа для f-string подстановки.
RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX"
)
RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DATA_QA_PREFIX = (
    "resume-editor-employer-list-item-"  # compatibility for fakes/tests
)
RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DELETE = _selector(
    "resume_visibility.RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DELETE"
)
# Один и тот же data-qa в обоих состояниях модалки: текст "Добавить" в режиме
# поиска, "Готово" в режиме списка (пустой поиск). Различать по тексту, не по
# селектору — подтверждено живым DOM 2026-08-29.
RESUME_VISIBILITY_MODAL_CONFIRM = _selector("resume_visibility.RESUME_VISIBILITY_MODAL_CONFIRM")
RESUME_VISIBILITY_MODAL_CLOSE = _selector("resume_visibility.RESUME_VISIBILITY_MODAL_CLOSE")

# Кнопки экрана видимости (вне модалки). Сохранение фиксирует одновременно
# выбранный режим и текущий список компаний одним запросом.
RESUME_VISIBILITY_SAVE = _selector("resume_visibility.RESUME_VISIBILITY_SAVE")
RESUME_VISIBILITY_CANCEL = _selector("resume_visibility.RESUME_VISIBILITY_CANCEL")
