"""Чистые планировщики и сверка import-resume (#1023); браузерной части нет."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hhru_bot.export_resume import EXPORT_SCHEMA
from hhru_bot.import_resume import (
    IMPORT_SKILL_LEVEL,
    ImportPlanError,
    diff_export,
    export_photos_on_disk,
    load_export,
    parse_period,
    parse_salary_text,
    plan_education,
    plan_experience,
    plan_languages,
    plan_position,
    plan_skills,
)

pytestmark = pytest.mark.unit

PAYLOAD = {
    "schema": EXPORT_SCHEMA,
    "resume_id": "00001",
    "slug": "main",
    "resume_url": "https://hh.ru/resume/00001",
    "position": {
        "title": "Python-разработчик",
        "salary_text": "150 000 ₽",
        "fields": [
            {"field": "employmentForms", "text": "Постоянная работа"},
            {"field": "workFormats", "text": "Удалённо"},
            {"field": "travelTime", "text": "Не имеет значения"},
            {"field": "businessTripReadiness", "text": "Могу"},
        ],
    },
    "about": "Разрабатываю сервисы.",
    "experience": {
        "companies": [
            {
                "company": "ООО Тест",
                "positions": [
                    {
                        "title": "Разработчик",
                        "period": "Март 2020 — Март 2024",
                        "description": "Писал код",
                    }
                ],
            }
        ]
    },
    "education": [{"id": "1", "title": "МГТУ", "subtitle": "Факультет ИУ", "description": "2020"}],
    "skills": [{"id": "1", "name": "Python"}],
    "languages": [{"id": "1", "title": "Английский", "subtitle": "B2"}],
    "photos": [],
}


def _write_export(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "export.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_export_accepts_own_schema(tmp_path):
    payload = load_export(_write_export(tmp_path, PAYLOAD))
    assert payload["resume_id"] == "00001"


def test_load_export_rejects_foreign_schema(tmp_path):
    foreign = {**PAYLOAD, "schema": "someone-else/v9"}
    with pytest.raises(ImportPlanError):
        load_export(_write_export(tmp_path, foreign))


def test_parse_salary_text_digits_and_currency():
    assert parse_salary_text("150 000 ₽") == (150000, "RUR")
    assert parse_salary_text("2000$") == (2000, "USD")
    assert parse_salary_text("не указана") == (None, None)
    assert parse_salary_text(None) == (None, None)


def test_plan_position_maps_display_texts():
    plan, unavailable = plan_position(PAYLOAD)
    assert plan.title == "Python-разработчик"
    assert plan.salary == 150000
    assert plan.currency == "RUR"
    assert plan.employment == ["full_time"]
    assert plan.work_format == ["remote"]
    assert plan.commute == "no_limit"
    assert plan.business_trips is True
    assert unavailable == []


def test_plan_position_reports_unrecognized_field():
    payload = {
        **PAYLOAD,
        "position": {
            "title": "Тест",
            "salary_text": None,
            "fields": [{"field": "employmentForms", "text": "Какая-то новая занятость"}],
        },
    }
    plan, unavailable = plan_position(payload)
    assert plan.employment is None
    assert any("занятость" in note for note in unavailable)


def test_parse_period_closed_and_current():
    assert parse_period("Март 2020 — Март 2024") == ("3", "2020", "3", "2024", False)
    assert parse_period("Июнь 2021 — по настоящее время") == ("6", "2021", "", "", True)
    assert parse_period("2020") is None
    assert parse_period("") is None
    assert parse_period(None) is None


def test_plan_experience_skips_unparsable_period():
    payload = {
        "experience": {
            "companies": [
                {
                    "company": "ООО Тест",
                    "positions": [
                        {"title": "Разработчик", "period": "Март 2020 — Март 2024"},
                        {"title": "Стажёр", "period": "давно"},
                    ],
                }
            ]
        }
    }
    plan, unavailable = plan_experience(payload)
    assert [e.position for e in plan.entries] == ["Разработчик"]
    assert plan.entries[0].company == "ООО Тест"
    assert plan.entries[0].start_month == "3"
    assert plan.entries[0].current is False
    assert any("период не разобран" in note for note in unavailable)


def test_plan_skills_uses_documented_level_convention():
    skills, unavailable = plan_skills(PAYLOAD)
    assert [s.name for s in skills] == ["Python"]
    assert skills[0].level == IMPORT_SKILL_LEVEL


def test_plan_languages_keeps_cefr_skips_native():
    payload = {
        "languages": [
            {"id": "1", "title": "Английский", "subtitle": "B2"},
            {"id": "2", "title": "Русский", "subtitle": "Родной"},
        ]
    }
    languages, unavailable = plan_languages(payload)
    assert [(lang.name, lang.level) for lang in languages] == [("Английский", "B2")]
    assert any("не CEFR" in note for note in unavailable)


def test_plan_education_extracts_year():
    plan, unavailable = plan_education(PAYLOAD)
    assert plan.primary[0].institution == "МГТУ"
    assert plan.primary[0].year == "2020"
    assert unavailable == []


def test_export_photos_on_disk_reports_missing_files(tmp_path):
    existing = tmp_path / "photo_1.jpeg"
    existing.write_bytes(b"\xff\xd8fake")
    payload = {
        "photos": [
            {"photo_id": "1", "status": "downloaded", "file": str(existing)},
            {"photo_id": "2", "status": "downloaded", "file": str(tmp_path / "gone.jpeg")},
            {"photo_id": "3", "status": "failed", "file": None},
        ]
    }
    files, unavailable = export_photos_on_disk(payload)
    assert [f.name for f in files] == ["photo_1.jpeg"]
    assert len(unavailable) == 1
    assert "2" in unavailable[0]


def test_diff_export_equal_payloads_match():
    imported = json.loads(json.dumps(PAYLOAD))
    imported["position"]["salary_text"] = "150000 ₽"  # одна сумма, другой формат
    assert diff_export(PAYLOAD, imported) == []


def test_diff_export_lists_discrepancies():
    imported = json.loads(json.dumps(PAYLOAD))
    imported["position"]["title"] = "Другая роль"
    imported["about"] = None
    imported["skills"] = []
    diffs = diff_export(PAYLOAD, imported)
    assert any("title" in d for d in diffs)
    assert any("о себе" in d for d in diffs)
    assert any("навыки" in d for d in diffs)
