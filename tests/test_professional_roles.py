from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

import hhru_bot.professional_roles as professional_roles_module
from hhru_bot.professional_roles import (
    ProfessionalRole,
    ProfessionalRoleCacheError,
    ProfessionalRoleCatalog,
    build_role_choice_prompt,
    load_professional_role_cache,
    parse_role_choice,
    parse_role_queries,
    professional_role_cache_is_stale,
    resolve_explicit_role,
    search_cached_professional_roles,
    validate_professional_role_catalog,
    write_professional_role_cache,
)

pytestmark = pytest.mark.unit


def test_parse_role_queries_deduplicates_and_limits():
    assert parse_role_queries(
        '{"queries":["руководитель разработки","руководитель разработки",'
        '"тимлид","разработка","инженер","лишнее"]}'
    ) == ["руководитель разработки", "тимлид", "разработка", "инженер"]


def test_parse_role_queries_rejects_wrong_shape():
    with pytest.raises(ValueError, match="списком строк"):
        parse_role_queries('{"queries":"повар"}')


def test_role_choice_must_reference_live_catalog_id():
    roles = [ProfessionalRole("104", "Руководитель группы разработки", "ИТ")]

    with pytest.raises(ValueError, match="нет в прочитанном live-каталоге"):
        parse_role_choice('{"role_id":"999","reason":"guess"}', roles)


def test_role_choice_returns_exact_live_item():
    roles = [ProfessionalRole("104", "Руководитель группы разработки", "ИТ")]
    role, reason = parse_role_choice('{"role_id":"104","reason":"команда"}', roles)

    assert role is roles[0]
    assert reason == "команда"
    assert '"role_id": "104"' in build_role_choice_prompt("AI Team Lead", roles)[1]["content"]


def test_resolve_explicit_role_requires_one_normalized_exact_match(monkeypatch):
    monkeypatch.setattr(
        "hhru_bot.professional_roles.search_professional_roles",
        lambda page, queries: [ProfessionalRole("104", "Руководитель группы разработки")],
    )

    role = resolve_explicit_role(MagicMock(), "  руководитель   группы разработки ")

    assert role.role_id == "104"


def test_resolve_explicit_role_rejects_duplicate_labels(monkeypatch):
    monkeypatch.setattr(
        "hhru_bot.professional_roles.search_professional_roles",
        lambda page, queries: [
            ProfessionalRole("1", "Аналитик", "ИТ"),
            ProfessionalRole("2", "Аналитик", "Финансы"),
        ],
    )

    with pytest.raises(RuntimeError, match="совпадений: 2"):
        resolve_explicit_role(MagicMock(), "Аналитик")


def _catalog(*, fetched_at: datetime | None = None) -> ProfessionalRoleCatalog:
    return ProfessionalRoleCatalog(
        fetched_at=fetched_at or datetime(2026, 8, 24, tzinfo=UTC),
        categories=("ИТ", "Менеджмент"),
        roles=(
            ProfessionalRole("96", "Программист, разработчик", "ИТ"),
            ProfessionalRole("104", "Руководитель группы разработки", "Менеджмент"),
        ),
    )


def test_professional_role_cache_round_trip(tmp_path):
    path = tmp_path / "cache" / "professional_roles.json"

    write_professional_role_cache(_catalog(), path)

    assert load_professional_role_cache(path) == _catalog()


def test_invalid_catalog_does_not_replace_existing_cache(tmp_path):
    path = tmp_path / "professional_roles.json"
    write_professional_role_cache(_catalog(), path)
    before = path.read_bytes()
    invalid = ProfessionalRoleCatalog(
        fetched_at=datetime.now(UTC),
        categories=("ИТ",),
        roles=(ProfessionalRole("96", "Программист", "Другая"),),
    )

    with pytest.raises(ProfessionalRoleCacheError, match="неизвестную категорию"):
        write_professional_role_cache(invalid, path)

    assert path.read_bytes() == before


def test_cache_rejects_incompatible_schema(tmp_path):
    path = tmp_path / "professional_roles.json"
    path.write_text('{"schema_version":999}', encoding="utf-8")

    with pytest.raises(ProfessionalRoleCacheError, match="несовместима"):
        load_professional_role_cache(path)


def test_cache_rejects_non_string_role_fields(tmp_path):
    path = tmp_path / "professional_roles.json"
    write_professional_role_cache(_catalog(), path)
    raw = path.read_text(encoding="utf-8").replace('"role_id": "96"', '"role_id": 96')
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ProfessionalRoleCacheError, match="структура"):
        load_professional_role_cache(path)


def test_catalog_validation_rejects_category_without_roles():
    invalid = ProfessionalRoleCatalog(
        fetched_at=datetime.now(UTC),
        categories=("ИТ", "Пустая"),
        roles=(ProfessionalRole("96", "Программист", "ИТ"),),
    )

    with pytest.raises(ProfessionalRoleCacheError, match="Пустая"):
        validate_professional_role_catalog(invalid)


def test_cache_staleness_uses_seven_day_boundary():
    fetched = datetime(2026, 8, 1, tzinfo=UTC)
    catalog = _catalog(fetched_at=fetched)

    assert not professional_role_cache_is_stale(catalog, now=fetched + timedelta(days=7))
    assert professional_role_cache_is_stale(catalog, now=fetched + timedelta(days=7, seconds=1))


def test_cached_search_ranks_exact_then_prefix_then_substring_and_ors_queries():
    catalog = ProfessionalRoleCatalog(
        fetched_at=datetime.now(UTC),
        categories=("ИТ",),
        roles=(
            ProfessionalRole("1", "Ведущий программист", "ИТ"),
            ProfessionalRole("2", "Программист", "ИТ"),
            ProfessionalRole("3", "Программист микроконтроллеров", "ИТ"),
            ProfessionalRole("4", "Аналитик данных", "ИТ"),
        ),
    )

    roles = search_cached_professional_roles(catalog, ["программист", "аналитик данных"], limit=3)

    assert [role.role_id for role in roles] == ["2", "4", "3"]


def test_full_catalog_collection_keeps_category_binding(monkeypatch):
    categories = [
        professional_roles_module.ProfessionalRoleCategory("11", "ИТ"),
        professional_roles_module.ProfessionalRoleCategory("12", "Менеджмент"),
    ]
    search = MagicMock()
    monkeypatch.setattr(
        professional_roles_module,
        "_open_catalog_dialog",
        lambda _page: (object(), search),
    )
    monkeypatch.setattr(professional_roles_module, "_wait_for_tree", lambda *_a: None)
    monkeypatch.setattr(professional_roles_module, "_collect_categories", lambda *_a: categories)
    monkeypatch.setattr(
        professional_roles_module,
        "_collect_category_roles",
        lambda _page, _dialog, category: [
            ProfessionalRole(
                "96" if category.category_id == "11" else "104",
                "Программист" if category.category_id == "11" else "Руководитель",
                category.label,
            )
        ],
    )

    catalog = professional_roles_module.collect_professional_role_catalog(object())

    search.fill.assert_called_once_with("")
    assert catalog.categories == ("ИТ", "Менеджмент")
    assert [role.category for role in catalog.roles] == ["ИТ", "Менеджмент"]


def test_full_catalog_merges_same_role_id_across_categories(monkeypatch):
    categories = [
        professional_roles_module.ProfessionalRoleCategory("1", "Административный персонал"),
        professional_roles_module.ProfessionalRoleCategory("2", "Домашний, обслуживающий персонал"),
    ]
    monkeypatch.setattr(
        professional_roles_module,
        "_open_catalog_dialog",
        lambda _page: (object(), MagicMock()),
    )
    monkeypatch.setattr(professional_roles_module, "_wait_for_tree", lambda *_a: None)
    monkeypatch.setattr(professional_roles_module, "_collect_categories", lambda *_a: categories)
    monkeypatch.setattr(
        professional_roles_module,
        "_collect_category_roles",
        lambda _page, _dialog, category: [ProfessionalRole("8", "Администратор", category.label)],
    )

    catalog = professional_roles_module.collect_professional_role_catalog(object())

    assert len(catalog.roles) == 1
    assert catalog.roles[0].categories == tuple(category.label for category in categories)


def test_tree_input_parser_rejects_category_rows():
    tree_item = MagicMock()
    tree_item.count.return_value = 1
    tree_item.get_attribute.return_value = "1"
    item = MagicMock()
    item.locator.return_value = tree_item

    assert professional_roles_module._role_from_tree_input(item, category="ИТ") is None
