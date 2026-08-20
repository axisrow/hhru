# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

CLI-инструмент на Playwright для поиска вакансий, откликов и поднятия резюме на hh.ru.
Запускается **только вручную** из терминала. Каждая команда логирует свои действия,
поддерживает `--dry-run` и ограничена дневными лимитами + случайными паузами, чтобы
не выглядеть как подозрительная автоматизация для анти-фрод системы hh.ru. При работе
с этим кодом сохраняй этот принцип: не добавляй фоновые/скрытые режимы и не убирай
троттлинг/лимиты.

**Интерфейс — только CLI, вывод только текст/ASCII-таблицы, без эмодзи.** Эталон набора
команд и формата вывода (сигнатуры, примеры, природа READ/WRITE) — `docs/cli-spec.md`
(дизайн-документ ишью #21). Новую команду добавляй по образцу из спеки, её вывод должен
соответствовать зафиксированным там форматам (префиксы `[OK]`/`[INFO]`/`[FAIL]`/`[DRY-RUN]`/`[skip]`,
ASCII-таблицы через `report._ascii_table`).

## Команды

```bash
# Установка
pip3 install -r requirements.txt
python3 -m playwright install chromium

# Настройка (вся папка data/ в .gitignore — коммитить не нужно)
./scripts/run.sh account create default

# Все команды запускаются через обёртку run.sh (вызывает установленный entry point `hhru`)
./scripts/run.sh --account default login                                    # ручной вход, сохраняет сессию
./scripts/run.sh --account default search --resume <id> --dry-run           # поиск без откликов
./scripts/run.sh --account default apply  --resume <id> --dry-run --limit 5 # план откликов
./scripts/run.sh --account default apply  --resume <id> --limit 5           # боевой отклик
./scripts/run.sh --account default bump   --resume <id>                     # поднять резюме (не чаще 1 раза в 4 часа)
./scripts/run.sh --account default run                                       # apply + bump для всех резюме

# Общие флаги: --headless, --verbose, --config <path>, --history <path>, --max-pages <n>
# --resume опционален — без него команда идёт по всем резюме из конфига
```

Тестов, линтера и системы сборки в проекте **нет** — это простой скрипт с `requirements.txt`
и запуском через установленный entry point `hhru` (editable install `pip install -e .`).

## Архитектура

Поток данных — цепочка ответственностей, не файлов: **сбор вакансий**
(`search_vacancies`, живёт в `search.py`) → **фильтрация/отсев** (`filter_candidates`
+ pre-LLM-префильтр #85 + таблица skipped #87, живёт в `search.py`/`history.py`) →
**планирование** (`run_apply_for_resume` — ранжирование/скоринг #74 + дневной лимит,
живёт в `commands/_common.py`) → **действие** (`apply_to_vacancy`/`bump_resume`, живёт в
`apply/`/`bump.py`) → **запись результата** (`history`, живёт в `history.py`). Каждая
ответственность — отдельный модуль с чистыми переиспользуемыми функциями (см. `apply/`
ниже); имена файлов — это «где живёт», а не суть шага. Все браузерные модули
используют **синхронный** Playwright API (`playwright.sync_api`).

### Ключевые архитектурные решения (неочевидны из кода одного файла)

1. **Поиск и фильтрация намеренно разделены.** `search_vacancies()` возвращает ВСЕ
   карточки без применения `exclude_employers`/`exclude_keywords` и без учёта истории.
   Отсев делает отдельная чистая функция `filter_candidates()` — так её причины отказа
   логируются и она тестируема без браузера. Не сливай эти два шага обратно.

2. **Дедупликация откликов не зависит от разметки hh.ru.** «Уже откликались» определяется
   по локальной SQLite-истории (`history.py`), а не по маркеру на странице (анонимному
   запросу hh.ru его не показывает). В `history.py` есть частичный UNIQUE-индекс по
   `(resume_id, vacancy_id)` для `action='apply'` со статусом `success`/`dry_run` —
   `has_applied()` опирается на него. **Важно:** `dry_run`-отклики тоже пишутся в историю
   и считаются «уже откликались», поэтому повторный `--dry-run` по той же вакансии её
   отсеет. `count_today()`/`last_action_at()` для лимитов считают `status='success'` и
   `status='uncertain'` (#176: действие могло выполниться при упавшем посреди клика
   Playwright — fail-closed, `uncertain` тоже дедуплицируется `has_applied()`).

3. **Двухуровневый троттлинг** в `throttle.py`:
   - Дневные лимиты (`daily_apply_limit`, `daily_bump_limit`) — проверяются перед каждым
     действием, при `dry_run` не применяются.
   - Кулдаун поднятия резюме: жёстко `BUMP_COOLDOWN = 4 часа` (`can_bump_now()`), сверх
     дневного лимита.
   - `throttle.wait()` — случайная пауза `min_delay..max_delay` секунд после каждого
     РЕАЛЬНОГО действия на hh.ru — клика поднятия/submit отклика (`BumpResult.acted`/
     `ApplyResult.acted`, #163). Ранние выходы до действия (плейсхолдер в конфиге,
     форма входа, hint «рано», dry-run) не ждут паузу и не пишут `failed` в actions:
     на hh.ru не осталось следа, пауза не от чего не защищает. Исключение Playwright
     в момент самого клика — это НЕ ранний выход: клик мог уйти, поэтому такой исход
     несёт `acted=True` + `uncertain=True` (статус `uncertain` в actions, #176).
   - **«Серая зона» после клика по кнопке отклика** (#207): клик по
     `VACANCY_APPLY_BUTTON` — начало зоны, где локальные таймауты (навигация на
     форму, отрисовка, success-сигнал) НЕ доказывают отсутствие отклика (боевые
     кейсы #199/МТС и #207/YADRO: отклик ушёл при `[FAIL]` в CLI). Все fail-исходы
     этой зоны финализируются через `pipeline._finalize_post_click_failure`:
     внешний источник истины — `/applicant/negotiations` (SSR `topicList[].vacancyId`
     + DOM-fallback, `apply/verify.py`, read-only). Вердикты: found → `success`
     (acted=True); not_found — вердикт сайта без изменений; список не прочитан →
     `uncertain` + `acted=True` (fail-closed, как #176). До клика по кнопке отклика
     проверка не применяется — там отклик физически невозможен.

4. **Форма отклика — двухшаговая навигация.** `VACANCY_APPLY_BUTTON` на странице вакансии
   это `<a href="/applicant/vacancy_response?...">`, а НЕ триггер модалки на той же
   странице. `apply.py`/`apply/steps.py::navigate_to_response_form` кликает и ждёт
   `page.wait_for_url(..., wait_until="commit")`, и только потом ищет поля формы.
   Не переписывай на «клик → искать модалку сразу». **Не `page.expect_navigation()`**
   (#179): у залогиненного пользователя переход на `/applicant/vacancy_response`
   рендерится как same-document/SPA-навигация (`history.pushState`) —
   `domcontentloaded` там не наступает, хотя `page.url` меняется и отклик реально
   уходит на hh.ru; `expect_navigation(wait_until="domcontentloaded")` в этом
   случае падает по таймауту даже при успешном клике. **`wait_until` обязателен
   явно**: `page.wait_for_url()` реализован через тот же `expect_navigation`
   внутри Playwright и без явного `wait_until` дефолтится на `"load"` — это
   строже, а не мягче `domcontentloaded`, и не решило бы проблему.
   `wait_until="commit"` — единственное значение, не ждущее lifecycle-событие
   документа вообще. И клик по кнопке отклика, и `wait_for_url` обёрнуты в
   try/except `PlaywrightError` (не только timeout) — ошибка здесь просто
   логируется и разбор вакансии прекращается через `return`, не крашит pipeline
   и не обрывает цикл apply по остальным вакансиям/резюме; дальнейшие шаги
   (submit-кнопка/detect_questions) сами решают, загрузилась ли форма.

   **`commit` не значит «отрисовано».** `wait_until="commit"` подтверждает только смену
   `page.url`, а не то, что React-SPA уже гидратировала целевой экран — сразу после
   `commit`/клика, запускающего асинхронный ре-рендер, `<body>` может быть ещё пустым.
   Строгая проверка через `count() != 1` (fail-closed паттерн этого проекта) в этот момент
   увидит `count=0` и ошибочно спишет случай на «селектор не подтверждён», хотя реальная
   причина — race condition. Поэтому после `wait_until="commit"` или клика, запускающего
   React-рендер, перед первой строгой проверкой видимости нужен явный
   `page.locator(sel).first.wait_for(state="visible", timeout=...)`. Паттерн уже используется
   как минимум в `resume_position.py`, `skills.py`, `about.py`, `bump.py`, `apply/steps.py`,
   `commands/clear_negotiations.py`, `create_resume.py`, `delete_resume.py` — каждый раз
   с собственным inline-таймаутом и комментарием под конкретный экран; общего хелпера в
   `browser.py` намеренно нет (таймауты законно разные, обёртка была бы тонким проходом
   без добавленного инварианта).

   После успешного submit hh.ru может показать отдельный попап «Резюме доставлено» с
   предложением выбрать статус поиска. Это штатный UI hh.ru, а не часть формы отклика.
   В разведке #178 он не повлиял на pipeline: `wait_success_confirmation()` уже получил
   позитивный сигнал, а следующий вызов `goto_hh()` на той же `page` заменяет текущую
   страницу вакансии. Поэтому попап не закрываем и не добавляем для него неподтверждённый
   селектор. `apply --dry-run` такой попап не создаёт; при следующем живом прогоне нужно
   отдельно проверить, мешает ли он навигации. Если начнёт мешать, сначала подтвердить
   селектор по живому DOM и добавить опциональное закрытие в `apply/steps.py`.

5. **Пустой результат требует подтверждения состояния страницы.** Если браузерный
   путь не смог подтвердить DOM из-за timeout, сетевой ошибки, анти-бота или дрейфа
   селектора, он не должен выдавать пустой список за достоверный. Для общего
   инварианта используется `browser.PageStateIndeterminate`; частные исключения
   (`NotAuthenticated`, `ResumeListIndeterminate`) сохраняют свои имена и сообщения,
   а флаговые результаты (`questions.indeterminate`, `probe.unreachable`) используют
   тот же словарь состояний `browser.PAGE_STATE`.

### `selectors.py` — статус проверки селекторов (критично)

Все CSS/`data-qa` селекторы hh.ru собраны в одном файле `selectors.py`; остальной код их
не дублирует. Их статус разный, и это отражено в комментариях файла:

- **Подтверждено curl-дампом** (без логина): селекторы страницы поиска (`/search/vacancy`)
  и страницы вакансии (`/vacancy/{id}`).
- **Подтверждено живым DOM / боевыми дампами** (2026-08-20, multi-resume аккаунт):
  `APPLY_RESUME_SELECT` (`resume-title` — свёрнутый ТРИГГЕР выбора резюме, не коллекция
  опций; клик по нему раскрывает `<label data-qa="magritte-select-option-{resume_id}"
  role="option">` — resume_id уже в самом `data-qa`, атрибута `href` на форме нет вовсе),
  `APPLY_COVER_LETTER_TEXTAREA`, `APPLY_SUBMIT_BUTTON`, `APPLY_COVER_LETTER_TOGGLE_POPUP`
  (`add-cover-letter` — тоггл письма МОДАЛКИ). `APPLY_COVER_LETTER_TOGGLE`
  (`vacancy-response-letter-toggle`) относится к ПОЛНОЙ форме и в модалке не совпадает
  (см. `apply_form.py` и разбор двух shape ниже).
- **НЕ подтверждено** (рендерится только залогиненному через JS): страница резюме с кнопкой
  поднятия, маркер «уже откликались».

Перед первым боевым `bump` (форма отклика уже сверена, см. выше): пройти `login`, открыть
страницу резюме в обычном браузере (F12 → Elements), сверить `data-qa` и поправить прямо в
`selectors.py`/`selector_groups/`. При отладке падений на этом шаге первый подозреваемый —
устаревший непроверенный селектор, а не логика модуля.

**hh.ru рендерит форму отклика в ДВУХ shape с похожим, но не идентичным DOM:** МОДАЛКА
на самой странице вакансии (`form#RESPONSE_MODAL_FORM_ID`, letter-toggle `add-cover-letter`,
textarea `vacancy-response-popup-form-letter-input`) и полноценная страница
`/applicant/vacancy_response` (letter-toggle `vacancy-response-letter-toggle`, textarea
`vacancy-response-form-letter-input`).

**Прежнее утверждение «бот всегда использует ВТОРУЮ» ОПРОВЕРГНУТО (2026-08-20).** Во всех
дампах `data/logs/apply_*` начиная с 2026-08-16 `<link rel="canonical">` остаётся
`/vacancy/{id}` — навигации не происходит, фактически работает МОДАЛКА. Кнопка отклика
по-прежнему `<a href="/applicant/vacancy_response…">`, но hh.ru перехватывает клик JS.
Надёжный маркер shape в дампе — `form="RESPONSE_MODAL_FORM_ID"`; `add-cover-letter`
маркером НЕ является (его нет в DOM, когда hh.ru отрендерил textarea уже развёрнутой).

Цена ошибки была измерена: селектор letter-toggle полной формы в модалке не совпадает
ни разу, поэтому письмо молча терялось — по SSR `topicList[].hasResponseLetter` из 18
откликов аккаунта с письмом ушли 2, без письма 16. Теперь `fill_response_form` адресует
оба shape через `Locator.or_`, а отсутствие textarea — **fail-closed отказ до submit**
(отклик без письма не отправляем). Full-page селекторы НЕ удалять: оба shape наблюдались
в дампах одного дня (08-16).

**Панель выбора резюме не закрывается сама — её надо закрыть явно.** Боевой случай
2026-08-20 (`136190065`/`136190066`): клик по опции резюме выбирает её
(`aria-selected="true"`), но всплывающая панель `[data-qa='drop-base']` остаётся
открытой. Она спозиционирована абсолютно (`z-index: 2250`, высота ~281px) и физически
перекрывает submit в футере модалки → клик ретраил 30 с
(`subtree intercepts pointer events`) и падал в `SubmitClickUncertain` — ложная
«неопределённость» при НЕотправленном отклике.

Закрывается повторным кликом по триггеру `APPLY_RESUME_SELECT` (стандартный toggle
селекта; проверено probe-прогоном на живой форме). Escape не используем: в модалке он
может закрыть всю форму отклика.

Ждать надо скрытия **самой панели**, а не опции: опции внутри неё — постоянно видимые
карточки, они остаются `visible`, пока панель открыта, поэтому ожидание скрытия опции
не выполнилось бы никогда (проверено: такой вариант отказывал в 100% случаев).
`[data-qa='drop-base']` — single-match, подтверждён живыми probe-дампами: 0 элементов
до клика по триггеру, ровно 1 после.

### Граница браузерных действий

| Что | Как | Почему |
|---|---|---|
| Действие (клик, отправка, публикация, отзыв) | Только UI-клик в браузере | Как человек; не зависит от внутреннего endpoint hh.ru |
| Чтение состояния | DOM или SSR уже открытой страницы | Это чтение, а не скрытый запрос |
| Прямой HTTP | Запрещён (`page.request.*`) | Внутренний API хрупок и может молча изменить смысл операции |

Это не вопрос стиля: тест-страж проверяет отсутствие таких вызовов в `src/hhru_bot/`.
Для необратимого отзыва `clear-negotiations` кнопка и привязка к нужному `topic` должны
быть однозначно подтверждены до клика, а успех — только позитивным маркером интерфейса.
Если селектор или подтверждение не найдено, команда сообщает `[FAIL]` и не продолжает.

**Отдельно:** `apply/questions.py::_form_scope()` (#95) допускает, что кнопка отправки
формы отклика обёрнута в семантический `<form>`-тег (использует
`xpath=ancestor::form[1]` для скоупинга heuristic-детекции вопросов) — это допущение
о структуре DOM, а не просто про `data-qa`. **Подтверждено живым дампом 2026-08-20**
(`data/logs/apply_136190065_navigation_timeout.html`): модалка содержит
`<form name="vacancy_response" id="RESPONSE_MODAL_FORM_ID" method="POST">`, то есть
обёртка реально существует и `APPLY_QUESTION_FORM_BODY` (`form[name='vacancy_response']`)
тоже валиден. Прежнее предупреждение «НЕ подтверждено живым дампом» снято.
Если разметка снова уедет, симптом будет прежним: `detect_questions()` начнёт
систематически возвращать `indeterminate`, и pipeline будет `fail`-ить каждый
non-dry-run `apply` — диагностировать по `[WARN indeterminate]` в логе `probe`
на форме без вопросов.

### Структура пакетов под распараллеливание (Wave 0)

Код разложен так, чтобы 10 параллельных воркеров не конфликтовали в общих файлах.
Каждое feature-ишью владеет **своим файлом/шагом**, а не общим оркестратором.
При правках соблюдай эту структуру — не сваливай шаги обратно в монолит.

- **`commands/`** — каждая команда = отдельный модуль с `register(subparsers)`.
  `cli.py` авторегистрирует их через `pkgutil.iter_modules` — добавление команды = новый
  файл `commands/<name>.py`, **0 правок `cli.py`**. Общие аргументы/контекст — в
  `commands/_common.py` (`add_common_args`, `run_apply_for_resume`).
- **`apply/`** — пакет оркестрации отклика. Каждая **ответственность = отдельный модуль
  с чистыми переиспользуемыми функциями** (максимальное переиспользование — принцип проекта):
  `dedup.py` (#3 «уже откликались»), `steps.py` (#6 навигация/wait'ы формы),
  `success.py` (#7 подтверждение успеха), `probe.py` (#8 диагностический снимок),
  `letter.py` (#17 письмо), `questions.py` (#95 детект тест-вопросов) и т.д.
  `pipeline.py` — **тонкая связка**: последовательность, которая **вызывает** эти функции
  в нужных точках. **Трогать `pipeline.py` нормально** при добавлении вызова новой
  переиспользуемой функции (это его работа — оркестровать). Новая фича = новый модуль с
  чистой функцией + строка-вызов в `pipeline.py`, НЕ правка внутренностей чужого шага
  (что нарушает cohesion и переиспользование). Хуки в `ApplyContext` (`ctx.probe` #8,
  `ctx.letter_provider` #17) — для опциональных/injectable шагов (вкл/выкл через конфиг),
  не для обязательных точек между шагами (те — прямой вызов в `pipeline.py`).
  Селекторы статуса отклика живут **у владельцев** (dedup/success), а не в `selector_groups/`.
- **`config_sections/`** — реестр парсеров секций `config.yaml` (`@register("<name>")`).
  `load_config` делегирует resume-подсекции реестру. Новая секция = новый файл
  `config_sections/<name>.py`, `ResumeConfig` не трогается (для scoring/ai_profile там
  пред-добавлены нейтральные `Optional`-поля `= None`).
- **Схема SQLite — одна константа `SCHEMA` в `history.py`** (а не пакет `migrations/`):
  `CREATE TABLE IF NOT EXISTS` для всех таблиц (actions, responses, manual_offers),
  применяется `_init_schema()` через `conn.executescript(SCHEMA)` в `History.__init__`.
  Системы миграций в проекте нет намеренно (оверинжиниринг для такого размера) — при
  сильных изменениях схемы базу пересоздают заново (данных мало). Не заводи DDL в `.sql`
  и не вводи таблицу `schema_migrations`; новые таблицы дописывай в `SCHEMA`.
- **`selector_groups/`** — селекторы по страницам. `selectors.py` — тонкий shim
  (`sel.VACANCY_CARD`...) для обратной совместимости; новый код импортирует из группы.
- **`tests/`** — characterization-тесты на чистую логику (без браузера): `filter_candidates`,
  `build_search_url`, `render_cover_letter`, `load_config`, `_init_schema` идемпотентность
  (создание таблиц + повторный запуск через `IF NOT EXISTS`),
  `register_commands` + `--help`. Покрывают реструктуризацию от регрессий.

Конвенция репортов: **один report-топик на файл** (напр. `report.py` vs `report_funnel.py`),
чтобы параллельные ишью статистики/воронки не конфликтовали.

## Конфигурация и данные

- `config.py` парсит `config.yaml` в датаклассы (`AppConfig` → `ResumeConfig` → `SearchFilters`)
  с явной валидацией через `_require()`; `ResumeConfig.resume_id` вычисляется из хвоста
  `resume_url`. Ошибки конфига бросают `ConfigError`, а `load_config_or_exit()` печатает их
  и делает `sys.exit(1)`.
- Сопроводительное письмо: `cover_letter` резюме, иначе `cover_letter_default`. Плейсхолдеры
  `{vacancy_title}` и `{company_name}` подставляются в `render_cover_letter()` (apply.py).
- **Все изменяемые данные живут в `data/`, вся папка целиком в `.gitignore` одной строкой**
  (#133). Новый изменяемый артефакт кладётся туда же и правки `.gitignore` не требует —
  точечные правила («забыли строку → секрет в коммите») сознательно убраны.

```
data/                      # всё изменяемое, целиком в .gitignore
  accounts/<name>/
    config.yaml             # настройки аккаунта (создаётся account create)
    history.db              # история аккаунта
  config.yaml              # личные настройки (шаблон — config/config.example.yaml)
  history.db               # SQLite: история откликов, вакансии, ответы
  storage_state/
    hh_session.json        # сессия hh.ru (секрет)
  logs/
    hhru_bot.log           # лог CLI (+ дублируется в консоль)
    probe_*.html / .png    # дампы probe (#8)
    scheduled.log          # вывод scripts/scheduled_run.sh

config/
  config.example.yaml      # версионируемый шаблон формата, НЕ в игноре
```

- Пути **относительно cwd** (точки запуска), а не пакета: после `pip install` пакет уезжает
  в site-packages, и привязка к расположению кода ломала бы поиск конфига. Дефолты —
  `cli.DEFAULT_CONFIG_PATH` (`data/config.yaml`), `cli.DEFAULT_HISTORY_PATH`
  (`data/history.db`), `logging_setup.LOG_DIR` (`data/logs`, оттуда же наследует
  `apply/probe.PROBE_LOG_DIR`).
- **Исключение — `account.storage_state_file`:** резолвится относительно **директории файла
  конфига** (`parse_account`), а не cwd, чтобы `--config /abs/.../config.yaml` из чужой
  директории писал сессию рядом с конфигом. Shipped-значение `storage_state/hh_session.json`
  → `data/storage_state/` → покрыто `data/`. Значение подконтрольно пользователю и может
  увести секрет за пределы `data/` (`../…`), поэтому в `.gitignore` сохранён defence-in-depth
  catch-all `**/storage_state/*.json`; инвариант закреплён тестами в `tests/test_config.py`.
- Обратной совместимости со старой раскладкой (`config/config.yaml`, `logs/`) нет намеренно:
  #133 — breaking change, детекта старых путей и fallback'ов в коде быть не должно.

## Тестирование и TDD

- `pytest` по умолчанию запускает только безопасные тесты: `live_read`,
  `live_write` и `live_write_danger` исключены из сбора.
- Каждый тестовый файл имеет ровно один маркер: `unit`, `integration`, `smoke`,
  `e2e`, `live_read`, `live_write` или `live_write_danger`.
- Тесты `live_read` читают живой hh.ru. `live_write` обратимо изменяют только
  свой аккаунт и запускаются через `./scripts/live_test_safe.sh`. `live_write_danger`
  необратимы или видимы посторонним и запускаются через `./scripts/live_test.sh`,
  который запрашивает отдельное подтверждение harness на каждый вызов.
- По границе команд: `copy-resume` относится к `live_write`; `publish-resume`,
  `apply`, `bump`, `run`, `reply-employers` и `clear-negotiations` — к
  `live_write_danger`.
- Не создавай новые live-тесты для проверки обычной логики: используй моки и
  HTML-фикстуры. Перед изменением тестовой инфраструктуры проверь
  `pytest --collect-only -q` и убедись, что live-категории не собраны.
