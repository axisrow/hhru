"""Проверяет, что каждый тест явно заявляет свою категорию."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


_ALLOWED = {
    "unit",
    "integration",
    "smoke",
    "e2e",
    "live_read",
    "live_write",
    "live_write_danger",
}


def test_every_test_file_has_exactly_one_category_marker() -> None:
    tests_dir = Path(__file__).parent
    test_files = sorted((*tests_dir.rglob("test_*.py"), tests_dir / "packaging_smoke.py"))
    missing_or_multiple: list[str] = []
    for path in test_files:
        tree = ast.parse(path.read_text(), filename=str(path))
        markers = []
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "pytestmark"
            ):
                markers.extend(
                    candidate.attr
                    for candidate in ast.walk(node.value)
                    if (
                        isinstance(candidate, ast.Attribute)
                        and candidate.attr in _ALLOWED
                        and isinstance(candidate.value, ast.Attribute)
                        and candidate.value.attr == "mark"
                        and isinstance(candidate.value.value, ast.Name)
                        and candidate.value.value.id == "pytest"
                    )
                )
        if len(markers) != 1 or markers[0] not in _ALLOWED:
            missing_or_multiple.append(f"{path.name}: {markers}")
    assert not missing_or_multiple, "\n".join(missing_or_multiple)
