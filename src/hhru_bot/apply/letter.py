"""Сопроводительное письмо. #17 расширит выбор провайдера (шаблон/AI) здесь.

Владелец: #17. pipeline и другие шаги этот файл не трогают.
"""

from __future__ import annotations

from ..search import VacancyCard


def render_cover_letter(template: str, vacancy: VacancyCard) -> str:
    return template.format(vacancy_title=vacancy.title, company_name=vacancy.company)
