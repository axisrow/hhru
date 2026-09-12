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

CLI — это конструктор примитивов, а не набор готовых сценариев: нестандартную
задачу собирай поэтапно из существующих команд. Подробный пример (перевод
резюме), справочник команд и правила безопасности — в `skills/hhru/SKILL.md`.

Правила безопасности обязательны: WRITE-команды к hh.ru — сначала `--dry-run`
и подтверждение человека перед боевым запуском; read-only свободны; троттлинг
и дневные лимиты не обходить; никаких эмодзи; никаких `page.request.*` и
внутренних API hh.ru. Полный текст — `skills/hhru/SKILL.md`, раздел
«Правила безопасности».

Если команды нет в справочнике — покажи пользователю
`bash ./scripts/run.sh --help` и список доступных команд. Не выдумывай флаги,
которых нет в CLI.
