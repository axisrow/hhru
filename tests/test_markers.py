"""Проверяет, что каждый тест явно заявляет свою категорию."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


_ALLOWED = {"unit", "integration", "smoke", "e2e", "live_read", "live_write"}


def test_every_test_file_has_exactly_one_category_marker() -> None:
    tests_dir = Path(__file__).parent
    test_files = sorted((*tests_dir.glob("test_*.py"), tests_dir / "packaging_smoke.py"))
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
                expression = ast.unparse(node.value)
                markers.extend(
                    marker for marker in _ALLOWED if f"pytest.mark.{marker}" in expression
                )
        if len(markers) != 1 or markers[0] not in _ALLOWED:
            missing_or_multiple.append(f"{path.name}: {markers}")
    assert not missing_or_multiple, "\n".join(missing_or_multiple)
