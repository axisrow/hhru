"""Unit-тесты read-only экспорта резюме (#1023): чистые парсеры без браузера."""

from __future__ import annotations

import pytest

from hhru_bot.export_resume import (
    _photo_id_from_url,
    _sniff_image_kind,
    build_export_payload,
    parse_block_items,
    parse_contacts,
    parse_experience_company,
)

pytestmark = pytest.mark.unit


def test_parse_contacts_prefers_parent_row_and_marks_preferred() -> None:
    rows = [
        {"qa": "resume-contact-phone", "text": "+7 000 000-00-00", "href": None},
        {
            "qa": "resume-contact-phone-value-preferred",
            "text": "+7 000 000-00-00",
            "href": "tel:+70000000000",
        },
        {"qa": "resume-contact-email-value", "text": "user@example.com", "href": None},
    ]
    contacts = parse_contacts(rows)
    assert contacts == [
        {
            "type": "email",
            "preferred": False,
            "value": "user@example.com",
            "href": None,
        },
        {
            "type": "phone",
            "preferred": True,
            "value": "+7 000 000-00-00",
            "href": "tel:+70000000000",
        },
    ]


def test_parse_experience_company_splits_header_and_steps() -> None:
    card = {
        "header": ['ООО "Пример"', "3 года и 1 месяц"],
        "steps": [
            [
                "Специалист",
                "Октябрь 2018 — Октябрь 2021 (3 года и 1 месяц)",
                "Запуск рекламы",
                "Аналитика",
            ],
            ["Маркетолог", "Май 2017 — Сентябрь 2018 (1 год и 5 месяцев)"],
        ],
    }
    parsed = parse_experience_company(card)
    assert parsed["company"] == 'ООО "Пример"'
    assert parsed["duration"] == "3 года и 1 месяц"
    assert parsed["positions"][0] == {
        "title": "Специалист",
        "period": "Октябрь 2018 — Октябрь 2021 (3 года и 1 месяц)",
        "description": "Запуск рекламы\nАналитика",
    }
    assert parsed["positions"][1]["description"] is None


def test_parse_block_items_keeps_raw_text_when_structure_misses() -> None:
    items = [
        {
            "id": "104607122",
            "title": "Университет",
            "subtitle": "Специалист",
            "description": "2014 · Высшее",
        },
        {
            "id": "42",
            "title": None,
            "subtitle": None,
            "description": None,
            "text": "Школа № 1\n2010 · Высшее",
        },
    ]
    parsed = parse_block_items(items)
    assert parsed[0] == {
        "id": "104607122",
        "title": "Университет",
        "subtitle": "Специалист",
        "description": "2014 · Высшее",
    }
    assert parsed[1]["raw_text"] == "Школа № 1\n2010 · Высшее"
    assert parsed[1]["lines"] == ["Школа № 1", "2010 · Высшее"]


def _full_raw() -> dict:
    return {
        "title": {"count": 1, "text": "Инженер"},
        "salary": {"count": 1, "text": "200 000 руб."},
        "about": {"count": 1, "text": "О себе: пример"},
        "avatar_img": None,
        "expand_button": False,
        "cards": [
            {"block": "education", "text": "Образование"},
            {"block": "recommendation", "text": "Рекомендации"},
        ],
        "items": [
            {
                "block": "education",
                "id": "104607122",
                "title": "Университет",
                "subtitle": "Инженер",
                "description": "2014 · Высшее",
                "text": "Университет Инженер 2014 · Высшее",
            },
            {
                "block": "recommendation",
                "id": "30308171",
                "title": "Иван Иванов",
                "subtitle": "Директор",
                "description": 'ООО "Пример"',
                "text": "Иван Иванов Директор",
            },
        ],
        "contacts": [{"qa": "resume-contact-email", "text": "user@example.com", "href": None}],
        "experience": [
            {
                "header": ['ООО "Пример"', "1 год"],
                "steps": [["Инженер", "2020 — 2021 (1 год)", "Делал проекты"]],
            }
        ],
        "skills": [{"qa": "skill-tag-674", "text": "JavaScript"}],
        "fields": [
            {"qa": "resume-position-field-employmentForms", "text": "Тип занятости: Полная"}
        ],
    }


def test_build_export_payload_routes_known_sections() -> None:
    payload, unavailable = build_export_payload(
        _full_raw(), resume_id="00001", resume_url="https://hh.ru/resume/00001", slug="test"
    )
    assert payload["schema"] == "export-resume/v1"
    assert payload["resume_id"] == "00001"
    assert payload["position"] == {
        "title": "Инженер",
        "salary_text": "200 000 руб.",
        "fields": [{"field": "employmentForms", "text": "Тип занятости: Полная"}],
    }
    assert payload["experience"]["companies"][0]["company"] == 'ООО "Пример"'
    assert payload["education"][0]["title"] == "Университет"
    assert payload["blocks"]["recommendation"][0]["title"] == "Иван Иванов"
    assert payload["skills"] == [{"id": "674", "name": "JavaScript"}]
    assert payload["languages"] == []
    assert payload["about"] == "О себе: пример"
    # Честные пропуски: языков/портфолио на странице не было.
    joined = "\n".join(unavailable)
    assert "языки" not in joined
    assert "портфолио" not in joined


def test_build_export_payload_honest_skips_without_invented_values() -> None:
    payload, unavailable = build_export_payload(
        {
            "title": {"count": 0, "text": None},
            "salary": {"count": 0, "text": None},
            "about": {"count": 2, "text": None},
            "avatar_img": None,
            "expand_button": True,
            "cards": [],
            "items": [],
            "contacts": [],
            "experience": [],
            "skills": [],
            "fields": [],
        },
        resume_id="00002",
        resume_url="https://hh.ru/resume/00002",
        slug="empty",
    )
    assert payload["position"]["title"] is None
    assert payload["position"]["salary_text"] is None
    assert payload["about"] is None
    assert payload["experience"]["companies"] == []
    assert payload["skills"] == []
    assert payload["contacts"] == []
    joined = "\n".join(unavailable)
    assert "позиция" in joined
    assert "зарплата" in joined
    assert "неоднозначный DOM" in joined  # about count=2
    assert "опыт работы" in joined
    assert "навыки" in joined
    assert "контакты" in joined
    assert "Развернуть" in joined


def test_sniff_image_kind_and_photo_id() -> None:
    assert _sniff_image_kind(b"\xff\xd8\xffrest") == "jpeg"
    assert _sniff_image_kind(b"\x89PNG\r\n\x1a\nrest") == "png"
    assert _sniff_image_kind(b"<html>not an image</html>") is None
    assert _photo_id_from_url("https://img.hhcdn.ru/photo/914537107.png?t=1&h=x") == "914537107"
    assert _photo_id_from_url("https://img.hhcdn.ru/other/x.png") is None
