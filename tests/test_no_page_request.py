"""Архитектурный страж: действия hh.ru идут только через UI."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_source_contains_no_direct_page_request_calls() -> None:
    """Не возвращать прямые HTTP-вызовы в браузерный слой."""
    src = Path(__file__).parents[1] / "src" / "hhru_bot"
    violations: list[str] = []
    exceptions: set[str] = set()  # Explicit allow-list: currently none.
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            request = node.func.value
            if not isinstance(request, ast.Attribute) or request.attr != "request":
                continue
            key = f"{path}:{node.lineno}"
            if key not in exceptions:
                violations.append(f"{path.name}:{node.lineno}: page.request.{node.func.attr}")
    assert not violations, "\n".join(violations)
