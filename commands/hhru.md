---
name: hhru
description: Run hhru CLI commands (search, apply, bump, probe, stats, query, responses, funnel, whoami, list-resumes, market, ...)
argument-hint: "<command> [flags]"
allowed-tools: Bash
---

# hhru — CLI hh.ru-бота

Запусти команду hhru-бота, которую запросил пользователь, через обёртку CLI:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" $ARGUMENTS
```

`$ARGUMENTS` — это `<command> [flags]`, которые пользователь передал после `/hhru`.
Например `/hhru whoami` -> `bash .../run.sh whoami`, `/hhru search --resume <id> --dry-run`
-> `bash .../run.sh search --resume <id> --dry-run`.

## Правила безопасности (обязательно)

- **Write-команды к hh.ru — сначала `--dry-run`.** `apply`, `bump`, `run`,
  `publish-resume`, `reply-employers`, `clear-negotiations`, `copy-resume` меняют
  состояние аккаунта на hh.ru. Если пользователь не передал `--dry-run` и не
  просил боевой запуск явно — сначала покажи план через `--dry-run`, затем
  спроси подтверждение перед боевым запуском.
- **Read-only для hh.ru.** `search`, `probe`, `stats`, `query`, `responses`,
  `funnel`, `whoami`, `list-resumes`, `market`, `log`, `schedule` ничего не меняют
  на hh.ru — их можно запускать без подтверждения.
- **Уважай троттлинг и дневные лимиты.** Не запускай `apply`/`bump`/`run` чаще,
  чем позволяет бот (кулдаун поднятия 4 часа, дневные лимиты). Не обходи их.
- **Никаких эмодзи.** Вывод CLI — только текст и ASCII-таблицы. Не добавляй
  эмодзи в вывод и в свои сообщения о результате.
- **Не используй `page.request.*` и внутренние API hh.ru** — только команды бота.

## Справочник команд

| Команда | Природа | Что делает |
|---|---|---|
| `login` / `login-code` / `import-cookies` | WRITE-local | вход, сохранение сессии |
| `search` | READ | поиск вакансий по фильтрам (без откликов) |
| `apply` | WRITE-hh-ru | отклик с письмом (dry-run-first) |
| `bump` | WRITE-hh-ru | поднятие резюме (кулдаун 4ч) |
| `run` | WRITE-hh-ru | полный цикл apply + bump |
| `probe` | READ | дамп формы отклика без отправки |
| `stats` / `query` | READ | сводка / read-only SELECT к истории |
| `responses` / `funnel` | READ | ответы работодателей / воронка |
| `whoami` / `list-resumes` | READ | сессия и резюме |
| `market` | READ | агрегаты рынка |
| `mark` / `clear-skipped` | WRITE-local | пометка оффера / очистка skipped |
| `publish-resume` / `reply-employers` / `clear-negotiations` / `copy-resume` | WRITE-hh-ru | деструктивные/массовые, требуют `--force` или подтверждения |

## Если команда не найдена

Покажи пользователю `bash "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" --help` и список
доступных команд. Не выдумывай флаги, которых нет в CLI.
