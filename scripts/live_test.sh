#!/bin/sh
set -eu
exec pytest -m live_write_danger "$@"
