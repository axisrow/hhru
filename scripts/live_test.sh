#!/bin/sh
set -eu
if [ "${1:-}" = "--dry-run" ]; then
    echo "[dry-run] канал ASK работает; ничего не запускаю"
    exit 0
fi
exec pytest -m live_write_danger "$@"
