"""География hh.ru: чистый парсинг дерева /areas и fail-closed поиск по имени.

Модуль D3 (issue-city-search-gaps, проблема 4): резолвер «название города ->
area id» для флагов вроде ``search --area``. Источник данных — GET /areas
(hh.ru API, ~2.4 МБ JSON): дерево узлов
``{"id": "1641", "parent_id": "1624", "name": "Набережные Челны", "areas": [...]}``
— id строковые, дети в ключе ``areas``, у листьев дополнительные поля
(utc_offset/lat/lng), корень «Россия». I/O в модуле нет намеренно: сырой JSON
передаёт вызывающий код (команда ``areas``), прецедент чистого модуля —
``catalog_preflight.evaluate_leaf``.

Fail-closed поиск: одноимённые и похожие названия в каталоге hh.ru — норма
(«Набережный» есть в Орловской области и Пермском крае, рядом с «Набережные
Челны» живут «Верхние Челны», «Старые Челны», «Набережное»), поэтому
``find_areas`` КЛАССИФИЦИРУЕТ результат (exact_unique / exact_multiple /
partial_only / none) и никогда не выбирает «наиболее похожий» автоматически —
выбор делает человек по явному id. Поле ``parent_id`` парсером не
используется: цепочка предков строится из фактической вложенности при обходе
(вложенность — авторитет, parent_id может ей противоречить).

Substring-семантика частичного матча без стемминга намеренна: «челны» НЕ
совпадает с «Челно-Вершины», и это честный partial-кандидатный список —
лучше перечислить меньше и не угадывать лишнего (принцип fail-closed).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Классификация результата поиска (fail-closed словарь, Upper-snake константы).
AREA_MATCH_EXACT_UNIQUE = "exact_unique"
AREA_MATCH_EXACT_MULTIPLE = "exact_multiple"
AREA_MATCH_PARTIAL_ONLY = "partial_only"
AREA_MATCH_NONE = "none"

_CHILDREN_KEY = "areas"


class AreaTreeError(ValueError):
    """Дерево /areas не соответствует ожидаемой форме (без догадок)."""


@dataclass(frozen=True)
class AreaParent:
    """Предок территории: id + имя (элемент цепочки от корня к родителю)."""

    id: int
    name: str


@dataclass(frozen=True)
class Area:
    """Плоское представление узла каталога: id, имя, цепочка предков."""

    id: int
    name: str
    parents: tuple[AreaParent, ...]


@dataclass(frozen=True)
class AreaSearchResult:
    """Вердикт поиска: вид (AREA_MATCH_*) и ВСЕ кандидаты без автовыбора."""

    kind: str
    matches: tuple[Area, ...]


def parse_area_tree(raw_nodes: Iterable[Mapping]) -> tuple[Area, ...]:
    """Развернуть JSON-дерево /areas в плоский список с цепочками родителей.

    Валидация на границе (fail-closed): узел без числового ``id`` или без
    непустого ``name`` — :class:`AreaTreeError`, а не пропуск узла: молча
    потерянная территория означала бы недостоверный «none» при поиске.
    """
    flat: list[Area] = []
    _walk_area_nodes(raw_nodes, parents=(), flat=flat)
    return tuple(flat)


def _walk_area_nodes(
    nodes: Iterable[Mapping],
    parents: tuple[AreaParent, ...],
    flat: list[Area],
) -> None:
    for node in nodes:
        if not isinstance(node, Mapping):
            raise AreaTreeError(
                f"узел дерева должен быть объектом JSON, получен {type(node).__name__}"
            )
        area_id = _parse_node_id(node)
        name = _parse_node_name(node, area_id)
        flat.append(Area(id=area_id, name=name, parents=parents))
        children = node.get(_CHILDREN_KEY) or []
        if isinstance(children, (str, bytes)) or not isinstance(children, Iterable):
            raise AreaTreeError(f"узел {area_id} ({name!r}): '{_CHILDREN_KEY}' должен быть списком")
        _walk_area_nodes(children, parents + (AreaParent(id=area_id, name=name),), flat)


def _parse_node_id(node: Mapping) -> int:
    raw_id = node.get("id")
    try:
        return int(str(raw_id).strip())
    except (TypeError, ValueError):
        raise AreaTreeError(f"узел дерева: id должен быть числом, получен {raw_id!r}") from None


def _parse_node_name(node: Mapping, area_id: int) -> str:
    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AreaTreeError(f"узел {area_id}: name должен быть непустой строкой, получен {name!r}")
    return name.strip()


def find_areas(areas: Iterable[Area], query: str) -> AreaSearchResult:
    """Регистронезависимый поиск территории с fail-closed классификацией.

    Точные совпадения (casefold-равенство имён) имеют приоритет над частичными
    (substring); единственный точный матч — ``exact_unique``, несколько —
    ``exact_multiple`` (одноимённые перечисляются ВСЕ). Пустой запрос —
    ``none``: пустая строка не должна «совпадать» со всем каталогом.
    Результат никогда не сужается до одного «наиболее похожего» кандидата.
    """
    needle = query.strip().casefold()
    if not needle:
        return AreaSearchResult(kind=AREA_MATCH_NONE, matches=())
    pool = tuple(areas)
    exact = tuple(a for a in pool if a.name.casefold() == needle)
    if len(exact) == 1:
        return AreaSearchResult(kind=AREA_MATCH_EXACT_UNIQUE, matches=exact)
    if exact:
        return AreaSearchResult(kind=AREA_MATCH_EXACT_MULTIPLE, matches=exact)
    partial = tuple(a for a in pool if needle in a.name.casefold())
    if partial:
        return AreaSearchResult(kind=AREA_MATCH_PARTIAL_ONLY, matches=partial)
    return AreaSearchResult(kind=AREA_MATCH_NONE, matches=())
