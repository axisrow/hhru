"""Round-trip export→import блоковых секций (#1123): чистые функции, без браузера.

Покрывает: версию схемы (v2 + back-compat v1), честный per-row результат
(фундамент #1118: planned/duplicate/failed), переносимость контактов
(#1119), сертификатов (#1120) и портфолио (#1121) из экспорта в план
импорта, сверку diff_export по блоковым секциям.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hhru_bot.export_resume import (
    EXPORT_SCHEMA,
    EXPORT_SCHEMA_V1,
    build_export_payload,
    parse_block_items,
    parse_certificate_items,
)
from hhru_bot.import_resume import (
    SUPPORTED_EXPORT_SCHEMAS,
    ImportPlanError,
    blocks_outcome_map,
    diff_export,
    is_legacy_export,
    load_export,
    plan_blocks,
    plan_certificates,
    plan_contacts,
    plan_portfolio,
)
from hhru_bot.resume_sections import (
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_PLANNED,
    Certificate,
    PortfolioItem,
)

pytestmark = pytest.mark.unit


def _payload(schema: str = EXPORT_SCHEMA, **extra) -> dict:
    payload = {
        "schema": schema,
        "resume_id": "00001",
        "slug": "main",
        "resume_url": "https://hh.ru/resume/00001",
    }
    payload.update(extra)
    return payload


def _write_export(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "export.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --- Версия схемы и back-compat (#1123 п.2) ----------------------------------


def test_supported_schemas_list_v1_and_v2() -> None:
    assert SUPPORTED_EXPORT_SCHEMAS == (EXPORT_SCHEMA, EXPORT_SCHEMA_V1)
    assert EXPORT_SCHEMA_V1 == "export-resume/v1"
    assert EXPORT_SCHEMA == "export-resume/v2"


def test_load_export_accepts_legacy_v1(tmp_path: Path) -> None:
    """Старый экспорт без новых ключей читается без ошибок и помечается legacy."""
    payload = load_export(_write_export(tmp_path, _payload(schema=EXPORT_SCHEMA_V1)))
    assert is_legacy_export(payload) is True


def test_load_export_accepts_v2(tmp_path: Path) -> None:
    payload = load_export(_write_export(tmp_path, _payload()))
    assert is_legacy_export(payload) is False


def test_load_export_rejects_future_schema(tmp_path: Path) -> None:
    with pytest.raises(ImportPlanError):
        load_export(_write_export(tmp_path, _payload(schema="export-resume/v3")))


def test_plan_blocks_legacy_export_is_empty() -> None:
    """v1: блоковые секции файлом не переносятся — план пуст, без ошибок."""
    blocks = plan_blocks(_payload(schema=EXPORT_SCHEMA_V1))
    assert blocks.legacy_export is True
    assert blocks.plan.contacts == []
    assert blocks.plan.certificates == []
    assert blocks.plan.portfolio == []
    assert blocks.outcomes == []


# --- Экспорт: сертификаты и портфолио ----------------------------------------


def test_parse_certificate_items_extracts_name_year_url() -> None:
    items = parse_certificate_items(
        [
            {
                "id": "1",
                "title": "AWS Solutions Architect",
                "subtitle": "2021",
                "description": None,
                "links": [{"href": "https://aws.amazon.com/verify", "text": "verify"}],
            }
        ]
    )
    assert items == [
        {"name": "AWS Solutions Architect", "year": "2021", "url": "https://aws.amazon.com/verify"}
    ]


def test_parse_certificate_items_falls_back_to_lines() -> None:
    """Структура -title/-subtitle не подтвердилась — имя из строк, без выдумывания."""
    items = parse_certificate_items(
        [
            {
                "id": "2",
                "title": None,
                "subtitle": None,
                "description": None,
                "lines": ["Скрипты и диаграммы 2019"],
            }
        ]
    )
    assert items == [{"name": "Скрипты и диаграммы 2019", "year": "2019", "url": None}]


def test_build_export_payload_certificate_block_becomes_section() -> None:
    raw = {
        "title": {"count": 1, "text": "Инженер"},
        "salary": {"count": 0, "text": None},
        "about": {"count": 0, "text": None},
        "avatar_img": None,
        "expand_button": False,
        "cards": [{"block": "certificate", "text": "Сертификаты"}],
        "items": [
            {
                "block": "certificate",
                "id": "1",
                "title": "Сертификат A",
                "subtitle": "2020",
                "description": None,
                "links": [],
                "text": "Сертификат A 2020",
            }
        ],
        "contacts": [],
        "experience": [],
        "skills": [{"qa": "skill-tag-1", "text": "Python"}],
        "fields": [],
    }
    payload, unavailable = build_export_payload(
        raw,
        resume_id="00002",
        resume_url="https://hh.ru/resume/00002",
        slug="certs",
        portfolio_ids={"914537107"},
    )
    assert payload["schema"] == "export-resume/v2"
    assert payload["certificates"] == [{"name": "Сертификат A", "year": "2020", "url": None}]
    assert payload["portfolio"] == [{"kind": "image", "photo_id": "914537107"}]
    # Секция забрала сырьё себе: в generic blocks сертификатов больше нет.
    assert "certificate" not in payload["blocks"]
    assert not any("сертификаты" in note for note in unavailable)


def test_build_export_payload_without_ssr_marks_portfolio_unavailable() -> None:
    payload, unavailable = build_export_payload(
        {
            "title": {"count": 1, "text": "Инженер"},
            "salary": {"count": 0, "text": None},
            "about": {"count": 0, "text": None},
            "avatar_img": None,
            "expand_button": False,
            "cards": [],
            "items": [],
            "contacts": [],
            "experience": [],
            "skills": [{"qa": "skill-tag-1", "text": "Python"}],
            "fields": [],
        },
        resume_id="00002",
        resume_url="https://hh.ru/resume/00002",
        slug="no-ssr",
        portfolio_ids=None,
    )
    assert payload["portfolio"] == []
    assert any("портфолио" in note and "SSR" in note for note in unavailable)


def test_parse_block_items_keeps_links_only_when_present() -> None:
    parsed = parse_block_items(
        [
            {
                "id": "1",
                "title": "T",
                "subtitle": None,
                "description": None,
                "links": [{"href": "https://example.com", "text": "site"}],
                "text": "T site",
            },
            {
                "id": "2",
                "title": "B",
                "subtitle": None,
                "description": None,
                "links": [],
                "text": "B",
            },
        ]
    )
    assert parsed[0]["links"] == [{"href": "https://example.com", "text": "site"}]
    assert "links" not in parsed[1]


# --- План контактов (#1119-схема) ---------------------------------------------


def test_plan_contacts_transfers_email_and_non_ru_phone() -> None:
    contacts, outcomes, unavailable = plan_contacts(
        _payload(
            contacts=[
                {"type": "email", "preferred": True, "value": "user@example.com", "href": None},
                {"type": "phone", "preferred": False, "value": "+66 123 456 789", "href": None},
            ]
        )
    )
    assert [(c.type, c.value, c.preferred) for c in contacts] == [
        ("email", "user@example.com", True),
        ("phone", "+66 123 456 789", False),
    ]
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED, OUTCOME_PLANNED]
    assert unavailable == []


def test_plan_contacts_skips_ru_phone_with_reason() -> None:
    """RU-номер требует SMS-подтверждения hh.ru: строка пропущена, email перенесён."""
    contacts, outcomes, unavailable = plan_contacts(
        _payload(
            contacts=[
                {"type": "phone", "preferred": False, "value": "+7 916 123-45-67", "href": None},
                {"type": "email", "preferred": False, "value": "user@example.com", "href": None},
            ]
        )
    )
    assert [c.type for c in contacts] == ["email"]
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED]
    assert len(unavailable) == 1
    assert "SMS" in unavailable[0]


def test_plan_contacts_conflicting_preferred_fails_whole_block() -> None:
    """Два preferred — конфликт формы (radio одна): блок целиком не переносится."""
    contacts, outcomes, unavailable = plan_contacts(
        _payload(
            contacts=[
                {"type": "email", "preferred": True, "value": "a@example.com", "href": None},
                {"type": "phone", "preferred": True, "value": "+66 123 456 789", "href": None},
            ]
        )
    )
    assert contacts == []
    assert [o.status for o in outcomes] == [OUTCOME_FAILED, OUTCOME_FAILED]
    assert any("preferred" in note for note in unavailable)


def test_plan_contacts_conflicting_same_type_fails_whole_block() -> None:
    """Два разных значения поля phone: форма одна, «победил» бы невидимый выбор."""
    contacts, outcomes, unavailable = plan_contacts(
        _payload(
            contacts=[
                {"type": "phone", "preferred": False, "value": "+66 111 111 111", "href": None},
                {"type": "phone", "preferred": False, "value": "+66 222 222 222", "href": None},
            ]
        )
    )
    assert contacts == []
    assert [o.status for o in outcomes] == [OUTCOME_FAILED, OUTCOME_FAILED]
    assert any("повторная строка type=phone" in note for note in unavailable)


def test_plan_contacts_marks_identical_repeat_duplicate() -> None:
    contacts, outcomes, _ = plan_contacts(
        _payload(
            contacts=[
                {"type": "email", "preferred": False, "value": "a@example.com", "href": None},
                {"type": "email", "preferred": False, "value": "a@example.com", "href": None},
            ]
        )
    )
    assert len(contacts) == 1
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED, OUTCOME_DUPLICATE]


# --- План сертификатов (#1120-схема) ------------------------------------------


def test_plan_certificates_round_trip() -> None:
    items, outcomes, unavailable = plan_certificates(
        _payload(
            certificates=[
                {"name": "Сертификат A", "year": "2020", "url": "https://a.example/cert"},
                {"name": "Сертификат B", "year": "", "url": ""},
            ]
        )
    )
    assert items == [
        Certificate(name="Сертификат A", year="2020", url="https://a.example/cert"),
        Certificate(name="Сертификат B", year="", url=""),
    ]
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED, OUTCOME_PLANNED]
    assert unavailable == []


def test_plan_certificates_skips_bad_year_and_missing_name() -> None:
    items, outcomes, unavailable = plan_certificates(
        _payload(
            certificates=[
                {"name": "Плохой год", "year": "двадцать", "url": ""},
                {"name": "", "year": "2020", "url": ""},
            ]
        )
    )
    assert items == []
    assert outcomes == []
    assert len(unavailable) == 2
    assert any("не распознан" in note for note in unavailable)
    assert any("без названия" in note for note in unavailable)


def test_plan_certificates_marks_identical_repeat_duplicate() -> None:
    items, outcomes, _ = plan_certificates(
        _payload(
            certificates=[
                {"name": "A", "year": "2020", "url": ""},
                {"name": "A", "year": "2020", "url": ""},
            ]
        )
    )
    assert len(items) == 1
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED, OUTCOME_DUPLICATE]


# --- План портфолио (#1121-схема) ----------------------------------------------


def test_plan_portfolio_image_rows_round_trip() -> None:
    items, outcomes, unavailable = plan_portfolio(
        _payload(
            portfolio=[
                {"kind": "image", "photo_id": "914537107"},
                {"kind": "image", "photo_id": "914537108"},
            ]
        )
    )
    assert items == [
        PortfolioItem(kind="image", photo_id="914537107"),
        PortfolioItem(kind="image", photo_id="914537108"),
    ]
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED, OUTCOME_PLANNED]
    assert unavailable == []


def test_plan_portfolio_link_rows_are_not_supported() -> None:
    """Link-строки (portfolioUrls) UI hh.ru не имеет (#1121) — честный пропуск."""
    items, outcomes, unavailable = plan_portfolio(
        _payload(
            portfolio=[
                {"kind": "link", "title": "Behance", "url": "https://behance.net/x"},
                {"kind": "image", "photo_id": "914537107"},
            ]
        )
    )
    assert [i.photo_id for i in items] == ["914537107"]
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED]
    assert len(unavailable) == 1
    assert "image" in unavailable[0]


def test_plan_portfolio_rejects_non_numeric_photo_id() -> None:
    items, _, unavailable = plan_portfolio(
        _payload(portfolio=[{"kind": "image", "photo_id": "abc"}])
    )
    assert items == []
    assert any("photo_id" in note for note in unavailable)


# --- Полный round-trip: экспорт → план импорта ---------------------------------


def test_round_trip_export_to_blocks_plan_preserves_rows() -> None:
    """Экспорт v2 с блоками → plan_blocks: строки плана соответствуют данным."""
    raw = {
        "title": {"count": 1, "text": "Инженер"},
        "salary": {"count": 0, "text": None},
        "about": {"count": 0, "text": None},
        "avatar_img": None,
        "expand_button": False,
        "cards": [{"block": "certificate", "text": "Сертификаты"}],
        "items": [
            {
                "block": "certificate",
                "id": "1",
                "title": "Сертификат A",
                "subtitle": "2020",
                "description": None,
                "links": [],
                "text": "Сертификат A 2020",
            }
        ],
        "contacts": [
            {
                "qa": "resume-contact-email-value-preferred",
                "text": "user@example.com",
                "href": "mailto:user@example.com",
            }
        ],
        "experience": [],
        "skills": [{"qa": "skill-tag-1", "text": "Python"}],
        "fields": [],
    }
    payload, _ = build_export_payload(
        raw,
        resume_id="00001",
        resume_url="https://hh.ru/resume/00001",
        slug="rt",
        portfolio_ids={"111", "222"},
    )
    blocks = plan_blocks(payload)
    assert blocks.legacy_export is False
    assert [(c.type, c.value, c.preferred) for c in blocks.plan.contacts] == [
        ("email", "user@example.com", True)
    ]
    assert [c.name for c in blocks.plan.certificates] == ["Сертификат A"]
    assert sorted(i.photo_id for i in blocks.plan.portfolio) == ["111", "222"]
    assert all(o.status == OUTCOME_PLANNED for o in blocks.outcomes)
    assert not any("контакты" in note or "сертификаты" in note for note in blocks.unavailable)


def test_blocks_outcome_map_excludes_duplicates_and_aligns_plan() -> None:
    blocks = plan_blocks(
        _payload(
            certificates=[
                {"name": "A", "year": "", "url": ""},
                {"name": "A", "year": "", "url": ""},
            ],
            portfolio=[{"kind": "image", "photo_id": "1"}],
        )
    )
    outcome_map = blocks_outcome_map(blocks)
    # В карте — только строки плана (duplicate отсеян), длины совпадают с планом.
    assert len(outcome_map["certificates"]) == len(blocks.plan.certificates) == 1
    assert len(outcome_map["portfolio"]) == len(blocks.plan.portfolio) == 1
    assert len(outcome_map["contacts"]) == len(blocks.plan.contacts) == 0
    assert [o.status for o in blocks.outcomes if o.block == "certificates"] == [
        OUTCOME_PLANNED,
        OUTCOME_DUPLICATE,
    ]


# --- Сверка diff_export по блокам (#1123 п.3) ----------------------------------


def test_diff_export_blocks_match_when_transferred() -> None:
    source = _payload(
        certificates=[{"name": "Сертификат A", "year": "2020", "url": ""}],
        contacts=[{"type": "email", "preferred": True, "value": "user@example.com", "href": None}],
        portfolio=[{"kind": "image", "photo_id": "111"}],
    )
    imported = _payload(
        certificates=[{"name": "сертификат a", "year": "2020", "url": ""}],
        contacts=[{"type": "email", "preferred": True, "value": "User@example.com", "href": None}],
        # photo_id между аккаунтами другие — сверяется число работ.
        portfolio=[{"kind": "image", "photo_id": "999"}],
    )
    assert diff_export(source, imported, include_blocks=True) == []


def test_diff_export_blocks_reports_discrepancies() -> None:
    source = _payload(
        certificates=[{"name": "A", "year": "2020", "url": ""}],
        contacts=[{"type": "email", "preferred": False, "value": "a@example.com", "href": None}],
        portfolio=[{"kind": "image", "photo_id": "111"}, {"kind": "image", "photo_id": "222"}],
    )
    imported = _payload(
        certificates=[],
        contacts=[],
        portfolio=[{"kind": "image", "photo_id": "333"}],
    )
    diffs = diff_export(source, imported, include_blocks=True)
    assert any(d.startswith("сертификаты:") for d in diffs)
    assert any(d.startswith("контакты:") for d in diffs)
    assert any(d.startswith("портфолио: работ 2 != 1") for d in diffs)


def test_diff_export_without_blocks_flag_keeps_legacy_behaviour() -> None:
    """v1-импорт: блоковые секции не сверяются — старое поведение без шума."""
    source = _payload(
        contacts=[{"type": "email", "preferred": False, "value": "a@example.com", "href": None}],
        certificates=[{"name": "A", "year": "2020", "url": ""}],
    )
    imported = _payload()
    assert diff_export(source, imported, include_blocks=False) == []


def test_diff_export_phone_mask_normalization() -> None:
    """Телефон сверяется по национальным цифрам: маска hh.ru переформатирует."""
    source = _payload(
        contacts=[{"type": "phone", "preferred": False, "value": "+66 123 456 789", "href": None}]
    )
    imported = _payload(
        contacts=[
            {"type": "phone", "preferred": False, "value": "+66 (123) 456-78-9", "href": None}
        ]
    )
    assert diff_export(source, imported, include_blocks=True) == []
