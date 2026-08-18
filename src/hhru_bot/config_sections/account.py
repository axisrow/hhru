"""Парсер корневой секции account → AccountConfig.

Обёртка над текущим парсингом account. #9 расширил AccountConfig опциональным
user_agent: если поле не задано в конфиге, browser/auth не передают user_agent
в Playwright new_context и тот ставит свой родной UA.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError
from ._validation import require


@dataclass
class AccountConfig:
    storage_state_file: Path
    # None = пусть Playwright ставит родной UA (по умолчанию). Задайте строку,
    # только если hh.ru требует конкретный User-Agent.
    user_agent: str | None = None


def parse_account(raw, base_dir: Path) -> AccountConfig:
    """raw — корневая секция account. Возвращает AccountConfig.

    ``storage_state_file`` резолвится **относительно директории файла конфига**
    (``base_dir``), а не относительно cwd или пакета. Так путь стабилен и не
    зависит от того, откуда запущен CLI — даже ``hhru-bot --config /abs/.../config.yaml login``
    из чужой директории пишет сессию рядом с конфигом, куда указал пользователь.

    SECURITY: shipped-путь в config.example.yaml — ``storage_state/...``
    (от ``data/``, где живёт конфиг после #133 → ``data/storage_state/`` →
    покрыто ``.gitignore`` правилом ``data/``). Относительно config
    резолвится безопасно; что бы ни было в ``base_dir``, итоговый путь — под
    контролем файла конфига, а не CWD процесса. См. regression-тест в test_config.py.
    """
    if not raw:
        raise ConfigError("В конфиге отсутствует обязательное поле 'storage_state_file' (account)")
    storage_state_file = require(raw, "storage_state_file", "account")
    # user_agent опционален: None = родной UA Playwright (никакого хардкода).
    user_agent = raw.get("user_agent")
    if user_agent is not None and not isinstance(user_agent, str):
        raise ConfigError("Поле 'user_agent' (account) должно быть строкой")
    return AccountConfig(
        storage_state_file=(base_dir / storage_state_file).resolve(),
        # `or None` намеренно: пустая строка трактуется как «не задано» → родной UA.
        user_agent=user_agent or None,
    )


# account — корневая секция, не resume-подсекция, поэтому в реестр resume-секций
# не регистрируется; используется напрямую из load_config.
