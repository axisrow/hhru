---
description: Run hhru CLI commands (search, apply, bump, update, probe, stats, query, responses, funnel, whoami, list-resumes, market, ...)
---

# hhru — CLI hh.ru-бота

Запусти команду hhru-бота, которую запросил пользователь, через обёртку CLI
из корня проекта (opencode выполняет shell-команды из корня проекта):

```bash
bash ./scripts/run.sh $ARGUMENTS
```

`$ARGUMENTS` — это `<command> [flags]`, которые пользователь передал после `/hhru`.
Например `/hhru whoami` -> `bash ./scripts/run.sh whoami`,
`/hhru search --resume <id> --dry-run` -> `bash ./scripts/run.sh search --resume <id> --dry-run`.
Если в проекте доступен инструмент `hhru` (плагин `.opencode/plugins/hhru.ts`),
предпочитай его: он сам подставляет `--headless`/`--account` и держит
dry-run/подтверждение для WRITE-команд.

CLI — это конструктор примитивов, а не набор готовых сценариев по одному на
каждую пользовательскую задачу. Если готовой команды нет, собери workflow
поэтапно из существующих команд. Например, перевод резюме выполняется через
чтение текущих разделов, самостоятельную генерацию перевода и последовательное
применение per-section команд с `--dry-run` перед каждым сохранением. Подробный
пример и список команд для разделов приведён в `skills/hhru/SKILL.md`.

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

## Если команда не найдена

Покажи пользователю `bash ./scripts/run.sh --help` и список доступных команд.
Не выдумывай флаги, которых нет в CLI.
