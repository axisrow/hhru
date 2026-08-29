"""Парсер опциональной resume-секции ``cluster`` -> str | None (issue #754).

Часть эпика #750 (адаптивные резюме: пул под кластеры). Связывает резюме
конфига с одним из фиксированных кластеров вакансий (``resume_clusters.py``,
#752/#753): значение — ключ ``ResumeCluster.key`` (например, ``"ai_llm"``).

Секция опциональна: без неё ``ResumeConfig.cluster = None`` (резюме без
привязки к кластеру — обратная совместимость, дефолт для всех резюме,
заведённых до #754).

Значение валидируется через ``resume_clusters.cluster_by_key`` — неизвестный
ключ отклоняется здесь, при загрузке конфига, а не молча по цепочке позже.
"""

from __future__ import annotations

from ..config import ConfigError
from ._registry import register


@register("cluster")
def parse_cluster(raw, context: str) -> str | None:
    """raw — значение поля ``cluster`` резюме (строка-ключ кластера или None)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigError(f"Поле '{context}' должно быть строкой")
    from ..resume_clusters import cluster_by_key

    # Fail-closed: неизвестный ключ отклоняется на загрузке конфига, а не
    # молча просачивается в resume_pool/роутер как непокрытый кластер.
    # cluster_by_key поднимает ValueError (общая утилита resume_clusters.py,
    # не завязана на ConfigError секций) — конвертируем в ConfigError здесь,
    # на границе парсера, как и остальные config_sections/*.py.
    try:
        cluster_by_key(raw)
    except ValueError as exc:
        raise ConfigError(f"Поле '{context}': {exc}") from exc
    return raw
