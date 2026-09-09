"""Парсер корневой секции account → AccountConfig.

Обёртка над текущим парсингом account. #9 расширил AccountConfig опциональным
user_agent: если поле не задано в конфиге, browser/auth не передают user_agent
в Playwright new_context и тот ставит свой родной UA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConfigError
from ._validation import require


@dataclass
class AccountConfig:
    storage_state_file: Path
    # None = пусть Playwright ставит родной UA (по умолчанию). Задайте строку,
    # только если hh.ru требует конкретный User-Agent.
    user_agent: str | None = None
    current_employers: list[str] = field(default_factory=list)
    # #1103: сессии внешних провайдеров (провайдер → путь storage_state-файла).
    # Отдельный секрет второго уровня — никогда не hh_session.json. Пусто по
    # умолчанию: логин-сессия провайдера опциональна.
    external_sessions: dict[str, Path] = field(default_factory=dict)


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
    current_employers = raw.get("current_employer", [])
    if not isinstance(current_employers, list) or any(
        not isinstance(name, str) or not name.strip() for name in current_employers
    ):
        raise ConfigError("Поле 'current_employer' (account) должно быть списком непустых строк")
    external_sessions = _parse_external_sessions(raw.get("external_sessions"), base_dir)
    return AccountConfig(
        storage_state_file=(base_dir / storage_state_file).resolve(),
        # `or None` намеренно: пустая строка трактуется как «не задано» → родной UA.
        user_agent=user_agent or None,
        current_employers=[name.strip() for name in current_employers],
        external_sessions=external_sessions,
    )


def _parse_external_sessions(raw, base_dir: Path) -> dict[str, Path]:
    """Секция ``account.external_sessions`` → провайдер → абсолютный путь.

    #1103: каждый провайдер — свой storage_state-файл, резолвится относительно
    директории конфига (тот же контракт, что ``storage_state_file``). Имена
    провайдеров сверяются с реестром external_sessions.PROVIDERS на этапе
    загрузки конфига: опечатка («yandeks») не должна молча оставить fill-form
    без сессии. Один провайдер — один файл: дубликаты в YAML невозможны
    (последний ключ выигрывает в safe_load), мультилогин за рамками #1103.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("Поле 'external_sessions' (account) должно быть секцией провайдеров")
    from ..external_sessions import PROVIDERS

    result: dict[str, Path] = {}
    for provider_name, section in raw.items():
        if provider_name not in PROVIDERS:
            known = ", ".join(sorted(PROVIDERS))
            raise ConfigError(
                f"Неизвестный провайдер 'external_sessions.{provider_name}' (account); "
                f"доступны: {known}"
            )
        if not isinstance(section, dict):
            raise ConfigError(
                f"Поле 'external_sessions.{provider_name}' (account) должно быть секцией"
            )
        path = require(section, "storage_state_file", f"account.external_sessions.{provider_name}")
        result[provider_name] = (base_dir / path).resolve()
    return result


# account — корневая секция, не resume-подсекция, поэтому в реестр resume-секций
# не регистрируется; используется напрямую из load_config.
