"""Фиксированные кластеры вакансий для адаптивных резюме (эпик #750).

Источник — research #752 (замер 2026-08-29, 432 уникальные вакансии живой
выдачи, READ-only ``search --dry-run``). Список кластеров закрыт этим
research'ем: **четыре**, а не три из исходной гипотезы эпика. Data Science/ML —
самостоятельный кластер, а НЕ часть AI/LLM (61 вакансия матчится только туда,
ни разу не задевая LLM-термины) — см. тело #752 и #753, не схлопывать.
DevOps/Infra проверен и отвергнут: в выборке нет самостоятельных позиций,
только сопутствующий стек внутри AI/Data-вакансий.

Каждый кластер несёт ``tags`` — список тегов, совпадающий по словарю с
``tags`` в ``config_sections/candidate_facts.py`` (issue #751): отбор фактов
под кластер (#753/#754) сопоставляет факт с кластером через пересечение
множеств тегов, а не через свободный текст.

``keywords`` — характерные термины кластера из таблицы #752, используются как:
  - словарь для LLM-промпта (акценты формулировок),
  - fallback-выбор тегов при отсутствии LLM,
  - лексика для собранной ``VacancyCard`` в проверке ``resume_match_score``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResumeCluster:
    key: str
    title: str
    tags: tuple[str, ...]
    keywords: tuple[str, ...]
    median_salary_rub: int


AI_LLM = ResumeCluster(
    key="ai_llm",
    title="AI/LLM-инженер",
    tags=("ai", "llm", "nlp"),
    keywords=(
        "LLM",
        "NLP",
        "промпт",
        "LangChain",
        "RAG",
        "GPT",
        "ИИ-агенты",
    ),
    median_salary_rub=200_000,
)

DATA_SCIENCE = ResumeCluster(
    key="data_science",
    title="Data Science / ML",
    tags=("data_science", "ml"),
    keywords=(
        "pandas",
        "PyTorch",
        "TensorFlow",
        "регрессия",
        "классификация",
        "ML-модели",
    ),
    median_salary_rub=337_500,
)

DATA_ENGINEER = ResumeCluster(
    key="data_engineer",
    title="Data-инженер",
    tags=("data_engineer", "etl"),
    keywords=(
        "ETL",
        "DWH",
        "Airflow",
        "Spark",
        "ClickHouse",
        "Kafka",
        "data pipeline",
    ),
    median_salary_rub=270_000,
)

PYTHON_BACKEND = ResumeCluster(
    key="python_backend",
    title="Python-бэкенд",
    tags=("backend", "python"),
    keywords=(
        "Django",
        "FastAPI",
        "Flask",
        "REST API",
        "PostgreSQL",
        "микросервисы",
    ),
    median_salary_rub=127_500,
)

#: Порядок фиксирован — используется и как порядок вывода в CLI-справке.
CLUSTERS: tuple[ResumeCluster, ...] = (AI_LLM, DATA_SCIENCE, DATA_ENGINEER, PYTHON_BACKEND)

CLUSTERS_BY_KEY: dict[str, ResumeCluster] = {c.key: c for c in CLUSTERS}


def cluster_by_key(key: str) -> ResumeCluster:
    try:
        return CLUSTERS_BY_KEY[key]
    except KeyError:
        available = ", ".join(CLUSTERS_BY_KEY)
        raise ValueError(f"неизвестный кластер {key!r}, доступны: {available}") from None


__all__ = [
    "AI_LLM",
    "DATA_SCIENCE",
    "DATA_ENGINEER",
    "PYTHON_BACKEND",
    "CLUSTERS",
    "CLUSTERS_BY_KEY",
    "ResumeCluster",
    "cluster_by_key",
]
