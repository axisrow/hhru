# Дифф-аудит референсов hh.ru по селекторам и wait-дисциплине

Дата аудита: 2026-08-19. Референсы прочитаны **по исходному коду**, не по README.

## Зачем этот аудит и чем он отличается от #335

Обычный набор тестов зелёный (1287 passed, Ruff clean), но браузерные пути покрыты
моками, поэтому CI не видит DOM drift и SPA-гонки. Предыдущий аудит #335 сравнивал
**control-flow** одного референса и дал ноль подтверждённых багов.

Здесь метод другой в двух отношениях, и именно это дало находки:

1. Сравниваются **селекторы и wait-дисциплина**, а не порядок шагов.
2. Каждое расхождение сверяется с **нашими живыми данными** — логами, SQLite-историей
   и probe-дампом. Это фильтр, который отделяет реальную находку от чужого мусора:
   так подтвердилась A1 и так была **отклонена** гипотеза про `_form_scope()` (A2).

Без шага 2 аудит выдал бы «у них 150 селекторов, которых нет у нас» — список без
диагностической ценности.

**Отличие от #334** (аудит копируемости модулей): там вопрос «что можно буквально
скопировать» и ответ упирается в лицензии и совместимость стека. Здесь вопрос другой —
«где чужой код показывает, что наш неправ», и копирование не предполагается вовсе.

## Корпус (13 репозиториев, закреплены по коммитам)

Клоны лежат в `/private/tmp` и **волатильны**; воспроизводить по коммиту.

| Репозиторий | Коммит | Лицензия | Стек | Уник. `data-qa` |
|---|---|---|---|---|
| `Vadtop/hh-mcp-server` | `91b7eea9` | MIT | Python, Playwright | 82 |
| `YAMAKAYAMACO/hh-autoresponder` | `bfb46696` | MIT | Python, Playwright | 47 |
| `konard/hh-job-application-automation` | `58ca4401` | Unlicense | Bun, Playwright/Puppeteer | 38 |
| `AgentShekel/hh-bot` | `3a12421e` | NOASSERTION | Python, Playwright | 37 |
| `semernyakov/hh-auto-apply` | `8bec8cc9` | MIT | Python, Playwright | 30 |
| `tgeruzov/hh-auto-responder` | `3e30d854` | MIT | Tampermonkey userscript | 27 |
| `RumyantsevQa/hh-ai-auto-apply-assistant` | `b1136448` | MIT | Python | 22 |
| `fikstt2/hh-ai-agent` | `c675c166` | MIT | Python, Playwright | 18 |
| `kavotavochavo1-ctrl/hh-ai-job-bot` | `3ded46fd` | MIT | Python, Playwright | 16 |
| `Steev193/hh-ru-apply` | `7a56af1e` | MIT | Node.js, Playwright | 15 |
| `lil-zon/hh-auto-apply` | `19dd68c2` | **нет LICENSE** | Python | 14 |
| `Vlad9572324/hh.ru-clicker` | `bd3d5262` | **нет LICENSE** | Python, HTTP | 5 (+ карта роутов) |
| `s3rgeym/hh-applicant-tool` | `63210bcc` | **нет LICENSE** | Python, REST API | 8 |

Всего у референсов 178 уникальных `data-qa`, у нас 125.

**Лицензии** (сверено через GitHub API на дату аудита). Три репозитория —
`s3rgeym/hh-applicant-tool`, `Vlad9572324/hh.ru-clicker`, `lil-zon/hh-auto-apply` —
**не имеют LICENSE вообще** (`license: null`, то есть all rights reserved);
у `AgentShekel/hh-bot` GitHub возвращает `NOASSERTION`. Это уточняет более раннее
указание «Non-Commercial» для `s3rgeym`: текущим репозиторием оно не подтверждается
(см. также аудит копируемости #334).

**Для этой работы ограничение не является блокирующим:** мы читаем чужой код ради
**поведения** и переносим строки селекторов как **факты о DOM hh.ru** —
`data-qa`-атрибуты являются разметкой hh.ru, а не авторским выражением референса.
**Исходный код ни из одного репозитория не копируется**, в том числе из
MIT/Unlicense-репозиториев. Если из какой-то находки вырастет буквальный перенос кода,
лицензию нужно перепроверить отдельно по критериям #334, а из репозиториев без
LICENSE не копировать вовсе.

**Оговорка о качестве источников.** Наличие селектора в референсе ≠ подтверждение.
Часть проектов написана слабо (голый `count() > 0`, отсутствие hydration-ожиданий,
широкие `except`) и могла никогда не отрабатывать на живом hh.ru. Поэтому вес имеет
**совпадение независимых источников** и, решающе, **наши собственные живые данные**.

## Использованные живые данные

- `data/logs/hhru_bot.log` (1305 строк, 27.07–19.08)
- `data/history.db` (таблица `actions`)
- `data/logs/probe_136230351_questions_live.html` — дамп **живой формы отклика**
- `data/logs/drill212_20260816_205447.txt` — протокол разбора инцидента

## Шкала доказательности

| Уровень | Значение |
|---|---|
| `PROVEN` | подтверждено нашим живым логом / дампом / инцидентом |
| `CORROBORATED` | несколько независимых референсов согласны; живьём не проверено |
| `INTERNAL` | нестыковка внутри нашего кода, референс не нужен |
| `HYPOTHESIS` | правдоподобно, доказательств отказа нет |

---

## A1 `PROVEN` — apply отправляет отклик с ЧУЖИМ резюме

Самая серьёзная находка: дефект корректности с доказанным живым инцидентом.

**Инцидент.** `data/logs/drill212_20260816_205447.txt`:

```
incident 135170581: found | topic=5503922709, resumeId=96223331
  — сайт приложил другое резюме аккаунта (конфиг: 284561395)
result: PROVEN
```

**Причинная цепочка** (проверена по коду и по нашему же дампу):

1. `selector_groups/apply_form.py:12` — `APPLY_RESUME_SELECT = "[data-qa='resume-topic-title']"`.
   `apply/steps.py:305` сам признаёт селектор «приблизительный (не подтверждён)».
2. В живом дампе формы отклика `resume-topic-title` встречается **0 раз**.
   Присутствуют Magritte-обёртки `data-qa="resume-title"` и `data-qa="resume-detail"`.
3. `apply/steps.py:239-243` — `wait_for(state="attached")` по несуществующему
   селектору всегда падает в `PlaywrightTimeoutError` → `options_count = 0` (`:246`).
4. Комментарий `steps.py:237-238` трактует это как «выбора нет (одно резюме),
   submit разрешён». Условие `options_count > 0` на `:266` ложно, значит
   `_select_resume_in_form` **никогда не вызывается**.
5. hh.ru прикладывает своё дефолтное резюме.

**Суть дефекта — ложная дихотомия на `:246`.** Таймаут означает «селектор не найден»,
а не «резюме одно». Для *неподтверждённого* селектора эти два случая неразличимы,
и fail-closed-инвариант #33 молча отключается ровно там, где он нужен. Ветка
`PlaywrightError` (`:247-255`) написана аккуратно и fail-closed — но она не
срабатывает, потому что таймаут по отсутствующему селектору легитимен.

**Что дают референсы.** `AgentShekel/hh-bot` `parser/hh_client.py:1050-1155`:
карточки выбора несут `data-magritte-select-option="<resume hash>"` — привязка по
хэшу, устойчивая к переименованию резюме; фолбэк на заголовок, затем на legacy
`[data-qa='resume-title']`; верификация схлопнутого заголовка по собственному живому
заголовку выбранной карточки; **fail-closed отказ** вместо отправки с дефолтным
резюме. Другие наименования того же контрола:

| Референс | Селектор |
|---|---|
| `AgentShekel` `hh_client.py:1117,1126` | `[data-magritte-select-option="<hash>"]` |
| `YAMAKAYAMACO` `app/parsers/hh_playwright.py:873-878` | `vacancy-response-popup-form-resume-dropdown` / `-resume-option` |
| `RumyantsevQa` `app/auto_apply.py:304`, `kavotavochavo1` `hh_playwright.py:10` | `[data-qa^="magritte-select-option-"]` |
| `Steev193` `lib/hh-chat-selectors.mjs:70` | `[data-qa="resume-select-item"]` |

Пять независимых проектов, ни один не использует `resume-topic-title`.

### Разбор якорей: какой из пяти кандидатов верен

Сверка **каждого** кандидата с нашим живым дампом:

| Кандидат | Источник | Вхождений в дампе |
|---|---|---|
| `resume-topic-title` | **наш текущий** | **0** |
| `data-magritte-select-option` | AgentShekel | 0 |
| `[data-qa^="magritte-select-option-"]` | RumyantsevQa, kavotavochavo1 | 0 |
| `vacancy-response-popup-form-resume-dropdown` / `-option` | YAMAKAYAMACO | 0 |
| `resume-select-item` | Steev193 | 0 |
| **`resume-title`** | AgentShekel (fallback) | **1** |
| **`resume-detail`** | AgentShekel, RumyantsevQa | **1** |

Полный список `data-qa` внутри `<form name="vacancy_response">`: `cell`,
`cell-left-side`, `cell-text-content`, `employer-asking-for-test`, `resume-detail`,
`resume-title`, `task-body`, `task-question`, `test-description`,
`textarea-native-wrapper`, `textarea-wrapper`, `title`,
`vacancy-response-letter-toggle`, `vacancy-response-submit-popup`.
Контрола `select`/`dropdown` с собственным `data-qa` в форме нет.

Разбор дерева даёт цепочку предков `resume-title`:

```
<div role="button" tabindex="0" aria-disabled="false">   <-- НЕТ data-qa
  └ <div data-qa="cell">
      └ <div data-qa="cell-left-side">
          └ <div data-qa="resume-title">
          └ <div data-qa="resume-detail">
```

Карточка резюме **обёрнута в интерактивный `role="button"` без `data-qa`** — это
кликабельный пикер, ровно как описывает AgentShekel (`hh_client.py:1050-1155`).

**Вывод:** `resume-title` — единственный якорь, подтверждённый нашим живым DOM.
Но прямая подстановка вместо `resume-topic-title` **недостаточна и сломает семантику**:
`resume-title` это **схлопнутый заголовок**, а не коллекция опций, поэтому
`count()` (`apply/steps.py:257`) вернёт 1 и при одном резюме, и при пяти.

**Оговорка.** Дамп снят на форме с **одним** резюме: он доказывает разметку
схлопнутого состояния, но разметку **развёрнутого** списка опций мы не видели ни разу.
Якорь опции обязан быть подтверждён probe на аккаунте с несколькими резюме.
Порядок работ для исполнителя зафиксирован в #340: сначала fail-closed на моках
(не отправлять при неподтверждённом контроле), затем probe, затем сам выбор.

---

## A2 `PROVEN` — сообщение об ошибке ложное; реальная причина в проглоченном сбое

**Логи:** 6× `не удалось определить границы формы отклика (нет <form>-предка у submit)`
(16.08), и **каждый раз** этому предшествует
`Форма отклика не отрисовалась (Timeout 10000ms exceeded)`.

**Текст ошибки опровергается нашим же дампом.** В `probe_136230351_questions_live.html`
присутствует `<form name="vacancy_response" id="RESPONSE_MODAL_FORM_ID" method="POST">`
(байты 712945–731212), а `vacancy-response-submit-popup` лежит на байте 730524 —
**внутри** этих границ.

**Вывод: допущение CLAUDE.md про `<form>`-обёртку (#95) ВЕРНО, `_form_scope()`
править не нужно.** Это отклонённая гипотеза, и её стоит зафиксировать: предупреждение
в CLAUDE.md («первый подозреваемый — `_form_scope()`») три дня уводило отладку в сторону.

**Реальный механизм:**

| Шаг | Файл | Что происходит |
|---|---|---|
| 1 | `apply/steps.py:194-196` | `navigate_to_response_form` **проглатывает** таймаут рендера: пишет `warning` и возвращает управление штатно |
| 2 | `apply/pipeline.py:269` | `detect_questions` вызывается так, будто форма отрисовалась |
| 3 | `apply/questions.py:52` | `_QUESTION_WAIT_TIMEOUT_MS = 1_500` — модалке, только что не уложившейся в 10 с, даётся 1.5 с |

Дефект — в **распространении сбоя**, а не в величине таймаута: поднятие 1500 мс
заставит падать медленнее, а не успешнее.

**Что даёт референс.** `konard` трактует «модалка не появилась» как **точку ветвления
с различимыми терминальными состояниями**, а не как warning
(`src/vacancies.mjs:879-918`): `direct_application`, `modal_timeout`,
`modal_not_appeared`, `limit_error`. Он же якорит форму строго —
`src/hh-selectors.mjs:22`: `form#RESPONSE_MODAL_FORM_ID[name="vacancy_response"]`.

**Уточнение, снимающее ложный вывод:** наш `questions.py:17` уже знает, что submit
может лежать **вне** `<form>` и иметь атрибут `form="RESPONSE_MODAL_FORM_ID"`, и
`_form_scope()` этот случай обрабатывает (`questions.py:139-145`). То есть «якорить по
ID» — не находка; находка — именно проглоченный сбой и ложная формулировка причины.

---

## A3 `CORROBORATED` — #337: маршрут верный, неверно допущение «клик ведёт на роут»

- Наше: `resume_position.py:238-242` кликает `[data-qa='edit-position-button']`, затем
  `wait_for_url("**/resume/edit/{id}/position", wait_until="commit")` **без явного
  таймаута** → потолок 90 с (`browser.py:127`, `:211`). По #337 у якоря нет `href`,
  URL не меняется → зависание на 90 с.
- `Vlad9572324/hh.ru-clicker` `app/hh_resume.py:843`, `app/routes/accounts.py:958,1054`
  — обращается с `/resume/edit/{hash}/position` как с самостоятельным URL (в том числе
  отдаёт его пользователю как `edit_url`); флоу называется
  `hhtmSource=resume_partial_edit`.
- `Vadtop/hh-mcp-server` `src/services/resume.py:94` кнопку не кликает вовсе:
  `goto("https://hh.ru/resume/{id}/edit")`.

**Референсы противоречат друг другу**, и это важно: у Vadtop URL **другой**
(`/resume/{id}/edit` против нашего `/resume/edit/{id}/position`), а его код слаб как
свидетельство (голый `count() > 0`, `random_delay` вместо позитивного маркера).
`hh.ru-clicker` — свидетель лучше, но `Referer` — заголовок, который он сам
выставляет, а не доказательство навигации.

**Поэтому направление фикса здесь не фиксируется как решённое.** Разрешающая проверка
(read-only, на авторизованном черновике `35661ef3…`): рендерится ли
`[data-qa='resume-edit-position-form']` при **прямом** `goto` на
`/resume/edit/{id}/position`.

Та же форма дефекта — `skills.py:144`. У `about.py:127-129` `wait_for_url` нет вовсе,
и её живой таймаут 19.08 13:08 (`resume-editor-about`, 30 с) — свежайший сбой в логах.

---

## A4 `CORROBORATED` — блокирующие модалки между кликом отклика и формой

| Что | Референс | У нас |
|---|---|---|
| `[data-qa="relocation-warning-confirm"]` — подтверждение для вакансий из другого города, **после** клика отклика и **до** формы | `AgentShekel` `hh_client.py:1238`; `tgeruzov` `script.js:79` | нет |
| `vacancy-response-similar-vacancies-close`, `button:has-text("Не сейчас")` | `AgentShekel` `hh_client.py:1028-1048` | нет |
| `vacancy-response-link-advertising-cancel` + `magritte-alert` — отклик на **внешнем сайте** работодателя | `konard` `hh-selectors.mjs:40-41`, `vacancies.mjs:891` | нет |
| `[data-qa-popup-error-code="negotiations-limit-exceeded"]` — лимит откликов hh.ru исчерпан | `konard` `hh-selectors.mjs:36`, `vacancies.mjs:910-915` | нет |
| `response-reject-warning`, `vacancy-response-error-notification` | `tgeruzov` `script.js:80` | нет |

`relocation-warning-confirm` подтверждён **двумя независимыми** референсами.

`AgentShekel` отдельно предупреждает (`hh_client.py:1031-1034`): нельзя кликать generic
«Закрыть» / `bloko-modal-close` — они совпадают с кнопкой закрытия самой формы отклика
и закроют её.

Необработанная модалка → форма не рендерится → ровно каскад A2 → «серая зона» #207 →
`uncertain`. Правдоподобный вклад в отказы 16 августа.

Отдельно `negotiations-limit-exceeded`: у нас есть собственные дневные лимиты, но
**лимит самого hh.ru** мы не распознаём. Он выглядел бы как серия необъяснимых
отказов — тот же сценарий, что описан для капчи в #84 (дневной лимит считает
`success`, а серия `failed` его не тормозит).

---

## A5 `INTERNAL` — дрейф wait-дисциплины (референсы не нужны)

`apply/steps.py:136-141` формулирует правило проекта: у залогиненного пользователя
навигация hh.ru — same-document `pushState`, поэтому безопасен только
`wait_until="commit"`; `"load"` строже и может не наступить никогда.

**3 из 9** вызовов `wait_for_url` это правило нарушают (нет `wait_until` → дефолт `"load"`):

| Место | Роль | Потолок | Замечание |
|---|---|---|---|
| `resume_education.py:220` | **единственный** сигнал успеха сохранения | 90 с | **высшая severity**; вдобавок не привязан к identity — подходит любой `/resume/*` |
| `resume_education.py:188` | ожидание входа в форму | 90 с | |
| `copy_resume.py:682` | навигация на новое резюме | 30 с | смягчено: таймаут ловится и сверяется со списком |

Кроме того, `experience.py:208-210,259-260` и `resume_sections.py:176-177` кликают и
**сразу** читают форму, без hydration-ожидания — та самая гонка, которую #328 закрыл в
соседних модулях.

Эталон правильной реализации **внутри проекта** — `create_resume.py` и
`delete_resume.py`: `wait_until="commit"` плюс явный
`wait_for(state="visible", timeout=15000)`. Косвенное подтверждение: по истории
`create_resume`/`delete_resume` падают с `совпадений: 0` и 15-секундными таймаутами,
то есть отказывают **предсказуемо и быстро**, а не зависают на 90 с.

---

## A6 `HYPOTHESIS` — профилактика (доказанных отказов нет)

- **Captcha-halt** (идея №1 из #84, P1, не реализована). В наших логах `captcha` —
  0 совпадений, то есть отказа пока не было. Реализация у `tgeruzov`
  (`script.js:2223-2270`): iframe `recaptcha|hcaptcha`, `[data-qa*="captcha" i]`,
  `/captcha|/checkpoint|/nocaptcha` в pathname → `haltForCaptcha()` останавливает
  прогон. Селекторы `Vadtop`: `captcha`, `account-captcha-input`,
  `account-captcha-picture`. Это **остановка**, а не обход.
- **Auth/login-семейство** (контекст для #332): `account-login-*`,
  `login-input-username/password`, `expand-login-by-password`, `get-code-button`,
  `code-input`, `magritte-pincode-input-field`, `cookie-accept`. У нас `selectors.py:52`
  явно не имеет константы поля одноразового кода.
  **Оговорка:** сбои `login-code` 19.08 наблюдались в worktree `hhru-163` со смешанной
  установкой — до воспроизведения в основном чекауте выводов не делаем.
- **`task-body`/`task-question`** — цитата **проверена**: `konard` `src/qa.mjs:20,22`
  и `src/hh-selectors.mjs:44` действительно их используют, и оба присутствуют в нашем
  живом дампе. Селектор верен. Но референсы используют и другое семейство —
  `vacancy-response-question` / `-question-text` (`semernyakov`
  `auto_apply_template.py:557-558`; `kavotavochavo1` `hh_playwright.py:22` матчит
  подстрокой `[data-qa*="vacancy-response-question"]`), плюс `vacancy-questions`,
  `applicant-questions`, `response-questions`. Кандидат на union, а не замену.

---

## Что у нас правее референсов

Чтобы дифф не читался как «у всех лучше»:

1. Разделение `success` / `failed` / `uncertain` вокруг необратимого клика и
   post-click reconciliation через `/applicant/negotiations` (#207) — ни один
   референс не имеет эквивалента. `semernyakov` считает клик успехом после `sleep`.
2. Дедупликация по локальной истории, а не по разметке страницы.
3. `dry-run`, дневные лимиты, троттлинг, durable history для `uncertain`.
4. Отказ отвечать на тест-вопросы за кандидата (#95) — сознательное решение, а не пробел.

Наш submit-селектор `vacancy-response-submit-popup` встречается в референсах 13 раз —
подтверждён консенсусом.

## Итог и приоритеты

| Находка | Уровень | Приоритет | Куда |
|---|---|---|---|
| A1 — отклик с чужим резюме | `PROVEN` | **highest** | #340 |
| A2 — проглоченный сбой рендера + ложный текст ошибки | `PROVEN` | **high** | #341 |
| A4 — необработанные блокирующие модалки и лимит hh.ru | `CORROBORATED` | medium | #342 |
| A5 — `wait_until`-дрейф | `INTERNAL` | medium | #343 |
| A3 — #337, проверка прямого `goto` | `CORROBORATED` | — | комментарий в #337 |
| A6 — captcha | `HYPOTHESIS` | medium | #344 |
| A6 — auth-селекторы | `HYPOTHESIS` | — | комментарий в #332 |

`priority/critical` в репозитории нет, поэтому A1 заведена как `priority/high` +
`needs-live-account`; по сути это самая приоритетная находка аудита.

Правок кода в рамках этого аудита не выполняется: каждая находка чинится отдельным воркером
отдельным PR.

## Воспроизводимость находок без браузера

```
# A1: селектора нет в живом дампе формы отклика
grep -c 'resume-topic-title' data/logs/probe_136230351_questions_live.html   # -> 0

# A2: <form> в дампе есть, submit внутри его границ
grep -o 'form name="vacancy_response"' data/logs/probe_136230351_questions_live.html

# A5: вызовы wait_for_url без wait_until
grep -rn "wait_for_url" src/hhru_bot/
```
