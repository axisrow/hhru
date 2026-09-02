#!/usr/bin/env bash
# Запуск браузера с уже загруженным расширением extensions/hhru-live (MVP
# issue #588) — альтернатива ручному chrome://extensions -> Load unpacked.
#
# Как это работает: тумблер Developer Mode через CLI не включается, но он и
# не нужен — распакованный плагин подгружается флагом запуска
# --load-extension=<путь>. Браузер — небрендированный Chromium из кэша
# Playwright (Chrome for Testing): в брендированном Google Chrome с ~137
# флаг --load-extension в stable отключён, в CfT работает. Запуск напрямую
# бинарником с отдельным --user-data-dir: если Chrome уже запущен, флаги
# через `open --args` молча игнорируются.
#
# Это НЕ Playwright-канал: никакого драйвера и автокликов — просто запускается
# браузер, в котором пользователь работает руками; расширение делает только
# то, что разрешено его allowlist (см. extensions/hhru-live/README.md).
#
# Профиль кладётся в data/extension-profile (data/ целиком в .gitignore).
# Сессия hh.ru в этом профиле своя: при первом запуске войдите вручную.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_DIR="$REPO_ROOT/extensions/hhru-live"
PROFILE_DIR="$REPO_ROOT/data/extension-profile"
URL="${1:-https://hh.ru/}"

find_binary() {
  local candidates=()
  # Chrome for Testing из кэша Playwright, старшие версии первыми.
  local dirs=()
  local dir
  for dir in "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac*/; do
    [ -d "$dir" ] || continue
    dirs+=("$dir")
  done
  for dir in $(printf '%s\n' "${dirs[@]}" | sort -rV); do
    candidates+=("$dir/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    candidates+=("$dir/chrome-mac/Chromium.app/Contents/MacOS/Chromium")
  done
  # Системный Chromium (НЕ брендированный Google Chrome).
  candidates+=("/Applications/Chromium.app/Contents/MacOS/Chromium")
  candidates+=("$(command -v chromium 2>/dev/null || true)")
  local candidate
  for candidate in "${candidates[@]}"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if [ ! -f "$EXT_DIR/manifest.json" ]; then
  echo "[FAIL] manifest не найден: $EXT_DIR" >&2
  exit 1
fi

BINARY="$(find_binary)" || {
  echo "[FAIL] не найден небрендированный Chromium." >&2
  echo "       Установите: python3 -m playwright install chromium" >&2
  echo "       (брендированный Google Chrome не подходит: --load-extension отключён)" >&2
  exit 1
}

echo "[INFO] Браузер: $BINARY"
echo "[INFO] Расширение: $EXT_DIR"
echo "[INFO] Профиль: $PROFILE_DIR"
mkdir -p "$PROFILE_DIR"

# exec: браузер становится процессом скрипта; Ctrl-C в терминале закрывает его.
exec "$BINARY" \
  --user-data-dir="$PROFILE_DIR" \
  --load-extension="$EXT_DIR" \
  --no-first-run \
  --no-default-browser-check \
  "$URL"
