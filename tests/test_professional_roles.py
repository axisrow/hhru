from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

import hhru_bot.professional_roles as professional_roles_module
from hhru_bot.professional_roles import (
    ProfessionalRole,
    ProfessionalRoleCacheError,
    VacancySearchRoleCatalog,
    build_role_choice_prompt,
    load_professional_role_cache,
    parse_role_choice,
    parse_role_queries,
    professional_role_cache_is_stale,
    resolve_explicit_role,
    search_cached_professional_roles,
    validate_vacancy_search_role_catalog,
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


def test_resolve_explicit_role_missing_leaf_lists_offered_labels(monkeypatch):
    """#950: отказ ведёт к цели — перечень листов, предложенных фильтром."""
    monkeypatch.setattr(
        "hhru_bot.professional_roles.search_professional_roles",
        lambda page, queries: [
            ProfessionalRole("148", "Врач", "Медицина"),
            ProfessionalRole("40", "Другое", "Медицина"),
        ],
    )

    with pytest.raises(RuntimeError, match="фильтр предлагает: Врач$") as exc_info:
        resolve_explicit_role(MagicMock(), "Врач-хирург")

    # «Другое» — плейсхолдер, а не повторная цель (#913): в перечне его нет.
    assert "Другое" not in str(exc_info.value)


def _catalog(*, fetched_at: datetime | None = None) -> VacancySearchRoleCatalog:
    return VacancySearchRoleCatalog(
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
    invalid = VacancySearchRoleCatalog(
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
    invalid = VacancySearchRoleCatalog(
        fetched_at=datetime.now(UTC),
        categories=("ИТ", "Пустая"),
        roles=(ProfessionalRole("96", "Программист", "ИТ"),),
    )

    with pytest.raises(ProfessionalRoleCacheError, match="Пустая"):
        validate_vacancy_search_role_catalog(invalid)


def test_cache_staleness_uses_seven_day_boundary():
    fetched = datetime(2026, 8, 1, tzinfo=UTC)
    catalog = _catalog(fetched_at=fetched)

    assert not professional_role_cache_is_stale(catalog, now=fetched + timedelta(days=7))
    assert professional_role_cache_is_stale(catalog, now=fetched + timedelta(days=7, seconds=1))


def test_cached_search_ranks_exact_then_prefix_then_substring_and_ors_queries():
    catalog = VacancySearchRoleCatalog(
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
        "_open_vacancy_search_catalog_dialog",
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

    catalog = professional_roles_module.collect_vacancy_search_role_catalog(object())

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
        "_open_vacancy_search_catalog_dialog",
        lambda _page: (object(), MagicMock()),
    )
    monkeypatch.setattr(professional_roles_module, "_wait_for_tree", lambda *_a: None)
    monkeypatch.setattr(professional_roles_module, "_collect_categories", lambda *_a: categories)
    monkeypatch.setattr(
        professional_roles_module,
        "_collect_category_roles",
        lambda _page, _dialog, category: [ProfessionalRole("8", "Администратор", category.label)],
    )

    catalog = professional_roles_module.collect_vacancy_search_role_catalog(object())

    assert len(catalog.roles) == 1
    assert catalog.roles[0].categories == tuple(category.label for category in categories)


def test_tree_input_parser_rejects_category_rows():
    tree_item = MagicMock()
    tree_item.count.return_value = 1
    tree_item.get_attribute.return_value = "1"
    item = MagicMock()
    item.locator.return_value = tree_item

    assert professional_roles_module._role_from_tree_input(item, category="ИТ") is None


# --- #1004: решение по факту leaf-строк, а не по aria-expanded ----------------


class _LeafRow:
    def locator(self, _selector):
        return _RoleInput()


class _Leaves:
    """Живой счётчик leaf-строк: клик по шеврону меняет состояние."""

    def __init__(self):
        self._n = 0

    def count(self):
        return self._n

    @property
    def first(self):
        return self

    def all(self):
        return [_LeafRow() for _ in range(self._n)]

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self._n == 0:
            raise professional_roles_module.PlaywrightError("leaf-строки не появились")
        return None


class _TreeItem:
    def __init__(self, expanded):
        self._expanded = expanded

    def count(self):
        return 1

    def get_attribute(self, name):  # noqa: ARG002
        return self._expanded


class _Chevron:
    """Клик тогглит состояние дерева (aria-expanded) и строки; ghost=True
    повторяет клин виртуализатора: aria щёлкает, строки не размонтируются."""

    def __init__(self, tree_item, leaves, ghost=False):
        self._tree_item = tree_item
        self._leaves = leaves
        self.ghost = ghost
        self.clicks = 0

    def locator(self, _xpath):
        return self._tree_item

    def click(self):
        self.clicks += 1
        if self._tree_item._expanded == "true":
            self._tree_item._expanded = "false"
            if not self.ghost:
                self._leaves._n = 0
        else:
            self._tree_item._expanded = "true"
            self._leaves._n = 4


class _LyingAttrChevron(_Chevron):
    """Атрибут протух ("true" при свёрнутом дереве): первый клик раскрывает
    по факту и делает атрибут честным, дальше — обычный тоггл."""

    def __init__(self, tree_item, leaves):
        super().__init__(tree_item, leaves)
        self.recovered = False

    def click(self):
        if not self.recovered:
            self.clicks += 1
            self.recovered = True
            self._tree_item._expanded = "true"
            self._leaves._n = 4
        else:
            super().click()


class _Dialog:
    def __init__(self, leaves):
        self._leaves = leaves

    def locator(self, selector):  # noqa: ARG002
        return self._leaves


class _RoleInput:
    def count(self):
        return 1


def _run_collect(category_label="ИТ"):
    leaves = _Leaves()
    chevron = _Chevron(_TreeItem("false"), leaves)
    dialog = _Dialog(leaves)
    page = MagicMock()
    return chevron, leaves, dialog, page


def test_collect_category_expands_and_collapses_by_leaf_fact(monkeypatch):
    from hhru_bot.professional_roles import ProfessionalRoleCategory

    category = ProfessionalRoleCategory("11", "ИТ")
    chevron, leaves, dialog, page = _run_collect()
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)
    monkeypatch.setattr(professional_roles_module, "_tree_scroll", lambda *_a: True)
    monkeypatch.setattr(
        professional_roles_module,
        "_role_from_tree_input",
        lambda _input, category: ProfessionalRole("96", "Программист", category),
    )

    roles = professional_roles_module._collect_category_roles(page, dialog, category)

    assert [r.role_id for r in roles] == ["96"]
    assert chevron.clicks == 2  # раскрытие + схлопывание
    assert leaves.count() == 0  # схлопнута по факту


def test_collect_category_skips_click_when_leaves_already_attached(monkeypatch):
    from hhru_bot.professional_roles import ProfessionalRoleCategory

    category = ProfessionalRoleCategory("11", "ИТ")
    leaves = _Leaves()
    leaves._n = 4  # категория уже раскрыта: строки на месте
    chevron = _Chevron(_TreeItem("true"), leaves)
    dialog = _Dialog(leaves)
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)
    monkeypatch.setattr(professional_roles_module, "_tree_scroll", lambda *_a: True)
    monkeypatch.setattr(
        professional_roles_module,
        "_role_from_tree_input",
        lambda _input, category: ProfessionalRole("96", "Программист", category),
    )

    professional_roles_module._collect_category_roles(MagicMock(), dialog, category)

    assert chevron.clicks == 1  # только схлопывание в конце


def test_collect_category_click_recovers_stale_true_attribute(monkeypatch):
    """aria-expanded="true" при нуле строк — протухший атрибут: клик был,
    факт дождался строк (класс #840)."""
    from hhru_bot.professional_roles import ProfessionalRoleCategory

    category = ProfessionalRoleCategory("11", "ИТ")
    leaves = _Leaves()
    chevron = _LyingAttrChevron(_TreeItem("true"), leaves)
    dialog = _Dialog(leaves)
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)
    monkeypatch.setattr(professional_roles_module, "_tree_scroll", lambda *_a: True)
    monkeypatch.setattr(
        professional_roles_module,
        "_role_from_tree_input",
        lambda _input, category: ProfessionalRole("96", "Программист", category),
    )

    professional_roles_module._collect_category_roles(MagicMock(), dialog, category)

    assert chevron.clicks == 2  # атрибуту не поверили (раскрыли по факту) +
    # схлопывание: после честного клика aria=true и строки на месте


def test_collect_category_honest_failure_when_leaves_never_appear(monkeypatch):

    from hhru_bot.professional_roles import ProfessionalRoleCategory

    category = ProfessionalRoleCategory("11", "ИТ")
    leaves = _Leaves()
    chevron = _Chevron(_TreeItem("false"), leaves)
    dialog = _Dialog(leaves)

    def dead_click(self):  # клик есть, эффекта нет
        chevron.clicks += 1

    chevron.click = dead_click.__get__(chevron)
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)

    with pytest.raises(RuntimeError, match="не раскрылась.*aria-expanded='false'"):
        professional_roles_module._collect_category_roles(MagicMock(), dialog, category)


def test_open_filters_requires_hydration_before_decision(monkeypatch):
    """#858/#1004: до гидрации тоггла «Фильтры» решение не принимается."""
    page = MagicMock()
    monkeypatch.setattr(
        professional_roles_module,
        "wait_for_named_control_hydration",
        lambda *_a, **_k: False,
    )
    with pytest.raises(RuntimeError, match="не гидратировалась"):
        professional_roles_module._open_filters_if_needed(page)
    page.locator.assert_not_called()


def test_open_filters_proceeds_after_hydration(monkeypatch):
    trigger = MagicMock()
    trigger.count.return_value = 1
    trigger.is_visible.return_value = True
    page = MagicMock()
    page.locator.return_value = trigger
    monkeypatch.setattr(
        professional_roles_module,
        "wait_for_named_control_hydration",
        lambda *_a, **_k: True,
    )
    professional_roles_module._open_filters_if_needed(page)
    page.get_by_role.assert_not_called()  # клик по тогглу не понадобился


class _GhostChevron(_Chevron):
    """Клин виртуализатора: aria щёлкает, строки не размонтируются."""

    def __init__(self, tree_item, leaves):
        super().__init__(tree_item, leaves, ghost=True)


class _SlowLeaves(_Leaves):
    """Размонтирование медленнее одного цикла попытки, но в grace-окно
    укладывается (ревью #1008): счётчик падает в ноль с третьего чтения."""

    def __init__(self):
        super().__init__()
        self._reads = 0

    def count(self):
        self._reads += 1
        return 4 if self._reads <= 2 else 0


class _DeadChevron(_Chevron):
    """Клик есть, эффекта нет вообще (атрибут не меняется)."""

    def click(self):
        self.clicks += 1


def test_collapse_wedge_carries_collected_roles(monkeypatch):
    """Живой дрилл 2026-09-06: aria уходит в false, а строки-призраки остаются —
    честный TreeVirtualizationWedge, несущий уже собранные роли."""
    from hhru_bot.professional_roles import (
        ProfessionalRoleCategory,
        TreeVirtualizationWedge,
    )

    leaves = _Leaves()
    leaves._n = 4
    chevron = _GhostChevron(_TreeItem("true"), leaves)
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)

    with pytest.raises(TreeVirtualizationWedge) as exc_info:
        professional_roles_module._collapse_category(
            MagicMock(), _Dialog(leaves), ProfessionalRoleCategory("11", "ИТ"), leaves
        )
    assert "leaf-строк осталось в DOM" in str(exc_info.value)
    assert chevron.clicks == 1


def test_collapse_tolerates_slow_unmount_within_grace_window(monkeypatch):
    """Ревью #1008: быстрое, но не мгновенное размонтирование — не клин;
    grace-окно дожидается факта пустого списка, и wedge не диагностируется."""
    from hhru_bot.professional_roles import ProfessionalRoleCategory

    leaves = _SlowLeaves()
    chevron = _Chevron(_TreeItem("true"), leaves)
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)

    professional_roles_module._collapse_category(
        MagicMock(), _Dialog(leaves), ProfessionalRoleCategory("11", "ИТ"), leaves
    )

    assert chevron.clicks == 1


def test_collapse_collects_roles_through_wedge(monkeypatch):
    """_collect_category_roles не теряет роли категории при клине."""
    from hhru_bot.professional_roles import (
        ProfessionalRoleCategory,
        TreeVirtualizationWedge,
    )

    category = ProfessionalRoleCategory("11", "ИТ")
    leaves = _Leaves()
    leaves._n = 4
    chevron = _GhostChevron(_TreeItem("true"), leaves)
    dialog = _Dialog(leaves)
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)
    monkeypatch.setattr(professional_roles_module, "_tree_scroll", lambda *_a: True)
    monkeypatch.setattr(
        professional_roles_module,
        "_role_from_tree_input",
        lambda _input, category: ProfessionalRole("96", "Программист", category),
    )

    with pytest.raises(TreeVirtualizationWedge) as exc_info:
        professional_roles_module._collect_category_roles(MagicMock(), dialog, category)
    assert [r.role_id for r in exc_info.value.roles] == ["96"]


def test_catalog_loop_recovers_from_wedge_by_reopening_dialog(monkeypatch):
    """Клин → переоткрытие диалога → сбор продолжается, роли сохранены."""
    from hhru_bot.professional_roles import (
        ProfessionalRoleCategory,
        TreeVirtualizationWedge,
    )

    categories = [
        ProfessionalRoleCategory("11", "ИТ"),
        ProfessionalRoleCategory("12", "Менеджмент"),
    ]
    monkeypatch.setattr(
        professional_roles_module,
        "_collect_categories",
        lambda *_a: categories,
    )
    monkeypatch.setattr(professional_roles_module, "_wait_for_tree", lambda *_a: None)
    reopens = []
    search = MagicMock()
    search.fill = lambda _v: reopens.append("fill")

    def fake_open(_page):
        reopens.append("open")
        return object(), search

    monkeypatch.setattr(professional_roles_module, "_open_vacancy_search_catalog_dialog", fake_open)
    calls = []

    def fake_collect(_page, _dialog, category):
        calls.append(category.category_id)
        if category.category_id == "11":
            raise TreeVirtualizationWedge("клин", [ProfessionalRole("96", "Программист", "ИТ")])
        return [ProfessionalRole("104", "Руководитель", "Менеджмент")]

    monkeypatch.setattr(professional_roles_module, "_collect_category_roles", fake_collect)

    catalog = professional_roles_module.collect_vacancy_search_role_catalog(object())

    assert calls == ["11", "12"]
    assert len(reopens) == 4  # старт(open+fill) + переоткрытие(open+fill)
    assert {r.role_id for r in catalog.roles} == {"96", "104"}
    assert catalog.roles[0].categories == ("ИТ",)


def test_collapse_honest_failure_when_attribute_never_flips(monkeypatch):
    from hhru_bot.professional_roles import ProfessionalRoleCategory

    leaves = _Leaves()
    leaves._n = 4
    chevron = _DeadChevron(_TreeItem("true"), leaves)
    monkeypatch.setattr(professional_roles_module, "_find_category", lambda *_a: chevron)

    with pytest.raises(RuntimeError, match="не сворачивается за 3 попытки"):
        professional_roles_module._collapse_category(
            MagicMock(), _Dialog(leaves), ProfessionalRoleCategory("11", "ИТ"), leaves
        )
