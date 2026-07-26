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


@dataclass
class ResumeConfig:
    id: str
    resume_url: str
    search: SearchFilters
    cover_letter: str | None = None

    @property
    def resume_id(self) -> str:
        return self.resume_url.rstrip("/").split("/")[-1]


@dataclass
class AppConfig:
    storage_state_file: Path
    throttle: ThrottleConfig
    cover_letter_default: str
    resumes: list[ResumeConfig]

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

    account = _require(raw, "account", "корневой раздел")
    storage_state_file = PROJECT_ROOT / _require(account, "storage_state_file", "account")

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
        search_raw = _require(r, "search", context)
        search = SearchFilters(
            text=_require(search_raw, "text", f"{context}.search"),
            area=search_raw.get("area"),
            salary_from=search_raw.get("salary_from"),
            experience=search_raw.get("experience"),
            schedule=search_raw.get("schedule"),
            exclude_employers=search_raw.get("exclude_employers") or [],
            exclude_keywords=search_raw.get("exclude_keywords") or [],
        )

        resumes.append(
            ResumeConfig(
                id=resume_id,
                resume_url=resume_url,
                search=search,
                cover_letter=r.get("cover_letter"),
            )
        )

    return AppConfig(
        storage_state_file=storage_state_file,
        throttle=throttle,
        cover_letter_default=cover_letter_default,
        resumes=resumes,
    )


def load_config_or_exit(path: str | Path) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(1)
