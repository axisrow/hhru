# hhru-bot

CLI-бот для поиска работы на hh.ru: ищет вакансии, откликается письмом,
поднимает резюме, следит за ответами и умеет редактировать само резюме.
Работает через Playwright (браузер), а не через API — hh.ru закрыл его для
соискателей в декабре 2025.

## Чем мы отличаемся от аналогов

Идея сравнения и разбор референсов — issue [#84](https://github.com/axisrow/hhru/issues/84);
ревизия 2026-09 — проверено по коду референсов, не по README.
Легенда: ✅ есть, ❌ нет, ⚠️ есть с оговоркой (она указана в ячейке).

| Фича | s3rgeym | fikstt2 | Steev193 | tgeruzov | konard | hhru-bot |
|---|---|---|---|---|---|---|
| Поиск + отклик | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Поднятие резюме | ✅ через API | ❌ | ❌ | ❌ | ❌ | ✅ через UI |
| Троттлинг и дневные лимиты | ⚠️ без дневных | ✅ | ⚠️ без дневных | ⚠️ без дневных | ⚠️ без дневных | ✅ |
| Мультиаккаунт | ✅ профили | ❌ | ❌ | ❌ | ❌ | ✅ |
| Редактирование и создание резюме | ⚠️ клон + шаблон | ❌ | ❌ | ❌ | ❌ | ✅ |
| Анализ резюме конкурентов | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| Обучаемые ответы на анкеты | ❌ | ❌ | ❌ | ❌ | ✅ Q&A-файл, авто-накопление | ✅ подтверждённые шаблоны + LLM |
| Честный статус «не знаю, дошло ли» | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| Воронка и статистика по истории | ⚠️ | ❌ | ❌ | ❌ | ❌ | ✅ |

- **Поднятие резюме** — s3rgeym делает это API-вызовом (хрупко после закрытия
  API для соискателей), hhru-bot — кликом по кнопке в UI, как человек.
- **Троттлинг и дневные лимиты** — ⚠️ у всех четверых случайные паузы есть,
  но дневного лимита откликов нет (лимит «за запуск» или реакция на отказ
  hh.ru); только fikstt2 ведёт настоящий дневной счётчик.
- **Мультиаккаунт** — s3rgeym изолирует профили через `--profile`; hhru-bot —
  `data/accounts/<name>/` с отдельной сессией, историей и лимитами на каждый
  аккаунт, разные аккаунты можно гонять параллельно.
- **Редактирование и создание резюме** — `create-resume`, `edit-experience`,
  `edit-education`, `edit-skills`, `edit-languages`, `resume-position`,
  `publish-resume` и другие: собрать резюме с нуля или довести до публикации.
  У s3rgeym — только клон и создание из markdown-шаблона через API,
  редактирования секций нет.
- **Анализ резюме конкурентов** (`competitors`) — с кем ты конкурируешь за
  вакансию: отчёт по ролям, зарплатам и навыкам других соискателей.
- **Обучаемые ответы на анкеты** (`questionnaire`) — бот подбирает ответ по
  подтверждённым формулировкам, для новых вопросов подключает LLM;
  неуверенные случаи идут в очередь на твоё решение. У konard база Q&A —
  файл с нечётким матчем, накапливается автоматически при твоих ручных
  ответах; подтверждения формулировок и LLM нет.
- **Честный статус «не знаю»** (`uncertain`) — если связь с hh.ru оборвалась
  в момент клика, бот не врёт «готово»/«не готово», а говорит «не знаю».
- **Воронка и статистика** (`funnel`, `stats`, `responses`, `market`) —
  сколько откликов ушло, сколько ответов, где «мёртвая зона».

Проекты и ссылки — [docs/similar-projects.md](docs/similar-projects.md).
Полный список команд — [docs/cli-reference.md](docs/cli-reference.md).

## Основные возможности

Ядро бота — основной цикл соискателя: найти вакансии → откликнуться →
не попасть под анти-фрод → увидеть результат.

| Фича | Команды | Что делает |
|---|---|---|
| Поиск вакансий с фильтрами | `search` | Фильтры на каждое резюме (`text`, `area`, `salary_from`, `exclude_employers`, `exclude_keywords`), отсев с логированием причин |
| Отклик с письмом | `apply` | Сопроводительное с плейсхолдерами `{vacancy_title}`/`{company_name}`, `--limit` успешных откликов за запуск |
| Поднятие резюме | `bump`, `run` | Не чаще раза в 4 часа; `run` = полный цикл apply + bump |
| Троттлинг и дневные лимиты | — | Случайные паузы после каждого действия + дневные лимиты откликов/поднятий |
| Дедупликация откликов | — | Локальная SQLite-история: повторно ту же вакансию не отправит, даже между резюме |
| Безопасный запуск | `--dry-run`, `--force` | План без единого клика по hh.ru; опасные действия — только с подтверждением |
| Честный статус «не знаю» | `uncertain` | Если связь оборвалась в момент клика — «не знаю, дошло ли», а не ложное «готово»; вердикт сверяется с hh.ru |
| Сессия и мультилогин | `login`, `import-cookies`, `--account` | Вход вручную один раз, дальше headless; отдельная история и лимиты на каждый аккаунт |

## Дополнительные возможности

Надстройки над ядром — от полного редактора резюме до аналитики и
интеграций с агентами.

| Фича | Команды | Что делает |
|---|---|---|
| Создание резюме с нуля | `create-resume`, `wizard-next`, `publish-resume` | Визард hh.ru: черновик → дозаполнение → публикация |
| Редактирование резюме | `edit-experience`, `edit-education`, `edit-skills`, `edit-languages`, `resume-position`, `common`, `resume-sections` | Заполнение секций через UI hh.ru — вручную или LLM-планом |
| Ручной ввод без LLM | `--entry`, `--text`, `--skill` и т.п. | Те же команды с готовыми значениями — LLM не нужен |
| Фото резюме | `upload-photo`, `select-photo`, `delete-photo` | Загрузка, назначение из библиотеки, скрытие/удаление |
| Перенос резюме между аккаунтами | `export-resume`, `import-resume`, `copy-resume` | Полный дамп в JSON, импорт в другой аккаунт, дублирование |
| Адаптация под кластеры вакансий | `adaptive-resume`, `resume-pool` | Заголовок/«Обо мне»/навыки под конкретный кластер; пул резюме по кластерам |
| Обучаемые ответы на анкеты | `questionnaire`, `apply --learn-questionnaires` | Ответы по подтверждённым шаблонам, LLM для новых, неуверенные — в очередь |
| Ответы работодателям | `responses`, `reply-employers`, `robot-*`, `clear-negotiations` | Новые приглашения, follow-up при молчании, ответы роботам, отзыв откликов |
| Календарь | `calendar event`, `responses --calendar-hint` | Готовая команда события с плейсхолдерами под новое приглашение |
| Воронка и статистика | `funnel`, `stats`, `resume-views`, `skipped` | Отклики → ответы → «мёртвая зона», агрегаты по истории |
| Анализ рынка и конкурентов | `market`, `competitors` | Медианы ЗП по сфере; отчёт по ролям/зарплатам/навыкам других соискателей |
| Видимость резюме | `resume-visibility`, `blacklist` | «Кто видит/не видит», стоп-лист работодателей |
| Автопоиски hh.ru | `search --save`, `search --saved` | Сохранение параметрики поиска на стороне hh.ru и запуск по ней |
| Запуск по расписанию | `schedule`, cron/launchd/Docker | Внешний планировщик; те же лимиты и кулдаун, что и вручную |
| Агентские интеграции | `/hhru` (Claude Code, Codex, opencode) | Команды бота как инструменты/скиллы из агентской сессии |
| Диагностика | `diagnostics doctor`, `probe`, `census`, `query` | Healthcheck селекторов, census отрисованных контролов, SQL к истории |
| Обслуживание | `backup`, `restore`, `log --prune`, `update` | Бэкап/восстановление `data/`, чистка дампов, синхронный апдейт CLI и плагина |

## Как это работает

Запускается вручную из терминала. Каждая команда печатает, что делает,
поддерживает `--dry-run` (план без единого клика по hh.ru) и ограничена
дневными лимитами и случайными паузами — чтобы не выглядеть подозрительной
автоматизацией для анти-фрод системы hh.ru. Опасные действия (отклик,
публикация, удаление резюме) без `--force` или подтверждения не выполняются.

Для регулярного запуска (например, поднимать резюме каждые 4 часа) подключи
внешний планировщик — cron/launchd или Docker (см. «Автопилот» ниже). Своего
фонового демона у бота нет.

## ⚠️ Про селекторы

Канонический источник селекторов — пакет `src/hhru_bot/selector_groups/`
(по страницам). Большинство уже подтверждено живыми дампами и боевыми
прогонами (страница поиска, страница вакансии, форма отклика, формы
редактирования резюме); статус подтверждённости каждого селектора — в
комментариях того модуля, где он определён.

**Перед первым боевым `apply`/`bump`:**
1. Прогони `search --dry-run`, затем `apply --dry-run` — они не делают
   ни одного клика по hh.ru.
2. Если что-то не находится — запусти `probe --healthcheck` или `census
   --url <URL>` (таблица отрисованных контролов, read-only), сверь
   актуальные `data-qa` с `selector_groups/` через F12 → Elements и
   поправь прямо в модуле группы.
3. Только потом — боевой запуск с малым `--limit`.

## Установка

```bash
pip3 install -r requirements.txt
python3 -m playwright install chromium
pip3 install -e .
```

## Настройка

```bash
hhru account create default
./scripts/run.sh --account default login
```

Отредактируй созданный `data/accounts/default/config.yaml`:
- `resumes` — твои резюме, у каждого свои фильтры поиска (`text`, `area`,
  `salary_from`, `experience`, `schedule`, `exclude_employers`,
  `exclude_keywords`) и опционально своё сопроводительное письмо.
- `exclude_keywords` — стоп-слова для заголовков вакансий (без учёта
  регистра), отдельно для каждого резюме. Пример — в `config/config.example.yaml`.
- `throttle` — паузы между действиями и дневные лимиты откликов/поднятий.
- В письме доступны плейсхолдеры `{vacancy_title}` и `{company_name}`.

## Структура каталогов

Всё изменяемое — конфиг, база, сессия hh.ru, логи — живёт в `data/`, целиком
в `.gitignore`.

```
data/
  accounts/
    default/
      config.yaml           # конфиг аккаунта (создаётся account create)
      history.db            # история аккаунта
  config.yaml               # шаблон — config/config.example.yaml
  history.db                # SQLite: история откликов, вакансии, ответы
  storage_state/
    hh_session.json         # сессия hh.ru — секрет, никогда не коммитить
  logs/
    hhru_bot.log            # лог CLI (дублируется в консоль)
    probe_*.html / .png     # дампы probe
    scheduled.log           # вывод запусков по расписанию

config/
  config.example.yaml       # шаблон формата, лежит в репозитории
```

Пути — относительно текущей директории: запускай команды из корня проекта
или указывай `--config`/`--history` явно. `hh_session.json` даёт доступ к
hh.ru наравне с паролем — не коммить `data/` и не включай в Docker-образ.

## Первый запуск: вход в аккаунт

```bash
./scripts/run.sh login
```

Откроется окно браузера на странице входа hh.ru. Войди в аккаунт вручную
(логин, пароль, СМС-код, капча — что бы ни попросил hh.ru), затем вернись
в терминал и нажми Enter. Сессия сохранится в
`data/storage_state/hh_session.json` — все последующие команды будут
переиспользовать её без повторного входа.

## Мультилогин: несколько аккаунтов hh.ru

Каждый аккаунт — своё имя и своя папка `data/accounts/<name>/`.

```bash
hhru account create marketing
./scripts/run.sh --account marketing login
# или, если куки hh.ru уже есть в профиле Chrome:
./scripts/run.sh --account marketing import-cookies --profile Default
```

Флаг `--account <name>` ставится до имени подкоманды:

```bash
./scripts/run.sh --account marketing apply --resume resume-name-1 --limit 5
```

То же самое для любой команды делает переменная окружения `HHRU_ACCOUNT`
(дефолт флага `--account`; явный `--account` в приоритете). Её же понимает
`scripts/scheduled_run.sh`:

```bash
HHRU_ACCOUNT=marketing scripts/scheduled_run.sh --headless apply --limit 5
```

Список аккаунтов и их состояние: `./scripts/run.sh account list`.

Дефолтный аккаунт для команд без флага и env задаётся строкой
`default_account: <name>` в корневом `data/config.yaml` (#1086):
без неё поведение прежнее (корневой конфиг), с ней команда без `--account`
идёт с `data/accounts/<name>/`. Указание на несуществующий аккаунт — `[FAIL]`
при запуске. Явные `--config`/`--history` отменяют настройку.

У каждого аккаунта своя сессия, своя история откликов/поднятий и свои
дневные лимиты — они не суммируются и не переносятся между аккаунтами.
Разные аккаунты можно гонять параллельно; два одновременных прогона
**одного** аккаунта запрещены (второй получит `[FAIL]`).

`hh_session.json` даёт доступ к hh.ru наравне с паролем; файлы сессии
создаются с правами `0600`, каталог аккаунта — `0700` (проверка —
`diagnostics doctor`).

Чтобы убрать аккаунт — удали `data/accounts/<name>/` вручную.

## Команды

Все команды поддерживают `--resume <id>` (по умолчанию — все резюме из
конфига) и `--dry-run` (показать план действий без реальных кликов).

```bash
# Проверить, что находит поиск, без откликов
./scripts/run.sh search --resume resume-name-1 --dry-run

# Откликнуться на подходящие вакансии (максимум 5 за запуск)
./scripts/run.sh apply --resume resume-name-1 --dry-run --limit 5

# То же самое по-настоящему, без dry-run
./scripts/run.sh apply --resume resume-name-1 --limit 5

# Поднять резюме в поиске (hh.ru разрешает не чаще раза в 4 часа)
./scripts/run.sh bump --resume resume-name-1

# Полный цикл (apply + bump) для всех резюме из конфига
./scripts/run.sh run
```

### Профиль для внешних форм

Контактные данные аккаунта сохраняются автоматически после `login`. Для
данных, которых нет на hh.ru (например, Telegram) — задай вручную, с тем же
`--account`:

```bash
./scripts/run.sh --account default profile set "Telegram" "@username"
./scripts/run.sh --account default profile show
./scripts/run.sh --account default profile unset "Telegram"
```

Добавь `--headless`, если не нужно видеть окно браузера.

## Автопилот: запуск по расписанию

Регулярность задаёт внешний планировщик. Каждый вариант ниже запускает
обычную команду `run` (apply + bump) — те же лимиты и кулдаун, что и вручную.
Перед автоматизацией проверь `./scripts/run.sh run --dry-run`.

### Локаль (cron / launchd)

Скопируй `scripts/crontab.example`, замени `__REPO_ROOT__` и `__PYTHON_BIN__`
на абсолютные пути, добавь через `crontab -e`. Обёртка пишет в
`data/logs/scheduled.log` сама.

`responses --alert-new` пишет `***** НОВОЕ ПРИГЛАШЕНИЕ *****` при новом
приглашении; `HHRU_ALERT_CMD` опционально запускает свою команду при
обнаружении. Пример — в `scripts/crontab.example`.

На macOS вместо cron — готовые шаблоны `deploy/com.hhru.bot.apply.plist`
(ежедневный apply) и `deploy/com.hhru.bot.bump.plist` (bump каждые 4 часа).
Замени `__REPO_ROOT__`/`__PYTHON_BIN__`, установи:

```bash
cp deploy/com.hhru.bot.apply.plist ~/Library/LaunchAgents/com.hhru.bot.apply.plist
launchctl load ~/Library/LaunchAgents/com.hhru.bot.apply.plist
```

Выгрузить: `launchctl unload ~/Library/LaunchAgents/com.hhru.bot.apply.plist`.
Сгенерировать шаблон: `./scripts/run.sh schedule --format plist --action apply
--apply-time 10:00 --apply-limit 5`.

### Docker

`data/` монтируется с хоста и не попадает в образ. Подготовь конфиг на хосте,
затем разовый прогон:

```bash
mkdir -p data
./scripts/run.sh account create default
docker compose run --rm --entrypoint hhru hhru --headless --account default run
```

Непрерывный режим — `docker compose up -d`: `docker-compose.yml` запускает
`run --headless` каждые 4 часа без дрейфа расписания.

```bash
docker compose up -d
docker compose logs -f hhru
```

Остановить: `docker compose down`. Не монтируй `storage_state` в публичные
каталоги и не добавляй `data/` в образ или git.

## Сценарий: пришло приглашение

1. Планировщик распознаёт новое приглашение (код выхода 10):

   ```bash
   ./scripts/run.sh --headless responses --alert-new
   ```

2. Посмотреть, что нового: `./scripts/run.sh responses`.

3. Что подтянуть по требованиям из собранных вакансий:
   `./scripts/run.sh learn --resume <id>`.

4. Время собеседования CLI не угадывает — `responses --calendar-hint`
   печатает готовую команду `calendar event` с плейсхолдерами:

   ```bash
   ./scripts/run.sh responses --calendar-hint
   ./scripts/run.sh calendar event --summary "ООО Ромашка - 12345" \
     --start 2026-09-01T14:00:00+03:00 --end 2026-09-01T15:00:00+03:00
   ```

Если работодатель молчит после отклика — напомнить о себе:

```bash
./scripts/run.sh reply-employers --follow-up --after-days 7 --dry-run
./scripts/run.sh reply-employers --follow-up --after-days 7 --force
```

### VPS

```bash
ssh user@example.com 'cd /opt/hhru && docker compose up -d --build'
ssh user@example.com 'cd /opt/hhru && docker compose logs -f hhru'
```

Сессию создай локально (`./scripts/run.sh --account default login`) или в
контейнере с временно отключённым `--headless`. Обновление:
`git pull && docker compose up -d --build`.

## Claude Code и Codex plugin

Репозиторий — маркетплейс плагина `hhru-cc-plugin`: подключает команды бота
как инструменты и скиллы прямо из агентской сессии Claude Code или Codex.

### Установка

```bash
claude plugin marketplace add axisrow/hhru
claude plugin install hhru-cc-plugin@hhru --scope user
```

### Codex: установка и обновление

Одна команда покрывает первую установку, апгрейд и восстановление после
ошибки — она идемпотентна, повторный запуск безопасен:

```bash
hhru update
```

Проверить состояние без изменений: `hhru diagnostics doctor` (или
`./scripts/run.sh diagnostics doctor` из checkout). При рассинхроне между CLI,
marketplace и plugin cache печатает `[DRIFT]` и подсказывает `hhru update`.

Уже открытая задача Codex после обновления продолжает работать со старым
skill — начни новую задачу, чтобы подхватить изменения.

### Команда `/hhru`

```bash
/hhru whoami
/hhru search --resume <id> --dry-run --max-pages 3
/hhru competitors collect --text "AI" --auth-mode anonymous --detail-workers 10
/hhru apply --resume <id> --dry-run --limit 5
/hhru responses
```

Write-команды (`apply`/`bump`/`run`/...) сначала `--dry-run`, потом
подтверждение перед боевым запуском.

### Скиллы

- **`hhru`** — главный: CLI-справочник, правила безопасности, проверка готовности.
- **`hhru-apply`** — воркфлоу отклика (dry-run-first, safety-critical).
- **`hhru-market`** — анализ рынка (read-only).
- **`hhru-monitor`** — мониторинг/статус (read-only).

### opencode: плагин, команда и скиллы из коробки

[opencode](https://opencode.ai) подхватывает интеграцию автоматически при запуске
из корня репозитория — без установки и настройки:

- **Плагин** (`.opencode/plugins/hhru.ts`) — инструмент `hhru`: READ-команды
  свободны, WRITE-hh.ru только через `dry_run=true` + явное подтверждение
  (`confirmed=true`), локальные WRITE — по подтверждению; WRITE через голый bash
  в обход инструмента блокируется. Ищет CLI в `.venv/bin/hhru` или в `PATH`;
  ограничение времени команд использует coreutils `timeout`/`gtimeout`, если
  найден в `PATH` (на стоковом macOS его нет — команды идут без OS-лимита,
  защитные лимиты остаются на стороне самого CLI).
- **Команда `/hhru`** (`.opencode/commands/hhru.md`) — тот же слэш-интерфейс,
  что у Claude Code / Codex plugin: `/hhru whoami`,
  `/hhru search --resume <id> --dry-run`.
- **Скиллы** — общий каталог `skills/` подключён симлинком
  `.opencode/skills` (документированный путь обнаружения opencode). Симлинк
  сохраняется только при установке через `git clone` (zip-выгрузка GitHub его
  разрушает). На Windows требуется включённый Developer Mode (или
  `git config core.symlinks true` при клонировании), иначе git оставит
  текстовую заглушку и скиллы не подхватятся.

Маркетплейса как в Claude Code (`claude plugin marketplace add`) у opencode нет:
по докам распространение плагинов — npm-пакет в `"plugin"`-ключе `opencode.json`
или локальные файлы в `.opencode/plugins/`. Здесь выбран локальный вариант —
клон репозитория сам является источником; npm-публикация останется отдельным
шагом, если понадобится.

Для работы интеграции сам бот должен быть установлен и настроен (см. «Установка»
и «Настройка» выше). При первом старте opencode сам ставит зависимости плагина
(Bun) в `.opencode/` — каталоги `node_modules`/`package.json` там игнорируются
через `.opencode/.gitignore`.

## Подготовка к интервью

Бот автоматизирует цепочку «пришло приглашение → follow-up» (см. «Сценарий:
пришло приглашение» выше). Само собеседование — нарратив, портфолио,
поведенческие вопросы — готовишь сам:

- подготовь проекты и портфолио под требования, которые показал `learn`;
- подготовь нарратив («расскажите о себе») и ответы на поведенческие вопросы;
- поставь реальное время интервью в `calendar event` вместо плейсхолдера.

## Документация

- **[docs/cli-reference.md](docs/cli-reference.md)** — полный справочник
  команд. Генерируется автоматически из argparse
  (`scripts/gen_cli_docs.py`); CI падает при рассинхроне с кодом.
- [docs/hh-quirks.md](docs/hh-quirks.md) — известные особенности hh.ru,
  подтверждённые живыми прогонами.
- [docs/how-it-works.md](docs/how-it-works.md) — как это устроено внутри.
- [docs/logs.md](docs/logs.md) — ротация логов.
- [docs/similar-projects.md](docs/similar-projects.md) — похожие проекты
  (для идей, не для копирования).
