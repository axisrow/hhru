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

import datetime
from dataclasses import dataclass, field

from ..config import ConfigError
from ._registry import register

# Поля-даты (year, period_from, period_to) естественно писать в YAML без
# кавычек ("2015", "2021-03-01") — PyYAML парсит их как int/date, не str.
# Соседняя секция education.py уже терпит int для year (isinstance(value,
# (str, int))); здесь то же самое расширено на date/datetime ради
# period_from/period_to (полный ISO-вид тоже валиден без кавычек в YAML).
_SCALAR_TYPES = (str, int, datetime.date, datetime.datetime)


def _require_str(raw: dict, key: str, context: str) -> str:
    value = raw.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, _SCALAR_TYPES):
        raise ConfigError(f"Поле '{key}' в '{context}' должно быть строкой, получено: {value!r}")
    return str(value).strip() if not isinstance(value, str) else value


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


# (dataclass, поля-строки, поля-списки-строк) на каждую подсекцию — параметризует
# generic-парсер ниже вместо четырёх механически идентичных функций (все четыре
# отличались только именем секции/полей, ту же форму уже обобщает
# education.py::_records() для одной записи).
_FACT_SPECS: dict[str, tuple[type, tuple[str, ...], tuple[str, ...]]] = {
    "work_experience": (
        WorkExperienceFact,
        ("company", "position", "period_from", "period_to", "description"),
        ("skills", "tags"),
    ),
    "education": (EducationFact, ("institution", "specialty", "year"), ("tags",)),
    "languages": (LanguageFact, ("name", "level"), ("tags",)),
    "projects": (ProjectFact, ("name", "description"), ("skills", "tags")),
}


def _parse_fact_list(raw: dict, section: str, context: str) -> list:
    fact_cls, str_fields, list_fields = _FACT_SPECS[section]
    value = raw.get(section, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Поле '{context}.{section}' должно быть списком")
    result = []
    for i, item in enumerate(value):
        item_context = f"{context}.{section}[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"Элемент '{item_context}' должен быть отображением")
        kwargs: dict[str, str | list[str]] = {}
        for name in str_fields:
            kwargs[name] = _require_str(item, name, item_context)
        for name in list_fields:
            kwargs[name] = _require_str_list(item, name, item_context)
        result.append(fact_cls(**kwargs))
    return result


@register("candidate_facts")
def parse_candidate_facts(raw, context: str) -> CandidateFacts | None:
    """raw — подсекция candidate_facts (может быть None/отсутствовать/{})."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")

    return CandidateFacts(
        work_experience=_parse_fact_list(raw, "work_experience", context),
        education=_parse_fact_list(raw, "education", context),
        languages=_parse_fact_list(raw, "languages", context),
        projects=_parse_fact_list(raw, "projects", context),
    )
