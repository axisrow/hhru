"""Контракт: фейки Playwright-объектов в tests/ не должны быть мягче реального API.

Регрессия #354/#371: steps.py вызвал ``page.wait_for_function(expr, selector,
timeout=...)`` — ``arg`` в реальном Playwright API это keyword-only параметр
(после ``*``), позиционная передача падает с TypeError. tests/test_apply_steps.py
мокал ``wait_for_function`` сигнатурой ``(self, _expression, arg=None,
timeout=None)`` — без ``*``, поэтому фейк молча принимал и позиционный, и
именованный вызов и не поймал регрессию до боевого прогона.

Этот тест статически сверяет сигнатуры одноимённых методов в тестовых fake-классах
(tests/*.py) с реальными Page/Locator/ElementHandle/Frame из установленного
Playwright: если параметр в реальном API keyword-only, а в фейке — обычный
(POSITIONAL_OR_KEYWORD), фейк умеет больше, чем настоящий объект, и маскирует
именно такие TypeError. Отдельно запрещён голый ``**kwargs`` на месте
контролируемых параметров — он глушит проверку по построению.

Ложные срабатывания возможны для методов с общими именами, не связанных с
Playwright (напр. свой ``close()``/``evaluate()`` на невизуальном классе) —
такие исключаются точечно через ``_IGNORE`` с комментарием-обоснованием.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from playwright.sync_api import ElementHandle, Frame, Locator, Page

pytestmark = pytest.mark.unit

TESTS_DIR = Path(__file__).parent
PLAYWRIGHT_CLASSES = (Page, Locator, ElementHandle, Frame)

# Методы, реально вызываемые в src/hhru_bot/ на Playwright-объектах (см. #408
# разбор) — контракт держим именно по ним, чтобы не тащить весь необъятный
# Playwright API и не плодить шум по несвязанным именам. Список сверен grep'ом
# по src/hhru_bot/ на методы Page/Locator с keyword-only параметрами в реальном
# API; expect_navigation оставлен несмотря на #179 (заменён на wait_for_url) —
# держим на случай регрессии обратно на него. get_by_text/reload добавлены
# после cycle-review PR #410 round 1 (Codex): оба реально используются в src/ с
# keyword-only параметрами (exact/wait_until) и были пропущены исходным grep'ом.
# get_by_label/get_by_role добавлены после round 2 (Codex) по той же причине —
# оба вызываются в src/ с exact/name как keyword-only.
RELEVANT_METHODS = {
    "check",
    "click",
    "evaluate",
    "expect_navigation",
    "fill",
    "get_attribute",
    "get_by_label",
    "get_by_role",
    "get_by_text",
    "goto",
    "inner_text",
    "input_value",
    "press",
    "reload",
    "screenshot",
    "select_option",
    "text_content",
    "wait_for",
    "wait_for_function",
    "wait_for_load_state",
    "wait_for_selector",
    "wait_for_url",
}

# (файл, класс, метод) — точечные, обоснованные исключения из контракта.
# Пусто по состоянию на #408: ни одного законного случая пока не найдено.
_IGNORE: set[tuple[str, str, str]] = set()


def _real_keyword_only_params() -> dict[str, set[str]]:
    """method_name -> множество имён параметров, keyword-only хотя бы в одном
    из отслеживаемых Playwright-классов (Page/Locator/... могут расходиться
    в деталях — берём union, фейк не должен быть мягче ни для одного из них)."""
    result: dict[str, set[str]] = {}
    for method_name in RELEVANT_METHODS:
        kwonly: set[str] = set()
        for cls in PLAYWRIGHT_CLASSES:
            real_fn = getattr(cls, method_name, None)
            if real_fn is None:
                continue
            sig = inspect.signature(real_fn)
            kwonly |= {
                p.name for p in sig.parameters.values() if p.kind == inspect.Parameter.KEYWORD_ONLY
            }
        if kwonly:
            result[method_name] = kwonly
    return result


REAL_KWONLY = _real_keyword_only_params()


def _iter_fake_methods():
    """Находит все def <method>(self, ...) в классах внутри tests/*.py, где
    <method> входит в RELEVANT_METHODS — кандидаты в фейки Playwright-объектов."""
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == "test_playwright_fakes_contract.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, ast.FunctionDef):
                    continue
                if item.name not in RELEVANT_METHODS:
                    continue
                yield path, node.name, item


def _violations_for(func: ast.FunctionDef, kwonly_in_real: set[str]) -> list[str]:
    """Возвращает список проблем: параметры, которые в реальном API
    keyword-only, а в фейке объявлены как обычные (POSITIONAL_OR_KEYWORD),
    плюс голый **kwargs на месте контролируемых параметров."""
    problems = []

    args = func.args
    # args.args включает self первым элементом
    positional_or_keyword_names = {a.arg for a in args.args[1:]}
    # параметры после func.args.args, объявленные без "*, " перед ними но
    # являющиеся *args — не наш случай (args.vararg отдельно)
    overlap = positional_or_keyword_names & kwonly_in_real
    for name in sorted(overlap):
        problems.append(
            f"параметр '{name}' объявлен как обычный (POSITIONAL_OR_KEYWORD), "
            f"а в реальном Playwright API он keyword-only — фейк принимает "
            f"позиционный вызов, который в бою упадёт с TypeError"
        )

    if args.kwarg is not None and kwonly_in_real:
        # **kwargs сам по себе не запрещён (напр. **_kwargs для screenshot),
        # но если это единственная защита для keyword-only параметров реального
        # метода — он глушит контракт по построению, а не описывает его.
        # Флагуем только если явных keyword-only параметров в фейке нет вовсе
        # (иначе это осознанный catch-all поверх уже описанных имён).
        if not args.kwonlyargs:
            problems.append(
                "метод использует **kwargs вместо явных параметров — "
                "не различает позиционный и именованный вызов, маскирует "
                "несовместимость с реальным Playwright API по построению"
            )

    return problems


def test_fake_playwright_methods_not_more_permissive_than_real_api():
    all_problems: list[str] = []
    checked = 0

    for path, class_name, func in _iter_fake_methods():
        key = (path.name, class_name, func.name)
        if key in _IGNORE:
            continue
        kwonly_in_real = REAL_KWONLY.get(func.name)
        if not kwonly_in_real:
            continue  # метод без keyword-only параметров в реальном API — нечего сверять
        checked += 1
        problems = _violations_for(func, kwonly_in_real)
        for problem in problems:
            all_problems.append(f"{path.name}:{func.lineno} {class_name}.{func.name}() — {problem}")

    assert checked > 0, (
        "Контракт не нашёл ни одного fake-метода для сверки — "
        "вероятно, RELEVANT_METHODS или паттерн поиска классов разошлись "
        "с реальной структурой tests/. Проверь _iter_fake_methods()."
    )
    assert not all_problems, (
        "Найдены фейки Playwright-методов, которые мягче реального API "
        "(позволяют то, что в бою упадёт с TypeError, см. #354/#371/#408):\n"
        + "\n".join(all_problems)
        + "\n\nЕсли расхождение осознанное и безопасное — добавь точечное "
        "исключение в _IGNORE с комментарием-обоснованием, не расширяй "
        "прокладку молча."
    )
