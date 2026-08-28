"""Парсер опциональной resume-секции candidate_facts -> CandidateFacts (issue #751).

Фундамент эпика #750 (адаптивные резюме: пул под кластеры). `AIProfile`
(#17, ``ai_profile.py``) покрывает только генерацию сопроводительных писем и
не описывает структурированные факты о кандидате — места работы с датами,
образование, языки, проекты. Без них отбор фактов под кластер вакансий
(глубина адаптации, выбранная в #750) реализовать не из чего.

Секция опциональна: при отсутствии load_config оставит
ResumeConfig.candidate_facts = None (обратная совместимость — код,
написанный до #751, продолжает работать без изменений).

**Каждый факт (место работы, образование, язык, проект) несёт список
`tags: list[str]` — теги релевантности.** Это единственный контракт, ради
которого существует эта секция: без тегов sub-issue отбора (#753/#754) не
сможет решить, какой факт показывать под какой кластер вакансий. Пустой
список `tags` — валидное значение (факт релевантен всем кластерам или
кластеризация ещё не проведена), но поле обязано присутствовать в датаклассе.

Эта секция НЕ подключена ни к одному генератору разделов резюме (about.py,
experience.py, ...) — это осознанно оставлено sub-issue #753. #751 — только
машинно-читаемое описание фактов.

Секция типизирована явно (`CandidateFacts | None`), а не через нейтральный
`object | None` (в отличие от `ResumeConfig.ai_profile`/`scoring`/
`resume_sections`/`education` — те заведены раньше и их дефект типизации не
в скоупе этого issue, см. CLAUDE.md и тело #751 "Заодно исправить" /
"Чего НЕ делать"). Новый код так не делает.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ConfigError
from ._registry import register


def _require_str(raw: dict, key: str, context: str) -> str:
    value = raw.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"Поле '{key}' в '{context}' должно быть строкой, получено: {value!r}")
    return value


def _require_str_list(raw: dict, key: str, context: str) -> list[str]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"Поле '{key}' в '{context}' должно быть списком строк")
    return value


@dataclass(frozen=True)
class WorkExperienceFact:
    """Одно место работы. Правдивость обязательна (#750): даты/компания/должность
    не выдумываются, только отбираются под кластер через `tags`."""

    company: str = ""
    position: str = ""
    period_from: str = ""
    period_to: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EducationFact:
    institution: str = ""
    specialty: str = ""
    year: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LanguageFact:
    name: str = ""
    level: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectFact:
    name: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CandidateFacts:
    """Машинно-читаемые факты о кандидате. Все поля опциональны/списки могут
    быть пустыми — секция подключается постепенно, по мере заполнения."""

    work_experience: list[WorkExperienceFact] = field(default_factory=list)
    education: list[EducationFact] = field(default_factory=list)
    languages: list[LanguageFact] = field(default_factory=list)
    projects: list[ProjectFact] = field(default_factory=list)


def _parse_work_experience(raw, context: str) -> list[WorkExperienceFact]:
    value = raw.get("work_experience", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Поле '{context}.work_experience' должно быть списком")
    result = []
    for i, item in enumerate(value):
        item_context = f"{context}.work_experience[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"Элемент '{item_context}' должен быть отображением")
        result.append(
            WorkExperienceFact(
                company=_require_str(item, "company", item_context),
                position=_require_str(item, "position", item_context),
                period_from=_require_str(item, "period_from", item_context),
                period_to=_require_str(item, "period_to", item_context),
                description=_require_str(item, "description", item_context),
                skills=_require_str_list(item, "skills", item_context),
                tags=_require_str_list(item, "tags", item_context),
            )
        )
    return result


def _parse_education(raw, context: str) -> list[EducationFact]:
    value = raw.get("education", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Поле '{context}.education' должно быть списком")
    result = []
    for i, item in enumerate(value):
        item_context = f"{context}.education[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"Элемент '{item_context}' должен быть отображением")
        result.append(
            EducationFact(
                institution=_require_str(item, "institution", item_context),
                specialty=_require_str(item, "specialty", item_context),
                year=_require_str(item, "year", item_context),
                tags=_require_str_list(item, "tags", item_context),
            )
        )
    return result


def _parse_languages(raw, context: str) -> list[LanguageFact]:
    value = raw.get("languages", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Поле '{context}.languages' должно быть списком")
    result = []
    for i, item in enumerate(value):
        item_context = f"{context}.languages[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"Элемент '{item_context}' должен быть отображением")
        result.append(
            LanguageFact(
                name=_require_str(item, "name", item_context),
                level=_require_str(item, "level", item_context),
                tags=_require_str_list(item, "tags", item_context),
            )
        )
    return result


def _parse_projects(raw, context: str) -> list[ProjectFact]:
    value = raw.get("projects", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Поле '{context}.projects' должно быть списком")
    result = []
    for i, item in enumerate(value):
        item_context = f"{context}.projects[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"Элемент '{item_context}' должен быть отображением")
        result.append(
            ProjectFact(
                name=_require_str(item, "name", item_context),
                description=_require_str(item, "description", item_context),
                skills=_require_str_list(item, "skills", item_context),
                tags=_require_str_list(item, "tags", item_context),
            )
        )
    return result


@register("candidate_facts")
def parse_candidate_facts(raw, context: str) -> CandidateFacts | None:
    """raw — подсекция candidate_facts (может быть None/отсутствовать/{})."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")

    return CandidateFacts(
        work_experience=_parse_work_experience(raw, context),
        education=_parse_education(raw, context),
        languages=_parse_languages(raw, context),
        projects=_parse_projects(raw, context),
    )
