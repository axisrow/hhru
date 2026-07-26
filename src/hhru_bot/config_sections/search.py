"""Парсер секции resume.search → SearchFilters.

Поля перенесены дословно из load_config (бывшие строки 113–121). #15 может
добавить ранжирование, расширив SearchFilters, не трогая этот файл критично.
"""

from __future__ import annotations

from ..config import ConfigError, SearchFilters
from ._registry import register


def _require(mapping: dict, key: str, context: str):
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"В конфиге отсутствует обязательное поле '{key}' ({context})")
    return mapping[key]


@register("search")
def parse_search(raw, context: str) -> SearchFilters:
    """raw — подсекция search; context — строка вида 'resumes[i].search'."""
    if not raw:
        raise ConfigError(f"В конфиге отсутствует обязательное поле 'search' ({context})")
    return SearchFilters(
        text=_require(raw, "text", f"{context}.text"),
        area=raw.get("area"),
        salary_from=raw.get("salary_from"),
        experience=raw.get("experience"),
        schedule=raw.get("schedule"),
        exclude_employers=raw.get("exclude_employers") or [],
        exclude_keywords=raw.get("exclude_keywords") or [],
        must_have=raw.get("must_have") or [],
        nice_to_have=raw.get("nice_to_have") or [],
    )
