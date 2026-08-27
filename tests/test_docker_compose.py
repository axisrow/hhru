"""Regression checks for the Docker Compose runner (#719)."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

COMPOSE_FILE = Path(__file__).parents[1] / "docker-compose.yml"


def _service() -> dict:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    return compose["services"]["hhru"]


def test_chromium_has_a_larger_shared_memory_segment():
    assert _service()["shm_size"] == "1gb"


def test_compose_loop_sleeps_until_the_next_run_mark():
    command = "\n".join(_service()["command"])

    assert "started_at=$$(date +%s)" in command
    assert "elapsed=$$(($$(date +%s) - started_at))" in command
    assert "remaining=$$((14400 - elapsed))" in command
    assert 'sleep "$$remaining"' in command
    assert "sleep 14400" not in command
