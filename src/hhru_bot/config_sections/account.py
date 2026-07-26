"""Парсер корневой секции account → storage_state_file (Path).

Обёртка над текущим парсингом account. #9 расширит AccountConfig (например,
user_agent) здесь, не трогая load_config.
"""

from __future__ import annotations

from pathlib import Path

from ..config import PROJECT_ROOT, ConfigError


def _require(mapping: dict, key: str, context: str):
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"В конфиге отсутствует обязательное поле '{key}' ({context})")
    return mapping[key]


def parse_account(raw) -> Path:
    """raw — корневая секция account. Возвращает абсолютный путь к файлу сессии."""
    account = _require(raw, "storage_state_file", "account") if raw else None
    if account is None:
        raise ConfigError("В конфиге отсутствует обязательное поле 'storage_state_file' (account)")
    return PROJECT_ROOT / account


# account — корневая секция, не resume-подсекция, поэтому в реестр resume-секций
# не регистрируется; используется напрямую из load_config.
