"""Configuration for the resume education editor (#262)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ConfigError
from ._registry import register


@dataclass(frozen=True)
class EducationRecord:
    institution: str = ""
    # level is source metadata; hh.ru's confirmed forms expose faculty/
    # organization instead, so it is never written into those fields.
    level: str = ""
    faculty: str = ""
    organization: str = ""
    specialty: str = ""
    year: str = ""


@dataclass(frozen=True)
class EducationConfig:
    """User supplied context and optional existing values for education AI."""

    source: str = ""
    mode: str = "from_scratch"
    primary: list[EducationRecord] = field(default_factory=list)
    additional: list[EducationRecord] = field(default_factory=list)


def _records(raw, key: str, context: str) -> list[EducationRecord]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"Поле '{key}' в '{context}' должно быть списком")
    result = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"Элемент '{context}.{key}[{i}]' должен быть отображением")
        values = {}
        for field_name in ("institution", "level", "faculty", "organization", "specialty", "year"):
            value = item.get(field_name, "")
            if value is None:
                value = ""
            if not isinstance(value, (str, int)):
                raise ConfigError(f"Поле '{context}.{key}[{i}].{field_name}' должно быть строкой")
            values[field_name] = str(value).strip()
        result.append(EducationRecord(**values))
    return result


@register("education")
def parse_education(raw, context: str) -> EducationConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    source = raw.get("source", "")
    if source is None:
        source = ""
    if not isinstance(source, str):
        raise ConfigError(f"Поле '{context}.source' должно быть строкой")
    mode = raw.get("mode", "from_scratch")
    if mode not in ("from_scratch", "prefill"):
        raise ConfigError(f"Поле '{context}.mode' должно быть from_scratch или prefill")
    return EducationConfig(
        source=source,
        mode=mode,
        primary=_records(raw, "primary", context),
        additional=_records(raw, "additional", context),
    )
