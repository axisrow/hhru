"""Реестр парсеров секций resume-уровня в config.yaml.

Каждая resume-подсекция (search, scoring, ai_profile, ...) парсится своей функцией
в config_sections/<name>.py и регистрируется через декоратор @register("<name>").
Так feature-ишью (#9 account, #15 scoring, #17 ai_profile) добавляют свою секцию
новым файлом, не трогая load_config и ResumeConfig (Optional-поля для
scoring/ai_profile пред-добавлены).

Сигнатура парсера:
    parse_fn(raw: dict | None, context: str) -> Any | None
где raw — подсекция из yaml (может быть None), context — строка для ошибок.
Возвращает значение поля ResumeConfig или None, если секция отсутствует.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# name секции -> parse_fn(raw, context)
SectionParser = Callable[[Any, str], Any | None]
_PARSERS: dict[str, SectionParser] = {}


def register(name: str) -> Callable[[SectionParser], SectionParser]:
    """Декоратор: @register("search") регистрирует функцию как парсер секции."""

    def decorator(parser: SectionParser) -> SectionParser:
        _PARSERS[name] = parser
        return parser

    return decorator


def get(name: str) -> SectionParser | None:
    return _PARSERS.get(name)


def names() -> list[str]:
    return sorted(_PARSERS)
