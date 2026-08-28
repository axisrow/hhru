#!/usr/bin/env bash
# Обёртка запуска по расписанию для внешнего планировщика ОС (launchd/cron).
# Этап 6 «автопилот» (#18). Сама обёртка НЕ демон — её вызывает планировщик в
# нужное время. CLAUDE.md запрещает фоновые демоны внутри проекта.
#
# CLI резолвится через editable install (pip install -e .) site-packages,
# без подстановки PYTHONPATH на локальную копию src рядом со скриптом.
# Дополнительно: пишет свой вывод в data/logs/scheduled.log и вызывает bump + apply.
# Предохранители (дневные лимиты, кулдаун бампа 4ч) живут в коде — throttle.py.
#
# Аргументы передаются дальше CLI как есть, например:
#   scripts/scheduled_run.sh bump --headless
#   scripts/scheduled_run.sh apply --headless --limit 5
# Для именованного аккаунта передайте глобальный флаг до команды:
#   scripts/scheduled_run.sh --account marketing --headless apply --limit 5
# Либо задайте HHRU_ACCOUNT=marketing; явный --account в аргументах имеет
# приоритет, если заданы оба варианта.
# Планировщик обычно зовёт одно действие за раз (см. deploy/*.plist шаблоны и
# вывод `hhru-bot schedule`).
#
# `responses --alert-new` (#707/#708, эпик #704): при новых приглашениях CLI
# возвращает CommandExitCode.NEW_INVITATIONS (10). Обёртка распознаёт этот
# код, пишет заметную строку в scheduled.log и опционально вызывает
# пользовательский хук HHRU_ALERT_CMD (см. ниже) — строго opt-in, дефолт
# ничего не делает:
#   scripts/scheduled_run.sh --headless responses --alert-new
#   HHRU_ALERT_CMD='terminal-notifier -message "Новое приглашение hh.ru"' \
#     scripts/scheduled_run.sh --headless responses --alert-new

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# CLI резолвит data/config.yaml, data/history.db и data/logs/ ОТНОСИТЕЛЬНО cwd
# (см. cli.py: DEFAULT_CONFIG_PATH = Path("data")/"config.yaml"). launchd и
# cron НЕ гарантируют cwd = корень репо (часто это / или $HOME) — без этого cd
# плановый джоб не найдёт конфиг или создаст data/ в системном cwd, ломая
# дедупликацию и throttle. Явно переходим в корень репозитория.
cd "${PROJECT_ROOT}"

LOG_DIR="${PROJECT_ROOT}/data/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/scheduled.log"

# Метка запуска в логе: дата + что запускаем (без Date в самой обёртке —
# date(1) внешняя, не запрещена скрипту оболочки; запрет на Date.now() касается
# только JS workflow-скриптов).
{
  echo "----- $(date '+%Y-%m-%d %H:%M:%S') scheduled_run: $* -----"
} >> "${LOG_FILE}"

# Запускаем CLI, вывод записывается в scheduled.log. tee без буферизации (-a —
# дозапись, чтобы история запусков копировалась). exit-код пробрасываем дальше,
# чтобы планировщик видел упавший прогон.
#
# Интерпретатор: launchd/cron имеют УРЕЗАННЫЙ PATH и НЕ активируют виртуальное
# окружение проекта (.venv), поэтому голый `python3` резолвится в системный
# /usr/bin/python3 без playwright → ModuleNotFoundError до любого действия.
# Обёртка читает HHRU_PYTHON (абсолютный путь к python из venv) — его задаёт
# launchd-агент через EnvironmentVariables (см. deploy/*.plist) или cron через
# префикс `HHRU_PYTHON=/path/to/venv/bin/python`. Без него падаем на тот же
# python3, что и интерактивный run.sh (совместимость с ручным запуском).
# Без PYTHONPATH=src: код резолвится из site-packages editable install.
PYTHON_BIN="${HHRU_PYTHON:-python3}"
# Keep synchronized with CommandExitCode.SESSION_EXPIRED in exit_codes.py.
SESSION_EXPIRED_EXIT_CODE=78
# Keep synchronized with CommandExitCode.NEW_INVITATIONS in exit_codes.py.
# Возвращается только `responses --alert-new` (#707/#708, эпик #704) — на
# остальные команды/коды этот код не завязан.
NEW_INVITATIONS_EXIT_CODE=10
run_cli() {
  "${PYTHON_BIN}" -m hhru_bot.cli "$@" 2>&1 | tee -a "${LOG_FILE}"
  return "${PIPESTATUS[0]}"
}

ACCOUNT_ARGS=()
if [[ -n "${HHRU_ACCOUNT:-}" ]]; then
  ACCOUNT_ARGS=(--account "${HHRU_ACCOUNT}")
fi

set +e
run_cli "${ACCOUNT_ARGS[@]}" "$@"
status=$?
set -e

if [[ "${status}" -eq "${SESSION_EXPIRED_EXIT_CODE}" ]]; then
  echo "[SESSION_EXPIRED] Сессия hh.ru истекла; выполните: hhru login или hhru refresh-token" \
    | tee -a "${LOG_FILE}"
fi

if [[ "${status}" -eq "${NEW_INVITATIONS_EXIT_CODE}" ]]; then
  echo "***** НОВОЕ ПРИГЛАШЕНИЕ *****" | tee -a "${LOG_FILE}"
  # Опциональный пользовательский хук — строго opt-in, дефолт "ничего не
  # вызывать" (инвариант issue #708). Никакой встроенной отправки
  # почты/пушей в самом репозитории.
  if [[ -n "${HHRU_ALERT_CMD:-}" ]]; then
    echo "[ALERT_HOOK] Вызываю HHRU_ALERT_CMD" | tee -a "${LOG_FILE}"
    set +e
    eval "${HHRU_ALERT_CMD}" >> "${LOG_FILE}" 2>&1
    set -e
  fi
fi

exit "${status}"
