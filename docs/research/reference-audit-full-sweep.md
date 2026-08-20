# Аудит #4: полный проход по 13 референсам + вычёс `src/hhru_bot`

Дата аудита: 2026-08-20. Референсы прочитаны по исходному коду, не по README.
Каждая находка снабжена **воспроизводящим тестом** — `tests/test_audit_launch_and_navigation.py`.
hh.ru в ходе аудита **не открывался**, записей не выполнялось, ничего не создавалось и не удалялось.

## Чем этот аудит отличается от трёх предыдущих

| Аудит | Метод | Результат |
|---|---|---|
| `issue-335-semernyakov-diff-audit.md` | сравнение control-flow с одним референсом | **0** багов |
| `issue-84-references.md` | идеи по 5 референсам | бэклог идей |
| `reference-selector-diff-audit.md` | селекторы + wait-дисциплина + сверка с живыми данными | 6 находок |
| **этот** | референсы как **каталог шаблонов дефектов** + вычёс всего `src/hhru_bot` + тест на каждую находку | **5 находок** |

Ключевой сдвиг метода. Предыдущий аудит искал **расхождения в разметке** («у них селектор X,
у нас Y»). Этот берёт из референсов **классы ошибок**, которые их код показывает как
возможные, и прогоняет каждый класс по **всему** нашему коду, а не по модулю-источнику.
Именно поэтому находки лежат в модулях, которых нет ни в одном референсе
(`clear_negotiations`, `about`, `throttle`): дефект найден не сравнением, а инвариантом.

**Все 4 находки прошлого аудита остались закрытыми** — проверено на `HEAD == origin/main ==
b68f7e8`: #340/#341/#343/#344 в коде. Дублей нет.

## Корпус

13 репозиториев склонированы в **`~/Projects/hh-references`** (постоянно, не в /tmp),
закреплены по тем же коммитам, что в предыдущем аудите:

`hh-mcp-server` `91b7eea` · `hh-autoresponder` `bfb4669` · `hh-job-application-automation`
`58ca440` · `hh-bot` `3a12421` · `hh-auto-apply` (semernyakov) `8bec8cc` · `hh-auto-responder`
`3e30d85` · `hh-ai-auto-apply-assistant` `b113644` · `hh-ai-agent` `c675c16` · `hh-ai-job-bot`
`3ded46f` · `hh-ru-apply` `7a56af1` · `lil-zon-hh-auto-apply` `19dd68c` · `hh.ru-clicker`
`bd3d526` · `hh-applicant-tool` `63210bc`

**Лицензии.** `s3rgeym/hh-applicant-tool`, `Vlad9572324/hh.ru-clicker`, `lil-zon/hh-auto-apply`
— **без LICENSE** (all rights reserved); `AgentShekel/hh-bot` — NOASSERTION. Код **не
копировался ни из одного репозитория**, включая MIT/Unlicense: читалось только поведение.

## Шкала доказательности

Уровень `HYPOTHESIS` в этом документе **не используется**: находка либо доказана кодом и
тестом, либо вынесена в раздел отклонённых.

| Уровень | Значение |
|---|---|
| `PROVEN` | наш живой лог/дамп/инцидент **и** воспроизводящий тест |
| `CORROBORATED` | разбор кода **и** тест; внешний свидетель без нашего живого следа |

---

## B1 `PROVEN` — headless-сбой песочницы даёт сырой traceback вместо `[ENVIRONMENT]`

**Живой инцидент.** `data/logs/hhru_bot.log:1243`, 2026-08-19 08:59:21, команда `list-resumes`,
режим **headless** (`chrome-headless-shell` в аргументах запуска):

```
FATAL:base/apple/mach_port_rendezvous_mac.cc:159 Check failed: kr == KERN_SUCCESS.
bootstrap_check_in org.chromium.Chromium.MachPortRendezvousServer.13682: Permission denied (1100)
```

**Механизм.** `browser.py:43` классифицирует сбой запуска в `BrowserLaunchError` под условием

```python
if not headless and any(marker in details for marker in sandbox_markers):
```

Не выполнено **ни одно** из двух условий: (1) режим был headless; (2) маркера `Permission
denied` в списке (`Operation not permitted`, `Crashpad`, `NSApplication`,
`NSMenuBarPresentationInstance`) нет. Исключение уходит мимо обработчика `cli.py:173` и
допечатывается Python-ом как ~40 строк traceback + Chromium-лог, что нарушает контракт вывода
CLI (CLAUDE.md: только `[OK]/[INFO]/[FAIL]/[DRY-RUN]/[skip]`).

**Дефект именно текущего кода.** Трейсбек прошёл **через** `launch_browser` (`browser.py:30` в
стеке). Более ранние крахи 00:16 и 08:33 (`browser.py:119`/`:163`) относятся к периоду до
появления helper'а и в счёт не идут.

**Отягчающее.** Тестов на `launch_browser`/`BrowserLaunchError` не было **ни одного**.

Тесты: `test_headless_sandbox_failure_is_not_classified_as_browser_launch_error`,
`test_permission_denied_is_absent_from_sandbox_markers`,
`test_headed_sandbox_failure_is_classified` (контроль: headed-путь работает).

---

## B3 `CORROBORATED` — дневной лимит `apply` считается по резюме, аккаунт множит его втрое

**Механизм.** `throttle.py:45` спрашивает `count_today(resume_id, "apply")` — счётчик **per
resume**. `commands/apply.py:38` без `--resume` идёт **по всем резюме конфига**. При трёх
резюме и `daily_apply_limit: 40` (значения из `data/config.yaml`) аккаунт отправляет до **120**
откликов в сутки при заявленном дневном лимите 40.

**Почему это дефект, а не задумка.** В том же классе `check_reply_limit` (`throttle.py:57-62`)
считает **аккаунт целиком** (`count_today("", "reply")`) и прямо документирован как
«account-wide replies». Два лимита одного класса имеют разную область — при том, что
антифрод-смысл у них общий. `commands/whoami.py:130` суммирует `count_today` по всем резюме,
то есть значимым для пользователя числом является именно аккаунтное.

Дневной лимит в этом проекте — не удобство, а защита: CLAUDE.md формулирует его как средство
«не выглядеть как подозрительная автоматизация для анти-фрод системы hh.ru».

Тест: `test_apply_daily_limit_is_per_resume_so_account_total_multiplies` (включая контроль,
что reply-лимит действительно аккаунтный и срабатывает).

---

## B4 `CORROBORATED` — необратимый отзыв отклика не умеет статус `uncertain`

**Механизм.** `commands/clear_negotiations.py:147` кликает `controls.first.click()` — это
**необратимый** отзыв отклика. Дальше `_run_topics` (`:300-309`) знает ровно два статуса:

```python
status = "success" if success else "failed"
```

Основной путь: пост-клик ожидание позитивного маркера падает в `except (PlaywrightTimeoutError,
PlaywrightError)` (`:168`) и **возвращает** `(False, ...)`, то есть исход штатно доезжает до
`_run_topics` и пишется `failed`. Тот же итог у внешнего `except` (`:179`) и у широкого
`except Exception` (`:302`). Слова `uncertain` в модуле **нет ни разу**.

**Почему это дефект.** Проект возвёл различение `success`/`failed`/`uncertain` вокруг
необратимого клика в инвариант (#176, #207) и реализовал его для **обратимого** `bump`
(`bump.py:102-119`: `acted=True, uncertain=True`) и для `reply_employers` (`:179-203`). Самая
разрушительная команда осталась без него.

**Последствие.** Аудиторская запись искажена: необратимое действие, которое могло состояться,
зафиксировано как несостоявшееся. Пользователь и `query` (#45) видят «отзыв не прошёл» там, где
отклик мог быть отозван.

**Оговорка, важная для фикса (уточнено ревью Codex).** Соблазнительно добавить сюда
«и поэтому дедупликация ломается», но это было бы overclaim: `_run_topics` **вообще не
опрашивает историю** перед отзывом (единственное обращение — `record_action` на `:309`), а
решение кликать принимается по текущему SSR/DOM. Поэтому одна лишь смена статуса на
`uncertain` дедупликацию не даёт — она требует отдельного history-guard'а перед кликом.
Статус и guard — две разные части фикса, и путать их нельзя.

Тесты: `test_withdraw_failure_after_destructive_click_is_recorded_as_failed_not_uncertain`
(на реальной SQLite во временной директории), `test_bump_module_implements_the_uncertain_invariant_that_withdraw_lacks`.

---

## B5 `CORROBORATED` — `save_about` сообщает «не сохранено» после состоявшегося клика

**Механизм.** `about.py:145-156`:

```python
save.click()
try:
    field.wait_for(state="hidden")
except PlaywrightError as exc:
    raise AboutGenerationError("сохранение не подтверждено: inline-форма не закрылась") from exc
```

Таймаут в этой точке — ровно «серая зона» #207: клик уже ушёл на hh.ru. `commands/about.py:108-110`
печатает `[FAIL]` и завершает процесс с кодом 1, то есть сообщает пользователю, что текст не
сохранён. Признака состоявшегося действия нет: `uncertain` в `about.py` не встречается.

Побочно: `field.wait_for(state="hidden")` вызван **без** `timeout` — в отличие от соседних
редакторов, где стоит явный `SAVE_TIMEOUT_MS`.

Тесты (после фикса инвертированы): `test_save_about_marks_post_click_timeout_as_uncertain`,
`test_about_module_marks_post_click_save_failures_as_uncertain`.

---

## B6 `CORROBORATED` — разрыв того же инварианта системный, а не единичный

Инвентарь всех WRITE-модулей по числу упоминаний `uncertain`:

| Соблюдают инвариант | Не имеют его вовсе |
|---|---|
| `apply/pipeline.py` (19), `publish_resume.py` (10), `resume_education.py` (7), `copy_resume.py` (6), `bump.py` (5), `reply_employers.py` (5), `delete_resume.py` (4), `create_resume.py` (3), `skills.py` (1) | `clear_negotiations.py` (0, см. B4), `about.py` (0, см. B5), `experience.py` (0), `resume_position.py` (0), `resume_sections.py` (0) |

Нагляднее всего `commands/resume_position.py:205-213`: после `page.locator(SAVE).click()`
любое исключение печатается как голый `[FAIL] {exc}` и команда возвращает `True`, хотя
сохранение уже могло примениться.

Тест (после фикса инвертирован):
`test_write_modules_mark_post_click_save_failures_as_uncertain` (параметризован по четырём
модулям).

---

## Отклонённые гипотезы (отрицательный результат — тоже результат)

| Гипотеза | Проверка | Вердикт |
|---|---|---|
| **B2:** `wait_for_url` без `timeout` в `create_resume.py:170,214` и `resume_education.py:243` наследует 90 с — дефект класса A5/#337 | `browser.py:181-188` задаёт потолок **context-wide** через `set_default_navigation_timeout` осознанно и по DRY; коммит #352 (`f7c1f53`) правил ровно эти вызовы, добавляя `wait_until="commit"` и привязку к identity, но намеренно **не** добавляя `timeout` | **Отклонена**: принятое решение проекта |
| Селекторы `resume-profile-position-input` / `resume-delete-confirm` не подтверждены (0 совпадений в дампе) | Помечены подтверждёнными живым DOM 18.08 (`resume_page.py:41-66`); дампов экранов create/delete среди 144 файлов **нет вовсе**, поэтому отсутствие в `resume-investigation-baseline.html` (экран *редактирования*) ничего не доказывает | **Отклонена**: ошибка выборки |
| Проглоченные `logger.warning` без `return` (шаблон A2/#341) — 15 мест | Разобраны все: `responses.py:365,441`, `copy_resume.py:488,701`, `search.py:326`, `_common.py:353` — каждый снабжён комментарием, объясняющим, почему продолжение корректно (read-only сбор, reconciliation ниже по коду, fail-closed в верификаторе) | **Отклонена** |
| Наивные `datetime.now()` в `history.py` — путаница локального времени и UTC | Все 14 мест записи и чтения последовательно наивно-локальные; `history.py:1811` это фиксирует явно | **Отклонена**: внутренне согласовано |
| Широкий `except Exception` в браузерных путях (21 место) | Все, кроме `clear_negotiations.py:302` (см. B4), несут `# noqa: BLE001` с обоснованием fail-closed | **Отклонена**, кроме B4 |
| Капча: нужен детект/решение | #344 закрыт: `apply/antibot.py` реализован и **подключён** — 13 call sites в `pipeline.py`, `verify.py`, `commands/_common.py`; терминальный `[FAIL]` в `cli.py:176` | **Уже сделано**; решение капчи (AI-Vision, `s3rgeym::_solve_captcha_async`) отклонено в #84 как обход анти-фрода |

## Что у нас правее референсов

1. Разделение `success`/`failed`/`uncertain` вокруг необратимого клика и post-click
   reconciliation через `/applicant/negotiations` — эквивалента нет ни у одного из 13.
   Находки B4–B6 — это именно **неполное применение собственного инварианта**, а не его отсутствие.
2. Дедупликация по локальной истории, а не по разметке страницы.
3. Отказ отвечать на тест-вопросы за кандидата (#95) — сознательное решение.
4. Детект анти-бот проверки как **остановка**, а не обход (#344).

## Воспроизводимость без браузера

```
# B1: условие классификации и отсутствие маркера
grep -n "not headless and any" src/hhru_bot/browser.py
grep -n "Permission denied" src/hhru_bot/browser.py          # -> пусто

# B3: лимит apply per-resume, лимит reply account-wide
grep -n "count_today" src/hhru_bot/throttle.py

# B4: статусы отзыва и отсутствие uncertain
grep -n 'status = "success" if success else "failed"' src/hhru_bot/commands/clear_negotiations.py
grep -c uncertain src/hhru_bot/commands/clear_negotiations.py   # -> 0

# B5/B6: инвентарь инварианта по WRITE-модулям
for f in about experience resume_position resume_sections; do
  echo "$f $(grep -c uncertain src/hhru_bot/$f.py)"; done

# все находки разом
pytest tests/test_audit_launch_and_navigation.py -q
```

## Итог и приоритеты

| Находка | Уровень | Приоритет | Суть |
|---|---|---|---|
| B4 — необратимый отзыв без `uncertain` | `CORROBORATED` | **high** | повторный отзыв уже отозванного; самая разрушительная команда |
| B3 — дневной лимит множится на число резюме | `CORROBORATED` | **high** | 120 откликов/сутки при лимите 40; антифрод-риск |
| B5 — `save_about` рапортует «не сохранено» после клика | `CORROBORATED` | medium | пользователь дублирует запись |
| B6 — разрыв инварианта в 4 редакторах резюме | `CORROBORATED` | medium | системный класс |
| B1 — сырой traceback вместо `[ENVIRONMENT]` | `PROVEN` | medium | нарушение контракта вывода; нулевое тестовое покрытие |

**Правок продакшн-кода в рамках этого аудита не выполняется** — каждая находка чинится
отдельным воркером отдельным PR (тот же контракт, что в предыдущем аудите). Добавлены только
тесты-доказательства: они **проходят на текущем коде**, фиксируя дефект как характеризацию, и
должны быть инвертированы автором фикса.
