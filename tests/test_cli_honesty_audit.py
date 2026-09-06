"""Аудит честности CLI (#1002-класс): CLI не должен отдавать агенту ложную
картину страницы — ни сырым HTML, ни селектором вместо отрисованной подписи,
ни советом несуществующего поля."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from hhru_bot import resume_education
from hhru_bot.browser import PageStateIndeterminate
from hhru_bot.commands._common_resume_guidance import print_common_resume_guidance

pytestmark = pytest.mark.unit


class _Resume:
    id = "abc123"


def test_guidance_points_to_common_command(capsys):
    """Прежний текст лгал «CLI пока не умеет сохранять общие данные» и звал
    заполнять workTicket, которого на common-визарде нет (#997)."""
    print_common_resume_guidance(_Resume(), include_publish=True)
    out = capsys.readouterr().out
    assert "hhru common --resume abc123" in out
    assert "publish-resume --resume abc123 --dry-run" in out
    assert "не умеет" not in out
    assert "workTicket" not in out
    assert "work-ticket" not in out


def test_work_ticket_help_names_rendered_label():
    """Help читают ДО запуска: семантика --work-ticket должна совпадать с
    рантайм-отказом #997, а не утверждать «Трудовая книжка» без оговорок."""
    from hhru_bot.cli import build_parser

    parser = build_parser()
    sub = parser._subparsers._group_actions[0].choices["common"]
    opt = next(a for a in sub._actions if "--work-ticket" in getattr(a, "option_strings", ()))
    assert "Разрешение на работу" in opt.help
    assert "не рендерится" in opt.help


class _ZeroPage:
    def locator(self, selector):  # noqa: ARG002
        return SimpleNamespace(count=lambda: 0)


def test_education_refusal_names_human_label_and_address():
    """Отказ называет поле подписью, селектор — только адресом в скобках."""
    with pytest.raises(PageStateIndeterminate) as exc:
        resume_education._field_locator(_ZeroPage(), "institution", additional=False)
    message = str(exc.value)
    assert "Название учебного заведения" in message
    assert "profile-education-university-input" in message

    with pytest.raises(PageStateIndeterminate) as exc:
        resume_education._field_locator(
            _ZeroPage(), "organization", additional=True, trigger_shape=True
        )
    message = str(exc.value)
    assert "Проводившая организация" in message
    assert "profile-education-additional-organization" in message


def test_transient_overlay_log_strips_html(caplog):
    """В лог идёт структура оверлея, не 12КБ outerHTML (#998-класс)."""
    from hhru_bot.transient_overlays import drain_transient_overlay_evidence

    item = {
        "type": "notification",
        "text": "Подтвердите отклик",
        "html": "<div>raw-markup-secret</div>",
        "visible": True,
    }

    class FakePage:
        def evaluate(self, _js):
            return [dict(item)]

    with caplog.at_level(logging.INFO, logger="hhru_bot.transient_overlays"):
        out = drain_transient_overlay_evidence(FakePage())
    assert out and out[0]["html"] == item["html"]
    assert "raw-markup-secret" not in caplog.text
    assert "html_bytes" in caplog.text
    assert "Подтвердите отклик" in caplog.text


def test_probe_stdout_has_no_raw_html():
    """probe negotiations печатал сырой outerHTML в stdout — материал #998.
    Сырая разметка теперь только в файл-дамп, в stdout — census-таблица."""
    from pathlib import Path

    import hhru_bot.commands.probe as probe_module

    src = Path(probe_module.__file__).read_text(encoding="utf-8")
    assert "RAW HTML fragment" not in src
    assert "outerHTML" not in src
    assert "subtree_controls_census" in src
