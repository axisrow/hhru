"""Characterization-тесты чистой логики сопроводительного письма (apply.py).

Поведение render_cover_letter не должно измениться после декомпозиции apply.
"""

from __future__ import annotations

from hhru_bot.apply import render_cover_letter
from hhru_bot.search import VacancyCard


def _card(title: str, company: str) -> VacancyCard:
    return VacancyCard(vacancy_id="1", title=title, company=company, url="https://hh.ru/vacancy/1")


def test_render_cover_letter_substitutes_placeholders():
    template = "Вакансия: {vacancy_title}, компания: {company_name}"
    rendered = render_cover_letter(template, _card("Python Dev", "Acme"))
    assert rendered == "Вакансия: Python Dev, компания: Acme"


def test_render_cover_letter_no_placeholders():
    assert render_cover_letter("Привет", _card("X", "Y")) == "Привет"


def test_render_cover_letter_multiline_example():
    template = "Здравствуйте!\nВакансия: {vacancy_title}"
    assert render_cover_letter(template, _card("DevOps", "Z")) == (
        "Здравствуйте!\nВакансия: DevOps"
    )
