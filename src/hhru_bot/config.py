from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

logger = logging.getLogger("hhru_bot.config")

# The shipped example deliberately uses X/Y runs instead of a real resume id.
# Keep this recognition narrow: arbitrary resume ids remain valid config values.
_RESUME_PLACEHOLDER_RE = re.compile(r"^(?:X{8,}|Y{8,})$")


def is_resume_url_placeholder(resume_url: str) -> bool:
    """Return whether ``resume_url`` contains the example-config placeholder."""
    resume_id = resume_url.rstrip("/").rsplit("/", 1)[-1]
    return bool(_RESUME_PLACEHOLDER_RE.fullmatch(resume_id))


if TYPE_CHECKING:
    # Только для статического анализа; реальный импорт был бы циклическим
    # (config_sections.ai импортирует ConfigError из config).
    from .config_sections.ai import AiConfig


@dataclass
class ThrottleConfig:
    min_delay_seconds: float = 8
    max_delay_seconds: float = 25
    daily_apply_limit: int = 40
    daily_bump_limit: int = 10


@dataclass
class SearchFilters:
    text: str
    area: int | None = None
    salary_from: int | None = None
    experience: str | None = None
    schedule: str | None = None
    exclude_employers: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    # Опциональные поля ранжирования (#15): буст за совпадение в title.
    must_have: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)


@dataclass
class ResumeConfig:
    id: str
    resume_url: str
    search: SearchFilters
    cover_letter: str | None = None
    # Пред-добавленные нейтральные заглушки под будущие feature-ишью:
    # #15 (scoring) и #17 (ai_profile) заполнят их своими датаклассами, не трогая
    # ResumeConfig. None = секция отсутствует в конфиге.
    scoring: object | None = None
    ai_profile: object | None = None
    resume_sections: object | None = None

    @property
    def resume_id(self) -> str:
        return self.resume_url.rstrip("/").split("/")[-1]


@dataclass
class AppConfig:
    storage_state_file: Path
    throttle: ThrottleConfig
    cover_letter_default: str
    resumes: list[ResumeConfig]
    # None = родной UA Playwright. Пробрасывается из account.user_agent (см. parse_account).
    user_agent: str | None = None
    # None = AI-функциональность выключена (issue #16, Этап 5). TOP-LEVEL секция ai
    # (как account), парсится в load_config через config_sections.ai.parse_ai.
    ai: AiConfig | None = None

    def get_resume(self, resume_id: str) -> ResumeConfig:
        for resume in self.resumes:
            if resume.id == resume_id:
                return resume
        available = ", ".join(r.id for r in self.resumes)
        raise ConfigError(f"Резюме '{resume_id}' не найдено в конфиге. Доступные: {available}")

    def cover_letter_for(self, resume: ResumeConfig) -> str:
        return resume.cover_letter or self.cover_letter_default


class ConfigError(Exception):
    pass


def _require(mapping: dict, key: str, context: str):
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"В конфиге отсутствует обязательное поле '{key}' ({context})")
    return mapping[key]


def load_config(path: str | Path) -> AppConfig:
    # Lazy-импорт, чтобы разорвать цикл config <-> config_sections
    # (config_sections.* импортируют типы из config на загрузке).
    from .config_sections import get as section_parser
    from .config_sections import names as section_names
    from .config_sections import parse_account
    from .config_sections.ai import parse_ai

    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Файл конфига не найден: {path}\n"
            f"Скопируйте config/config.example.yaml в data/config.yaml и заполните его."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ConfigError(f"Конфиг {path} пуст или некорректен")

    # storage_state_file резолвится относительно директории файла конфига —
    # стабильно, не зависит от cwd (даже при --config с абсолютным путём из
    # чужой директории). Shipped-путь storage_state/... (от data/, где теперь
    # живёт конфиг — #133) → data/storage_state/ → покрыт .gitignore правилом
    # 'data/'. См. regression-тест test_session_secret_is_gitignored_when_config_in_repo.
    account = parse_account(raw.get("account"), path.parent)
    storage_state_file = account.storage_state_file
    user_agent = account.user_agent

    # TOP-LEVEL секция ai (issue #16, Этап 5): провайдер/модель/base_url.
    # Опциональна — None, если секции нет. API-ключ НЕ парсится из yaml (только env).
    ai = parse_ai(raw.get("ai"), "ai")

    throttle_raw = raw.get("throttle", {})
    throttle = ThrottleConfig(
        min_delay_seconds=throttle_raw.get("min_delay_seconds", 8),
        max_delay_seconds=throttle_raw.get("max_delay_seconds", 25),
        daily_apply_limit=throttle_raw.get("daily_apply_limit", 40),
        daily_bump_limit=throttle_raw.get("daily_bump_limit", 10),
    )

    cover_letter_default = raw.get("cover_letter_default", "")

    resumes_raw = _require(raw, "resumes", "корневой раздел")
    if not isinstance(resumes_raw, list) or not resumes_raw:
        raise ConfigError("Раздел 'resumes' должен быть непустым списком")

    resumes: list[ResumeConfig] = []
    seen_ids = set()
    for i, r in enumerate(resumes_raw):
        context = f"resumes[{i}]"
        resume_id = _require(r, "id", context)
        if resume_id in seen_ids:
            raise ConfigError(f"Дублирующийся id резюме в конфиге: '{resume_id}'")
        seen_ids.add(resume_id)

        resume_url = _require(r, "resume_url", context)

        # Парсинг resume-подсекций делегирован реестру config_sections.
        kwargs: dict[str, object] = {"cover_letter": r.get("cover_letter")}
        for sec_name in section_names():
            parser = section_parser(sec_name)
            kwargs[sec_name] = parser(r.get(sec_name), f"{context}.{sec_name}")

        resume = ResumeConfig(
            id=resume_id,
            resume_url=resume_url,
            **kwargs,  # type: ignore[arg-type]
        )
        _warn_if_scoring_keywords_inert(resume, context)
        resumes.append(resume)

    return AppConfig(
        storage_state_file=storage_state_file,
        throttle=throttle,
        cover_letter_default=cover_letter_default,
        resumes=resumes,
        user_agent=user_agent,
        ai=ai,
    )


def _warn_if_scoring_keywords_inert(resume: ResumeConfig, context: str) -> None:
    """Предупреждает о молча неработающих must_have/nice_to_have (issue #121).

    Ключевые слова задаются в секции ``search``, а ВКЛЮЧАЮТСЯ секцией
    ``scoring``: без неё ранжирование использует нулевые веса (``_ZERO_WEIGHTS``
    в search.py — намеренно, ради обратной совместимости ``candidates[:limit]``),
    и score всех вакансий = 0.00. Конфиг при этом выглядит настроенным, поэтому
    молчание здесь хуже, чем явно выключённая фича.

    Поведение НЕ меняется (веса остаются нулевыми) — добавляется только
    диагностика: сломать существующие конфиги предупреждением нельзя.
    """
    filters = getattr(resume, "search", None)
    if filters is None or getattr(resume, "scoring", None) is not None:
        return
    if getattr(filters, "must_have", None) or getattr(filters, "nice_to_have", None):
        logger.warning(
            "%s: must_have/nice_to_have заданы, но секции 'scoring' нет — "
            "веса нулевые, ранжирование отключено (score всех вакансий 0.00). "
            "Добавьте scoring.weights, чтобы ключевые слова заработали.",
            context,
        )


def load_config_or_exit(path: str | Path) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(1)
