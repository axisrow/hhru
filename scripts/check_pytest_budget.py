#!/usr/bin/env python3
"""Run the complete test suite and enforce its wall-time budget."""

from __future__ import annotations

import subprocess
import sys
import time

# The suite runs under xdist (see the argv below), which puts CI wall time at
# 12-15s and local wall time at ~4s.
#
# The budget is deliberately far above that. This gate measures wall time on a
# shared GitHub runner, so it also captures queue contention: #428 recorded a
# 143s run with zero failing tests while local wall time stayed ~10s. That is
# why the threshold was escalated 20 -> 30 -> 90 -> 240 fix-by-fix.
#
# 60s is the middle ground: a real regression (an accidental O(n^2), a hung
# fixture) blows past it by multiples and is caught far earlier than 240s would
# catch it, while ordinary runner variance stays under it. The gate still
# measures runner noise rather than a property of the code — that design flaw
# is tracked in #438, not worked around here.
SUITE_BUDGET_SECONDS = 60.0


def main() -> int:
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "xdist.plugin",
            "-n",
            "4",
            "--dist",
            "worksteal",
        ],
        check=False,
    )
    elapsed = time.monotonic() - started

    print(f"pytest suite wall time: {elapsed:.2f}s (budget: {SUITE_BUDGET_SECONDS:.0f}s)")
    if result.returncode != 0:
        return result.returncode
    if elapsed > SUITE_BUDGET_SECONDS:
        print("pytest suite exceeded its wall-time budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
