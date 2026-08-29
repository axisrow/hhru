"""Планирование пула резюме под кластеры вакансий (#754, эпик #750).

Чистая функция без браузера/history — параллель к слою "browser flow" модулей
``copy_resume.py``/``create_resume.py``: здесь решается ТОЛЬКО что нужно
создать, сама материализация (клик "Дублировать") — в
``commands/resume_pool.py`` через уже существующий ``copy_resume_on_hh``.

Пул материализуется через ``copy-resume`` от одного заполненного базового
резюме (``--source``), НЕ через ``create-resume``: тот создаёт пустой
черновик визардом (только title/area, без единой записи опыта), а applying
адаптивного содержимого (issue #769) редактирует/дополняет уже существующие
записи опыта, не создаёт их с нуля. См. корректировку к issue #754
(комментарий в issue) — исходный текст issue ссылался на create_resume.py
как на равноценную альтернативу, это неверно.

Число кластеров сознательно НЕ хардкодится: код итерирует
``resume_clusters.CLUSTERS`` и использует ``len(...)`` динамически. Сегодня
там 4 записи (research #752), но правка ``resume_clusters.py`` не должна
требовать правки этого модуля.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ResumeConfig
from .resume_clusters import CLUSTERS, ResumeCluster


@dataclass(frozen=True)
class PoolPlanItem:
    """Одна недостающая копия: кластер, под который она создаётся, и slug,
    под которым она попадёт в config.yaml (--write-config) или в подсказанный
    YAML-сниппет."""

    cluster: ResumeCluster
    slug: str


@dataclass(frozen=True)
class PoolPlan:
    """Итог планирования: что уже покрыто, что нужно создать, что обрезано лимитом.

    ``covered`` — кластеры, для которых в конфиге уже есть резюме с этим
    ``cluster`` (идемпотентность повторного прогона — #754 DoD "каждое резюме
    пула связано со своим кластером", повторный запуск не плодит дубли).
    ``items`` — то, что реально будет создано за этот прогон (после обрезки
    лимитом). ``missing_total`` — сколько кластеров не покрыто ДО обрезки
    лимитом (для сообщений вида "N из M").
    """

    items: tuple[PoolPlanItem, ...]
    covered: tuple[ResumeCluster, ...] = field(default_factory=tuple)
    missing_total: int = 0


def _slug_for(source: ResumeConfig, cluster: ResumeCluster) -> str:
    return f"{source.id}-{cluster.key}"


def build_pool_plan(
    resumes: list[ResumeConfig],
    source: ResumeConfig,
    limit: int | None = None,
) -> PoolPlan:
    """Строит план материализации пула для ``source`` относительно ``resumes``
    (текущий полный список резюме конфига, включая ``source`` самого).

    ``limit`` — верхняя граница числа реально создаваемых копий за прогон
    (защитный предел команды, #754 п.6 — троттлинга у copy-resume нет вовсе).
    ``None`` или отсутствие эквивалентны "без искусственного предела сверх
    числа недостающих кластеров" — план всё равно ограничен
    ``len(missing)``, лимит здесь может только УМЕНЬШИТЬ план, никогда не
    увеличить его сверх реально недостающих кластеров.
    """
    covered_keys = {r.cluster for r in resumes if r.cluster is not None}
    covered = tuple(c for c in CLUSTERS if c.key in covered_keys)
    missing = tuple(c for c in CLUSTERS if c.key not in covered_keys)

    selected = missing if limit is None else missing[: max(limit, 0)]
    items = tuple(PoolPlanItem(cluster=c, slug=_slug_for(source, c)) for c in selected)

    return PoolPlan(items=items, covered=covered, missing_total=len(missing))


__all__ = ["PoolPlan", "PoolPlanItem", "build_pool_plan"]
