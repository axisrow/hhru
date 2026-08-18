"""Пакет парсеров секций config.yaml.

Импорт подмодулей тут нужен, чтобы их @register-декораторы выполнились и
реестр (config_sections._registry) заполнился. load_config обходит реестр
для resume-подсекций и вызывает parse_account для account напрямую.
"""

from __future__ import annotations

# Импорт регистрирует парсеры; порядок не важен.
from . import (  # noqa: F401
    account,
    ai_profile,
    education,
    resume_sections,
    scoring,
    search,
)
from ._registry import get, names, register
from .account import parse_account

__all__ = ["get", "names", "register", "parse_account"]
