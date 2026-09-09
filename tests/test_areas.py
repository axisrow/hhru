"""Тесты areas: парсинг дерева /areas + fail-closed поиск по имени (D3).

Fixture-данные — подставные, но структурно копируют живой GET /areas
(проверено дампом 2026-09-07): id — СТРОКИ, дети в ключе "areas",
parent_id присутствует (парсер его не использует — цепочка из вложенности),
у листьев есть лишние поля (utc_offset/lat/lng). Реальные area id из
docs/issue-city-search-gaps.md: Набережные Челны 1641 (Татарстан 1624),
Казань 88, Нижнекамск 1642, Елабуга 1633, Верхние Челны 7242,
Старые Челны 7247, Челно-Вершины 3032, Набережный (Пермский край) 13485,
Набережный 11602, Набережное 18147. Узлы 90001/90002/5001 — вымышленные
подставные (второй одноимённый «Набережный», «Новая Казань», область).
"""

import pytest

from hhru_bot.areas import (
    AREA_MATCH_EXACT_MULTIPLE,
    AREA_MATCH_EXACT_UNIQUE,
    AREA_MATCH_NONE,
    AREA_MATCH_PARTIAL_ONLY,
    AreaParent,
    AreaTreeError,
    find_areas,
    parse_area_tree,
)

pytestmark = pytest.mark.unit


def _leaf(node_id: str, name: str, **extra) -> dict:
    return {"id": node_id, "parent_id": None, "name": name, "areas": [], **extra}


def _area_tree() -> list[dict]:
    """Мини-копия дерева /areas: корень «Россия», регионы, города."""
    return [
        {
            "id": "113",
            "parent_id": None,
            "name": "Россия",
            "areas": [
                {
                    "id": "1624",
                    "parent_id": "113",
                    "name": "Республика Татарстан",
                    "areas": [
                        _leaf("88", "Казань", utc_offset="+03:00"),
                        _leaf("90002", "Новая Казань"),
                        _leaf("1641", "Набережные Челны", utc_offset="+03:00", lat=55.7, lng=52.3),
                        _leaf("1642", "Нижнекамск"),
                        _leaf("1633", "Елабуга"),
                        _leaf("7242", "Верхние Челны"),
                        _leaf("7247", "Старые Челны"),
                    ],
                },
                {
                    "id": "1586",
                    "parent_id": "113",
                    "name": "Самарская область",
                    "areas": [_leaf("3032", "Челно-Вершины")],
                },
                {
                    "id": "1317",
                    "parent_id": "113",
                    "name": "Пермский край",
                    "areas": [_leaf("13485", "Набережный (Пермский край)")],
                },
                {
                    "id": "1898",
                    "parent_id": "113",
                    "name": "Орловская область",
                    "areas": [
                        _leaf("11602", "Набережный"),
                        _leaf("90001", "Набережный"),
                    ],
                },
                {
                    "id": "5001",
                    "parent_id": "113",
                    "name": "Подставная область",
                    "areas": [_leaf("18147", "Набережное")],
                },
            ],
        }
    ]


def _parsed():
    return parse_area_tree(_area_tree())


def _by_id(areas, node_id: int):
    return next(a for a in areas if a.id == node_id)


# --- parse_area_tree ---


def test_parse_area_tree_flattens_all_nodes_with_int_ids():
    areas = _parsed()
    assert len(areas) == 18
    assert all(isinstance(a.id, int) for a in areas)
    assert {_by_id(areas, 1641).name, _by_id(areas, 88).name} == {
        "Набережные Челны",
        "Казань",
    }


def test_parse_area_tree_builds_parent_chain_root_to_parent():
    areas = _parsed()
    assert _by_id(areas, 1641).parents == (
        AreaParent(id=113, name="Россия"),
        AreaParent(id=1624, name="Республика Татарстан"),
    )
    assert _by_id(areas, 113).parents == ()


def test_parse_area_tree_rejects_node_without_id():
    tree = [{"name": "Без id", "areas": []}]
    with pytest.raises(AreaTreeError, match="id"):
        parse_area_tree(tree)


def test_parse_area_tree_rejects_non_numeric_id():
    tree = [{"id": "не-число", "name": "X", "areas": []}]
    with pytest.raises(AreaTreeError, match="id"):
        parse_area_tree(tree)


def test_parse_area_tree_rejects_node_without_name():
    tree = [{"id": "1", "areas": []}]
    with pytest.raises(AreaTreeError, match="name"):
        parse_area_tree(tree)


# --- find_areas: fail-closed классификация ---


@pytest.mark.parametrize(
    "query",
    ["набережные челны", "НАБЕРЕЖНЫЕ ЧЕЛНЫ", "  Набережные Челны  "],
)
def test_find_areas_exact_unique_is_case_insensitive(query):
    result = find_areas(_parsed(), query)
    assert result.kind == AREA_MATCH_EXACT_UNIQUE
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.id == 1641
    assert AreaParent(id=1624, name="Республика Татарстан") in match.parents


def test_find_areas_exact_multiple_lists_all_same_name():
    # Два одноимённых «Набережный» — резолвер НЕ выбирает один, а перечисляет оба;
    # частичный кандидат «Набережный (Пермский край)» в точный результат не входит.
    result = find_areas(_parsed(), "набережный")
    assert result.kind == AREA_MATCH_EXACT_MULTIPLE
    assert {a.id for a in result.matches} == {11602, 90001}


def test_find_areas_partial_only_is_substring_without_stemming():
    # «челны» — только substring-семантика: «Челно-Вершины» (3032) НЕ совпадает
    # («челны» не подстрока «челно»), стемминга намеренно нет — fail-closed:
    # частичные кандидаты перечисляются, автоматический выбор отсутствует.
    result = find_areas(_parsed(), "челны")
    assert result.kind == AREA_MATCH_PARTIAL_ONLY
    assert {a.id for a in result.matches} == {1641, 7242, 7247}
    assert 3032 not in {a.id for a in result.matches}


def test_find_areas_exact_wins_over_partial():
    # «казань» точным матчем у 88; «Новая Казань» (90002) содержит запрос,
    # но при наличии точного совпадения частичные кандидаты отбрасываются.
    result = find_areas(_parsed(), "казань")
    assert result.kind == AREA_MATCH_EXACT_UNIQUE
    assert [a.id for a in result.matches] == [88]


def test_find_areas_none_for_unknown_query():
    result = find_areas(_parsed(), "несуществующий-город-xyz")
    assert result.kind == AREA_MATCH_NONE
    assert result.matches == ()


@pytest.mark.parametrize("query", ["", "   "])
def test_find_areas_empty_query_is_none(query):
    # Пустой запрос не «совпадает со всем каталогом» — fail-closed NONE.
    result = find_areas(_parsed(), query)
    assert result.kind == AREA_MATCH_NONE
    assert result.matches == ()


def test_find_areas_every_match_carries_full_parent_chain():
    result = find_areas(_parsed(), "набережное")
    assert result.kind == AREA_MATCH_EXACT_UNIQUE
    match = result.matches[0]
    assert match.id == 18147
    assert [(p.id, p.name) for p in match.parents] == [
        (113, "Россия"),
        (5001, "Подставная область"),
    ]
