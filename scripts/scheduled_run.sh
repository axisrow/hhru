#!/usr/bin/env bash
# Обёртка запуска по расписанию для внешнего планировщика ОС (launchd/cron).
# Этап 6 «автопилот» (#18). Сама обёртка НЕ демон — её вызывает планировщик в
# нужное время. CLAUDE.md запрещает фоновые демоны внутри проекта.
#
# По образцу scripts/run.sh: ставит PYTHONPATH=src и вызывает hhru_bot.cli.
# Дополнительно: пишет свой вывод в logs/scheduled.log и вызывает bump + apply.
# Предохранители (дневные лимиты, кулдаун бампа 4ч) живут в коде — throttle.py.
#
# Аргументы передаются дальше CLI как есть, например:
#   scripts/scheduled_run.sh bump --headless
#   scripts/scheduled_run.sh apply --headless --limit 5
# Планировщик обычно зовёт одно действие за раз (см. deploy/*.plist шаблоны и
# вывод `hhru-bot schedule`).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PROJECT_ROOT}/src"

LOG_DIR="${PROJECT_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/scheduled.log"

# Метка запуска в логе: дата + что запускаем (без Date в самой обёртке —
# date(1) внешняя, не запрещена скрипту оболочки; запрет на Date.now() касается
# только JS workflow-скриптов).
{
  echo "----- $(date '+%Y-%m-%d %H:%M:%S') scheduled_run: $* -----"
} >> "${LOG_FILE}"

# Запускаем CLI, вывод дублируется в scheduled.log. tee без буферизации (-a —
# дозапись, чтобы история запусков копилась). exit-код пробрасываем дальше,
# чтобы планировщик видел упавший прогон.
run_cli() {
  python3 -m hhru_bot.cli "$@" 2>&1 | tee -a "${LOG_FILE}"
  return "${PIPESTATUS[0]}"
}

run_cli "$@"
