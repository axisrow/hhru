"""Тесты resume-секции candidate_facts (issue #751).

Фундамент эпика #750: структурированные факты о кандидате с тегами
релевантности. Секция опциональна, обратная совместимость обязательна.
"""

from __future__ import annotations

import textwrap

import pytest

from hhru_bot.config import ConfigError, load_config
from hhru_bot.config_sections.candidate_facts import (
    CandidateFacts,
    EducationFact,
    LanguageFact,
    ProjectFact,
    WorkExperienceFact,
    parse_candidate_facts,
)

pytestmark = pytest.mark.unit


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config() -> str:
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python developer"
    """


# --- parse_candidate_facts: unit-тесты чистой функции ---


def test_parse_candidate_facts_none_when_raw_is_none():
    assert parse_candidate_facts(None, "resumes[0].candidate_facts") is None


def test_parse_candidate_facts_none_when_raw_is_empty_dict():
    # Консистентно с ai_profile/scoring: {} трактуется как отсутствие секции.
    assert parse_candidate_facts({}, "resumes[0].candidate_facts") is None


def test_parse_candidate_facts_wrong_type_raises():
    with pytest.raises(ConfigError, match="отображением"):
        parse_candidate_facts("not-a-dict", "resumes[0].candidate_facts")


def test_parse_candidate_facts_full():
    raw = {
        "work_experience": [
            {
                "company": "ООО Пример",
                "position": "Backend-разработчик",
                "period_from": "2021-03",
                "period_to": "2024-06",
                "description": "Разработка API на Python/Django.",
                "skills": ["python", "django"],
                "tags": ["backend", "python"],
            }
        ],
        "education": [
            {
                "institution": "МГУ",
                "specialty": "Физика",
                "year": "2015",
                "tags": ["general"],
            }
        ],
        "languages": [
            {"name": "Английский", "level": "B2", "tags": ["general"]},
        ],
        "projects": [
            {
                "name": "Трекер вакансий",
                "description": "CLI на Playwright",
                "skills": ["python", "playwright"],
                "tags": ["backend", "automation"],
            }
        ],
    }
    facts = parse_candidate_facts(raw, "resumes[0].candidate_facts")
    assert facts == CandidateFacts(
        work_experience=[
            WorkExperienceFact(
                company="ООО Пример",
                position="Backend-разработчик",
                period_from="2021-03",
                period_to="2024-06",
                description="Разработка API на Python/Django.",
                skills=["python", "django"],
                tags=["backend", "python"],
            )
        ],
        education=[
            EducationFact(institution="МГУ", specialty="Физика", year="2015", tags=["general"])
        ],
        languages=[LanguageFact(name="Английский", level="B2", tags=["general"])],
        projects=[
            ProjectFact(
                name="Трекер вакансий",
                description="CLI на Playwright",
                skills=["python", "playwright"],
                tags=["backend", "automation"],
            )
        ],
    )


def test_parse_candidate_facts_partial_sections_default_empty():
    # Указана только одна подсекция — остальные дефолтятся в пустые списки.
    facts = parse_candidate_facts(
        {"languages": [{"name": "Английский", "level": "B2"}]},
        "resumes[0].candidate_facts",
    )
    assert facts is not None
    assert facts.work_experience == []
    assert facts.education == []
    assert facts.projects == []
    assert facts.languages == [LanguageFact(name="Английский", level="B2", tags=[])]


def test_parse_candidate_facts_tags_default_empty_list():
    # tags опционален на уровне отдельного факта: пустой список — валидное
    # значение (факт релевантен всем кластерам / кластеризация не проведена).
    facts = parse_candidate_facts(
        {"work_experience": [{"company": "ООО Пример", "position": "Dev"}]},
        "resumes[0].candidate_facts",
    )
    assert facts is not None
    assert facts.work_experience[0].tags == []


@pytest.mark.parametrize(
    "section",
    ["work_experience", "education", "languages", "projects"],
)
def test_parse_candidate_facts_section_wrong_type_raises(section):
    with pytest.raises(ConfigError, match=section):
        parse_candidate_facts({section: "not-a-list"}, "resumes[0].candidate_facts")


@pytest.mark.parametrize(
    "section",
    ["work_experience", "education", "languages", "projects"],
)
def test_parse_candidate_facts_section_item_wrong_type_raises(section):
    with pytest.raises(ConfigError, match="отображением"):
        parse_candidate_facts({section: ["not-a-dict"]}, "resumes[0].candidate_facts")


def test_parse_candidate_facts_tags_wrong_type_raises():
    with pytest.raises(ConfigError, match="tags"):
        parse_candidate_facts(
            {"work_experience": [{"company": "X", "tags": "not-a-list"}]},
            "resumes[0].candidate_facts",
        )


def test_parse_candidate_facts_skills_wrong_type_raises():
    with pytest.raises(ConfigError, match="skills"):
        parse_candidate_facts(
            {"projects": [{"name": "X", "skills": "python"}]},
            "resumes[0].candidate_facts",
        )


# --- load_config integration: обратная совместимость + полный конфиг ---


def test_load_config_candidate_facts_none_when_absent(tmp_path):
    config = load_config(_write_config(tmp_path, _minimal_config()))
    assert config.resumes[0].candidate_facts is None


def test_load_config_candidate_facts_full(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/CF100"
            search:
              text: "python developer"
            candidate_facts:
              work_experience:
                - company: "ООО Пример"
                  position: "Backend-разработчик"
                  period_from: "2021-03"
                  period_to: "2024-06"
                  description: "API на Python/Django"
                  skills: ["python", "django"]
                  tags: ["backend", "python"]
              education:
                - institution: "МГУ"
                  specialty: "Физика"
                  year: "2015"
                  tags: ["general"]
              languages:
                - name: "Английский"
                  level: "B2"
                  tags: ["general"]
              projects:
                - name: "Трекер вакансий"
                  description: "CLI на Playwright"
                  skills: ["python"]
                  tags: ["automation"]
        """,
    )
    facts = load_config(path).resumes[0].candidate_facts
    assert facts is not None
    assert facts.work_experience[0].company == "ООО Пример"
    assert facts.work_experience[0].tags == ["backend", "python"]
    assert facts.education[0].institution == "МГУ"
    assert facts.languages[0].name == "Английский"
    assert facts.projects[0].name == "Трекер вакансий"


def test_load_config_candidate_facts_empty_dict_is_none(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/CF200"
            search:
              text: "x"
            candidate_facts: {}
        """,
    )
    assert load_config(path).resumes[0].candidate_facts is None


def test_load_config_candidate_facts_invalid_type_raises(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/CF300"
            search:
              text: "x"
            candidate_facts:
              work_experience: "not-a-list"
        """,
    )
    with pytest.raises(ConfigError, match="work_experience"):
        load_config(path)


def test_load_config_without_candidate_facts_still_loads_other_sections(tmp_path):
    # Обратная совместимость: конфиг с ai_profile, но без candidate_facts,
    # продолжает грузиться (секции независимы).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/CF400"
            search:
              text: "x"
            ai_profile:
              summary: "Кратко о себе"
        """,
    )
    resume = load_config(path).resumes[0]
    assert resume.candidate_facts is None
    assert resume.ai_profile is not None
