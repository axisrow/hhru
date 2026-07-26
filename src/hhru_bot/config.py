from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    # (config_sections.* импортируют типы/PROJECT_ROOT из config на загрузке).
    from .config_sections import get as section_parser
    from .config_sections import names as section_names
    from .config_sections import parse_account

    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Файл конфига не найден: {path}\n"
            f"Скопируйте config/config.example.yaml в config/config.yaml и заполните его."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ConfigError(f"Конфиг {path} пуст или некорректен")

    account = parse_account(raw.get("account"))
    storage_state_file = account.storage_state_file
    user_agent = account.user_agent

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

        resumes.append(
            ResumeConfig(
                id=resume_id,
                resume_url=resume_url,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    return AppConfig(
        storage_state_file=storage_state_file,
        throttle=throttle,
        cover_letter_default=cover_letter_default,
        resumes=resumes,
        user_agent=user_agent,
    )


def load_config_or_exit(path: str | Path) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(1)
