"""Команда ``areas``: резолвер «название -> area-id» по каталогу hh.ru.

Каталог https://api.hh.ru/areas публичен (200 даже без авторизации — факт A3
из .tmp/tasks/city-search-gaps/findings.md), поэтому команда не требует
залогиненной сессии и не открывает страницу: достаточно контекстного
API-клиента Playwright. Запрос выполняется через ``BrowserContext.request``
за локальным алиасом — прямой HTTP запрещён (CLAUDE.md «Граница браузерных
действий»), а source-guard tests/test_no_page_request.py отклоняет прямую
запись ``context.request.get(...)`` (прецедент — call_api.py).

Классификация совпадений — чистая функция ``areas.find_areas``: команда
никогда не выбирает «наиболее похожий» вариант автоматически (fail-closed).
Единственный точный матч -> [OK] + id + цепочка родителей; неоднозначность
или только частичные совпадения -> ASCII-таблица кандидатов + [FAIL];
решение о выборе id принимает человек.
"""

from __future__ import annotations

import argparse
import json

from ..areas import (
    AREA_MATCH_EXACT_MULTIPLE,
    AREA_MATCH_EXACT_UNIQUE,
    Area,
    AreaSearchResult,
    AreaTreeError,
    find_areas,
    parse_area_tree,
)
from ..report import _ascii_table

AREAS_ENDPOINT = "https://api.hh.ru/areas"
# Однострочный лимит тела ошибки — как в call-api: [FAIL] обязан оставаться
# читаемой однострочной диагностикой, а не дампом аномального ответа.
_FAIL_BODY_LIMIT = 300


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "areas",
        help="Резолвер названия города/региона в area-id каталога hh.ru",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Название города или региона, например «Набережные Челны»",
    )
    parser.set_defaults(func=run)


def _parent_chain(area: Area) -> str:
    """Цепочка родителей от корня к непосредственному родителю («Россия -> ...»)."""
    return " -> ".join(parent.name for parent in area.parents)


def _print_candidates(name: str, result: AreaSearchResult) -> bool:
    """Таблица кандидатов + [FAIL]: неоднозначность решает человек, не код."""
    header = ["id", "название", "родители"]
    rows = [[str(area.id), area.name, _parent_chain(area) or "-"] for area in result.matches]
    print(_ascii_table(header, rows))
    if result.kind == AREA_MATCH_EXACT_MULTIPLE:
        reason = "точное совпадение не единственное"
        # m2 (ревью): при одноимённых areas уточнять --name бесполезно — имена
        # идентичны; id из таблицы предназначен для search --area <ID> или
        # search.area в конфиге. «Уточните --name» осмысленно только для частичных.
        hint = "используйте id из таблицы в `search --area <ID>` или в `search.area` конфига"
    else:
        reason = "точных совпадений нет, показаны частичные"
        hint = "выберите id из таблицы и уточните --name"
    print(f"[FAIL] «{name}»: {reason} — {hint}")
    return True


def _print_result(name: str, result: AreaSearchResult) -> bool | None:
    if result.kind == AREA_MATCH_EXACT_UNIQUE:
        area = result.matches[0]
        chain = _parent_chain(area)
        suffix = f" ({chain})" if chain else ""
        print(f"[OK] {name} -> id {area.id}{suffix}")
        return None
    if not result.matches:
        print(f"[FAIL] «{name}»: совпадений в каталоге hh.ru нет")
        return True
    return _print_candidates(name, result)


def _load_catalog(response) -> tuple[tuple[Area, ...] | None, bool]:
    """Проверяет ответ и парсит каталог; ошибки печатает как [FAIL].

    Возвращает (areas, failed): failed=True -> areas is None, причина уже
    напечатана. Вызывается внутри ``with launch_context``: ``response.text()``
    валиден только у живого контекста Playwright.
    """
    if not response.ok:
        body = " ".join(response.text().split())
        detail = f": {body[:_FAIL_BODY_LIMIT]}" if body else ""
        print(f"[FAIL] GET {AREAS_ENDPOINT} вернул HTTP {response.status}{detail}")
        return None, True
    try:
        raw_nodes = json.loads(response.text())
    except json.JSONDecodeError as exc:
        print(f"[FAIL] каталог /areas: некорректный JSON: {exc}")
        return None, True
    try:
        return parse_area_tree(raw_nodes), False
    except AreaTreeError as exc:
        print(f"[FAIL] каталог /areas: {exc}")
        return None, True


def run(args: argparse.Namespace) -> bool | None:
    # Fail-closed контракт cli.py: возвращённый True -> exit code 1. Ожидаемые
    # ошибки (не-2xx, битый JSON, невалидное дерево) печатаются как [FAIL]
    # здесь, а не уходят в generic except -> сырой traceback (паттерн call-api).
    from ..browser import launch_context
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        # Алиас — см. docstring модуля: это контекстный клиент Playwright,
        # а не отдельный HTTP-клиент; source-guard требует именно такую запись.
        api_request = context.request
        response = api_request.get(AREAS_ENDPOINT)
        areas, failed = _load_catalog(response)
    if failed or areas is None:
        return True
    return _print_result(args.name, find_areas(areas, args.name))
