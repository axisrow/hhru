from __future__ import annotations

import pytest

from hhru_bot import drift
from hhru_bot.browser import PageStateIndeterminate, labelled_field

pytestmark = pytest.mark.unit


class _FakeLabel:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class _CannedPage:
    """Страница с искусственным дрейфом: ожидаемый data-qa отсутствует."""

    url = "https://hh.ru/resume/0123456789abcdef0123456789abcdef"

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.evaluate_args: list | None = None

    def evaluate(self, js, arg=None):  # noqa: ANN001, ANN201
        self.evaluate_args = arg
        return self._payload

    def get_by_label(self, label: str, *, exact: bool = True) -> _FakeLabel:  # noqa: ARG002
        # Имитируем дрейф: подпись больше не разрешается однозначно.
        return _FakeLabel(2)


def _drift_payload() -> dict:
    return {
        "url": "https://hh.ru/applicant/vacancy_response",
        "candidates": [
            {
                "qa": "vacancy-response-letter-toggle2",
                "tag": "button",
                "role": "",
                "label": "",
                "text": "Пусть работодатель напишет на ivan.petrov@example.com",
                "classes": "magritte-hash1",
                "visible": True,
                "score": 28,
            },
            {
                "qa": "vacancy-actions-company",
                "tag": "a",
                "role": "",
                "label": "ООО Ромашка",
                "text": "ООО Ромашка",
                "classes": "",
                "visible": True,
                "score": 17,
            },
        ],
        "fragment": (
            "<button data-qa='vacancy-response-letter-toggle2' "
            "data-phone='+7 912 345 67 89'>Пишите на ivan.petrov@example.com, "
            "resume 0123456789abcdef0123456789abcdef</button>"
        ),
    }


@pytest.fixture(autouse=True)
def _reset_drift_context():
    drift.begin_drift_session("")


@pytest.fixture()
def drift_log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(drift.logging_setup, "LOG_DIR", tmp_path)
    return tmp_path


def test_mask_personal_data_masks_structured_pii():
    text = (
        "iv.petrov@example.com и +7 912 345 67 89, id 1234567890123, "
        "hex 0123456789abcdef0123456789abcdef"
    )
    masked = drift.mask_personal_data(text)
    assert "example.com" not in masked
    assert "912" not in masked
    assert "1234567890123" not in masked
    assert "0123456789abcdef" not in masked
    assert "<email-1>" in masked
    assert "<телефон-1>" in masked
    assert "<id-1>" in masked
    assert "<hex-id-1>" in masked


def test_registry_lookup_finds_selector_name():
    selector = "[data-qa='vacancy-response-letter-toggle']"
    name = drift.registry_lookup(selector)
    assert name == "apply_form.APPLY_COVER_LETTER_TOGGLE"


def test_emit_drift_report_prints_full_package(drift_log_dir, capsys):
    drift.begin_drift_session("apply")
    drift.note_drift_step(
        "navigate_to_response_form",
        expected=("[data-qa='vacancy-response-letter-toggle']",),
        screen="vacancy_response",
    )
    page = _CannedPage(_drift_payload())
    body_path = drift.emit_drift_report(page, RuntimeError("форма не найдена"))

    out = capsys.readouterr().out
    assert body_path is not None and body_path.exists()
    assert "[DRIFT] Похоже на дрейф DOM hh.ru" in out
    assert "apply" in out and "navigate_to_response_form" in out
    # Ожидаемый селектор — из реестра, с именем.
    assert "apply_form.APPLY_COVER_LETTER_TOGGLE" in out
    # URL и фрагмент замаскированы: реальных email/телефона/hex-id нет.
    assert "example.com" not in out
    assert "912" not in out
    assert "0123456789abcdef" not in out
    # Готовая команда создания ишью с записанным body-файлом.
    assert "gh issue create --title " in out
    assert f"--body-file {body_path}" in out

    body = body_path.read_text(encoding="utf-8")
    assert "## Что наблюдалось" in body
    assert "## Ожидалось" in body
    assert "example.com" not in body
    assert "+7" not in body


def test_note_drift_step_feeds_expected_selectors(drift_log_dir, capsys):
    # Продакшн-подключение: apply/steps.py::navigate_to_response_form
    # фиксирует шаг и ожидаемые селекторы ДО работы с формой — центральные
    # точки отказа browser.py подхватывают их без явной передачи.
    drift.begin_drift_session("apply")
    drift.note_drift_step(
        "navigate_to_response_form",
        expected=("[data-qa='vacancy-response-letter-toggle']",),
        screen="vacancy_response",
    )
    page = _CannedPage(_drift_payload())
    drift.emit_drift_report(page, RuntimeError("форма не найдена"))
    out = capsys.readouterr().out
    assert "apply_form.APPLY_COVER_LETTER_TOGGLE" in out
    assert "navigate_to_response_form" in out
    assert "vacancy_response" in out


def test_emit_masks_employer_text(drift_log_dir, capsys):
    drift.begin_drift_session("probe")
    page = _CannedPage(_drift_payload())
    drift.emit_drift_report(page, RuntimeError("x"), expected_selectors=("[data-qa='x']",))
    out = capsys.readouterr().out
    assert "ООО Ромашка" not in out
    assert "<работодатель>" in out


def test_emit_degrades_without_dom(drift_log_dir, capsys):
    class _DeadPage:
        url = "https://hh.ru/x"

        def evaluate(self, js, arg=None):  # noqa: ANN001, ANN201
            raise RuntimeError("page closed")

    drift.begin_drift_session("bump")
    body_path = drift.emit_drift_report(
        _DeadPage(), RuntimeError("таймаут"), expected_selectors=("[data-qa='y']",)
    )
    out = capsys.readouterr().out
    assert body_path is not None
    assert "Кандидаты в DOM не сняты" in out
    assert "https://hh.ru/x" in out


def test_labelled_field_fail_closed_with_drift_package(drift_log_dir, capsys):
    drift.begin_drift_session("fill-form")
    page = _CannedPage(_drift_payload())
    with pytest.raises(PageStateIndeterminate):
        labelled_field(page, "Организация")
    out = capsys.readouterr().out
    assert "[DRIFT] Похоже на дрейф DOM hh.ru" in out
    assert "fill-form" in out
