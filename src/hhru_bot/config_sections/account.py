"""Парсер корневой секции account → AccountConfig.

Обёртка над текущим парсингом account. #9 расширил AccountConfig опциональным
user_agent: если поле не задано в конфиге, browser/auth не передают user_agent
в Playwright new_context и тот ставит свой родной UA.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_ROOT, ConfigError


@dataclass
class AccountConfig:
    storage_state_file: Path
    # None = пусть Playwright ставит родной UA (по умолчанию). Задайте строку,
    # только если hh.ru требует конкретный User-Agent.
    user_agent: str | None = None


def _require(mapping: dict, key: str, context: str):
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"В конфиге отсутствует обязательное поле '{key}' ({context})")
    return mapping[key]


def parse_account(raw) -> AccountConfig:
    """raw — корневая секция account. Возвращает AccountConfig."""
    if not raw:
        raise ConfigError("В конфиге отсутствует обязательное поле 'storage_state_file' (account)")
    storage_state_file = _require(raw, "storage_state_file", "account")
    # user_agent опционален: None = родной UA Playwright (никакого хардкода).
    user_agent = raw.get("user_agent")
    if user_agent is not None and not isinstance(user_agent, str):
        raise ConfigError("Поле 'user_agent' (account) должно быть строкой")
    return AccountConfig(
        storage_state_file=PROJECT_ROOT / storage_state_file,
        # `or None` намеренно: пустая строка трактуется как «не задано» → родной UA.
        user_agent=user_agent or None,
    )


# account — корневая секция, не resume-подсекция, поэтому в реестр resume-секций
# не регистрируется; используется напрямую из load_config.
