"""Парсер секции resume.search → SearchFilters.

Поля перенесены дословно из load_config (бывшие строки 113–121). #15 может
добавить ранжирование, расширив SearchFilters, не трогая этот файл критично.
"""

from __future__ import annotations

from ..config import ConfigError, SearchFilters
from ._registry import register
from ._validation import require

# Значения параметра schedule поиска hh.ru (расширенный фильтр «График
# работы»). ПОДТВЕРЖДЕНО живым прогоном: remote фильтрует выдачу до
# удалёнки (2026-09-08, аккаунт testing). ВАЖНО для формата работы: поиск
# hh.ru выражает ТОЛЬКО удалёнку — «офис»/«гибрид» отдельными значениями
# фильтра не существует (это атрибут вакансии, не графика), их не выбирает
# ни одно значение этого поля.
_SCHEDULE_VALUES = frozenset({"fullDay", "shift", "flex", "flyInFlyOut", "remote"})


def _parse_str_list(raw, key: str, context: str) -> list[str]:
    """Список строк из raw[key]; None/отсутствие → [].

    Явная валидация типа (fail-closed на границе конфига): строка вместо
    списка или не-строковый элемент дают ConfigError с указанием поля.
    """
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{context}.{key} должен быть списком строк")
    return list(value)


def _parse_optional_int(raw, key: str, context: str) -> int | None:
    """Целое из raw[key]; None/отсутствие → None.

    bool — подтип int в Python, но YAML true/false числом не является —
    отклоняем явно, чтобы `salary_to: true` не превратился молча в 1.
    """
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context}.{key} должен быть целым числом")
    return value


@register("search")
def parse_search(raw, context: str) -> SearchFilters:
    """raw — подсекция search; context — строка вида 'resumes[i].search'."""
    if not raw:
        raise ConfigError(f"В конфиге отсутствует обязательное поле 'search' ({context})")
    schedule = raw.get("schedule")
    if schedule is not None and schedule not in _SCHEDULE_VALUES:
        # Непрозрачный passthrough молча делал бы битый URL поиска: опечатка
        # («удалённо», «remote ») или русское значение тихо обнуляла фильтр и
        # выдача возвращалась без удалёнки (живой кейс 2026-09-08: area=1 без
        # schedule принёс 10 московских офисных/гибридных вакансий).
        raise ConfigError(
            f"{context}.schedule: недопустимое значение {schedule!r}; "
            f"допустимо одно из {sorted(_SCHEDULE_VALUES)} или null "
            "(без фильтра — вся выдача, включая офис и гибрид)"
        )
    return SearchFilters(
        text=require(raw, "text", f"{context}.text"),
        area=raw.get("area"),
        salary_from=raw.get("salary_from"),
        salary_to=_parse_optional_int(raw, "salary_to", context),
        experience=raw.get("experience"),
        schedule=schedule,
        allow_relocation=bool(raw.get("allow_relocation", False)),
        include_employers=_parse_str_list(raw, "include_employers", context),
        exclude_employers=raw.get("exclude_employers") or [],
        exclude_keywords=raw.get("exclude_keywords") or [],
        must_have=raw.get("must_have") or [],
        nice_to_have=raw.get("nice_to_have") or [],
    )
