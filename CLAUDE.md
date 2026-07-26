# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

CLI-инструмент на Playwright для поиска вакансий, откликов и поднятия резюме на hh.ru.
Запускается **только вручную** из терминала. Каждая команда логирует свои действия,
поддерживает `--dry-run` и ограничена дневными лимитами + случайными паузами, чтобы
не выглядеть как подозрительная автоматизация для анти-фрод системы hh.ru. При работе
с этим кодом сохраняй этот принцип: не добавляй фоновые/скрытые режимы и не убирай
троттлинг/лимиты.

## Команды

```bash
# Установка
pip3 install -r requirements.txt
python3 -m playwright install chromium

# Настройка (config.yaml в .gitignore — коммитить не нужно)
cp config/config.example.yaml config/config.yaml

# Все команды запускаются через обёртку run.sh (ставит PYTHONPATH=src и вызывает hhru_bot.cli)
./scripts/run.sh login                                    # ручной вход, сохраняет сессию
./scripts/run.sh search --resume <id> --dry-run           # поиск без откликов
./scripts/run.sh apply  --resume <id> --dry-run --limit 5 # план откликов
./scripts/run.sh apply  --resume <id> --limit 5           # боевой отклик
./scripts/run.sh bump   --resume <id>                     # поднять резюме (не чаще 1 раза в 4 часа)
./scripts/run.sh run                                       # apply + bump для всех резюме

# Общие флаги: --headless, --verbose, --config <path>, --history <path>, --max-pages <n>
# --resume опционален — без него команда идёт по всем резюме из конфига
```

Тестов, линтера и системы сборки в проекте **нет** — это простой скрипт с `requirements.txt`
и запуском через `PYTHONPATH=src python3 -m hhru_bot.cli`.

## Архитектура

Поток данных: `cli.py` → `search.py` (сбор карточек) → `filter_candidates` (отсев) →
`apply.py`/`bump.py` (действие) → `history.py` (запись результата). Все браузерные модули
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
   отсеет. `count_today()`/`last_action_at()` для лимитов считают только `status='success'`.

3. **Двухуровневый троттлинг** в `throttle.py`:
   - Дневные лимиты (`daily_apply_limit`, `daily_bump_limit`) — проверяются перед каждым
     действием, при `dry_run` не применяются.
   - Кулдаун поднятия резюме: жёстко `BUMP_COOLDOWN = 4 часа` (`can_bump_now()`), сверх
     дневного лимита.
   - `throttle.wait()` — случайная пауза `min_delay..max_delay` секунд после каждого
     действия.

4. **Форма отклика — двухшаговая навигация.** `VACANCY_APPLY_BUTTON` на странице вакансии
   это `<a href="/applicant/vacancy_response?...">`, а НЕ триггер модалки на той же
   странице. `apply.py` кликает и ждёт `page.expect_navigation(...)`, и только потом ищет
   поля формы. Не переписывай на «клик → искать модалку сразу».

### `selectors.py` — статус проверки селекторов (критично)

Все CSS/`data-qa` селекторы hh.ru собраны в одном файле `selectors.py`; остальной код их
не дублирует. Их статус разный, и это отражено в комментариях файла:

- **Подтверждено curl-дампом** (без логина): селекторы страницы поиска (`/search/vacancy`)
  и страницы вакансии (`/vacancy/{id}`).
- **НЕ подтверждено** (рендерится только залогиненному через JS): форма отклика на
  `/applicant/vacancy_response`, страница резюме с кнопкой поднятия, маркер «уже откликались».

Перед первым боевым `apply`/`bump`: пройти `login`, открыть эти страницы в обычном браузере
(F12 → Elements), сверить `data-qa` и поправить прямо в `selectors.py`. При отладке падений
на этих шагах первый подозреваемый — устаревший непроверенный селектор, а не логика модуля.
`_select_resume_in_form()` в `apply.py` тоже помечен как приблизительный.

## Конфигурация и данные

- `config.py` парсит `config.yaml` в датаклассы (`AppConfig` → `ResumeConfig` → `SearchFilters`)
  с явной валидацией через `_require()`; `ResumeConfig.resume_id` вычисляется из хвоста
  `resume_url`. Ошибки конфига бросают `ConfigError`, а `load_config_or_exit()` печатает их
  и делает `sys.exit(1)`.
- Сопроводительное письмо: `cover_letter` резюме, иначе `cover_letter_default`. Плейсхолдеры
  `{vacancy_title}` и `{company_name}` подставляются в `render_cover_letter()` (apply.py).
- Пути (все относительно `PROJECT_ROOT`, определённого в `config.py`):
  - Сессия браузера: `data/storage_state/hh_session.json` (из `account.storage_state_file`)
  - История: `data/history.db` (SQLite)
  - Логи: `logs/hhru_bot.log` (+ дублируются в консоль)
- В `.gitignore`: `config/config.yaml`, `data/storage_state/*.json`, `data/*.db`, `logs/*.log`
  — реальные ссылки на резюме, сессия и история наружу не попадают.
