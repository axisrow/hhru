from unittest.mock import MagicMock

import pytest

from hhru_bot.professional_roles import (
    ProfessionalRole,
    build_role_choice_prompt,
    parse_role_choice,
    parse_role_queries,
    resolve_explicit_role,
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
