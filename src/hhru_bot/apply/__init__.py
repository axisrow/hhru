"""Пакет отклика на вакансию: pipeline + шаги + probe + letter.

Публичный API (apply_to_vacancy, ApplyResult, render_cover_letter) переэкспортирован
здесь для обратной совместимости — `from hhru_bot.apply import apply_to_vacancy`
работает как прежде. Внутренние модули (dedup/steps/success/probe/letter) —
точки расширения для feature-ишью (#3/#6/#7/#8/#17), каждый владеет своим файлом.
"""

from __future__ import annotations

from .letter import render_cover_letter
from .pipeline import ApplyContext, ApplyResult, apply_to_vacancy
from .probe import ProbeHook
from .questions import QuestionDetection, detect_questions

__all__ = [
    "ApplyContext",
    "ApplyResult",
    "ProbeHook",
    "QuestionDetection",
    "apply_to_vacancy",
    "detect_questions",
    "render_cover_letter",
]

# Cross-resume routing (#418) remains pure and optional for callers that want
# account-wide planning without changing the existing submit pipeline.
from .router import MergedVacancy, ResumeSelection, merge_vacancies, route_vacancies

__all__ += ["MergedVacancy", "ResumeSelection", "merge_vacancies", "route_vacancies"]
