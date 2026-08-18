"""Configuration for LLM-assisted additional resume sections (#266)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ConfigError
from ._registry import register

_MODES = ("from_scratch", "prefill")
_BLOCKS = ("attestations", "recommendations")


@dataclass
class ResumeSectionsConfig:
    """Input and safety policy for the additional-sections command.

    Unsupported HH.ru blocks are deliberately not accepted here. Adding a
    selector without a live read-only investigation would turn a missing
    feature into an unsafe write.
    """

    mode: str = "from_scratch"
    blocks: list[str] = field(default_factory=lambda: list(_BLOCKS))
    context: str = ""


@register("resume_sections")
def parse_resume_sections(raw, context: str) -> ResumeSectionsConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    mode = raw.get("mode", "from_scratch")
    if mode not in _MODES:
        raise ConfigError(f"Поле 'mode' в '{context}' должно быть одним из {_MODES}")
    blocks = raw.get("blocks", list(_BLOCKS))
    if not isinstance(blocks, list) or not blocks or not all(isinstance(v, str) for v in blocks):
        raise ConfigError(f"Поле 'blocks' в '{context}' должно быть непустым списком строк")
    unknown = sorted(set(blocks) - set(_BLOCKS))
    if unknown:
        raise ConfigError(f"Неподдерживаемые блоки в '{context}': {', '.join(unknown)}")
    source = raw.get("context", "")
    if not isinstance(source, str):
        raise ConfigError(f"Поле 'context' в '{context}' должно быть строкой")
    return ResumeSectionsConfig(mode=mode, blocks=list(dict.fromkeys(blocks)), context=source)
