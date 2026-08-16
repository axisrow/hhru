"""Privacy-инвариант probe (#8): дампы формы отклика НЕ попадают в git.

Дамп probe (data/logs/probe_*.{png,html}) содержит HTML/скриншот формы отклика
залогиненного пользователя — имя, резюме, контакты. Это тот же класс
чувствительных данных, что storage_state/history (CLAUDE.md: «реальные ссылки
на резюме, сессия и история наружу не попадают»). Тест страхует, что .gitignore
их покрывает — иначе `git add .` после прогона probe утёчёт в коммит.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_probe_dumps_are_gitignored():
    root = _repo_root()
    candidates = [
        "data/logs/probe_42_form.png",
        "data/logs/probe_42_form.html",
    ]
    result = subprocess.run(
        ["git", "check-ignore", *candidates],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    missing = [c for c in candidates if c not in ignored]
    assert not missing, f"probe-дампы не исключены из git: {missing} (privacy-утечка формы отклика)"
