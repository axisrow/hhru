#!/usr/bin/env python3
"""Run the complete test suite and enforce its wall-time budget."""

from __future__ import annotations

import subprocess
import sys
import time

# CI runners have variable startup/load time.  A tight 20s threshold made a
# passing suite fail intermittently (the suite itself is normally ~10-15s).
# Raised 30s -> 90s (#428): the suite grew to 1500+ tests and a loaded shared
# runner took 50-67s wall time with zero failing tests two runs in a row —
# not a regression in test cost (local wall time stayed ~9s), just less
# headroom against runner variance. 90s keeps a real regression detectable
# while giving that variance room.
SUITE_BUDGET_SECONDS = 90.0


def main() -> int:
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], check=False)
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
