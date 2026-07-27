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

# CLI резолвит config/config.yaml, data/history.db и logs/ ОТНОСИТЕЛЬНО cwd
# (см. cli.py: DEFAULT_CONFIG_PATH = Path("config")/"config.yaml"). launchd и
# cron НЕ гарантируют cwd = корень репо (часто это / или $HOME) — без этого cd
# плановый джоб не найдёт конфиг или создаст data/ в системном cwd, ломая
# дедупликацию и throttle. Явно переходим в корень репозитория.
cd "${PROJECT_ROOT}"

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
#
# Интерпретатор: launchd/cron имеют УРЕЗАННЫЙ PATH и НЕ активируют виртуальное
# окружение проекта (.venv), поэтому голый `python3` резолвится в системный
# /usr/bin/python3 без playwright → ModuleNotFoundError до любого действия.
# Обёртка читает HHRU_PYTHON (абсолютный путь к python из venv) — его задаёт
# launchd-агент через EnvironmentVariables (см. deploy/*.plist) или cron через
# префикс `HHRU_PYTHON=/path/to/venv/bin/python`. Без него падаем на тот же
# python3, что и интерактивный run.sh (совместимость с ручным запуском).
PYTHON_BIN="${HHRU_PYTHON:-python3}"
run_cli() {
  "${PYTHON_BIN}" -m hhru_bot.cli "$@" 2>&1 | tee -a "${LOG_FILE}"
  return "${PIPESTATUS[0]}"
}

run_cli "$@"
