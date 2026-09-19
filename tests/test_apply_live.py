"""Маппинг сценария apply-live на примитивы S2/S4 (#1162): пре-кликовая часть.

Сценарий гоняется с FakeChannel — stateful-моделью страницы вакансии (первые
PR стека: гейты до клика + dry-run). Вердикты серой зоны #207, форма, письмо
и submit покрываются вторым PR стека на той же модели с флагами формы.
"""

from __future__ import annotations

import pytest

from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.history import SKIP_REASONS
from hhru_bot.live.scenarios import (
    ACTION_CHECK,
    ACTION_CLICK,
    ACTION_FILL,
    ACTION_GET_STATE,
    ACTION_WAIT,
    FORM_WAIT_TIMEOUT_MS,
    ApplyLiveResult,
    PrimitiveError,
    apply_via_live,
)
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit

VACANCY_ID = "136789544"
VACANCY_URL = f"https://hh.ru/vacancy/{VACANCY_ID}"
RESUME_ID = "0123abcd"
TEXTAREA = "vacancy-response-popup-form-letter-input"
SUBMIT_MARKER = "responded-success-attach-cover-letter"


class Verdict:
    """Форма вердикта apply.verify (found/not_found/indeterminate)."""

    def __init__(self, status: str, detail: str = "d") -> None:
        self.status = status
        self.detail = detail

    @property
    def found(self) -> bool:
        return self.status == "found"

    @property
    def indeterminate(self) -> bool:
        return self.status == "indeterminate"


class FakeFormChannel:
    """Модель страницы вакансии + формы отклика глазами примитивов канала."""

    def __init__(
        self,
        *,
        url: str = VACANCY_URL,
        login_found: bool = False,
        already: bool = False,
        button_present: bool = True,
        modal_after_click: bool = True,
        page_form: bool = False,
        question_count: int = 0,
        question_text: str = "Ваш опыт с LLM?",
        picker_present: bool = True,
        option_present: bool = True,
        panel_closes: bool = True,
        toggle_present: bool = True,
        textarea_after_click: bool = True,
        fill_ok: bool = True,
        submit_markers: bool = True,
        apply_click_error: PrimitiveError | None = None,
        submit_click_error: PrimitiveError | None = None,
    ) -> None:
        self.url = url
        self.login_found = login_found
        self.already = already
        self.button_present = button_present
        self.modal_after_click = modal_after_click
        self.page_form = page_form
        self.question_count = question_count
        self.question_text = question_text
        self.picker_present = picker_present
        self.option_present = option_present
        self.panel_closes = panel_closes
        self.toggle_present = toggle_present
        self.textarea_after_click = textarea_after_click
        self.fill_ok = fill_ok
        self.submit_markers = submit_markers
        self.apply_click_error = apply_click_error
        self.submit_click_error = submit_click_error
        self.apply_clicked = False
        self.toggle_clicked = False
        self.option_clicked = False
        self.panel_open = False
        self.submitted = False
        self.filled_text: str | None = None
        self.calls: list[tuple[str, dict]] = []
        self.verdicts: list[str] = []

    # -- примитивы ------------------------------------------------------------

    def get_state(self) -> dict:
        # Контракт LiveChannel.get_state(): уже развёрнутый {url, ...} (обёртку
        # 'page' транспорта снимает сам канал, как 'element' у check).
        self.calls.append((ACTION_GET_STATE, {}))
        return {"url": self.url, "title": "Вакансия", "readyState": "complete"}

    def check(self, selector: str) -> dict:
        self.calls.append((ACTION_CHECK, {"selector": selector}))
        found, text, count = self._present(selector)
        return {"found": found, "visible": found, "matchCount": count, "text": text}

    def click(self, selector: str, wait_for: dict | None = None, allow_apply: bool = False) -> dict:
        self.calls.append(
            (ACTION_CLICK, {"selector": selector, "waitFor": wait_for, "allowApply": allow_apply})
        )
        if "vacancy-response-link-top" in selector and "again" not in selector:
            if self.apply_click_error is not None:
                raise self.apply_click_error
            self.apply_clicked = True
            if self.modal_after_click:
                self.panel_open = False
        elif "resume-title" in selector:
            # Триггер пикера: открыть/закрыть панель.
            if self.panel_open and self.panel_closes:
                self.panel_open = False
            elif not self.panel_open:
                self.panel_open = True
        elif "magritte-select-option-" in selector:
            self.option_clicked = True
        elif "add-cover-letter" in selector or "letter-toggle" in selector:
            if self.toggle_present:
                self.toggle_clicked = True
        elif "vacancy-response-submit" in selector:
            if self.submit_click_error is not None:
                raise self.submit_click_error
            self.submitted = True
        return {"clicked": True, "wait": {"met": self._wait_met(wait_for)}}

    def click_wait_met(self, selector: str, wait_for: dict, allow_apply: bool = False) -> bool:
        # Зеркало LiveChannel.click_wait_met: факт исполнения post-click условия.
        result = self.click(selector, wait_for=wait_for, allow_apply=allow_apply)
        return bool((result.get("wait") or {}).get("met", False))

    def wait(self, selector: str, state: str, timeout_ms: int) -> bool:
        self.calls.append(
            (ACTION_WAIT, {"selector": selector, "state": state, "timeoutMs": timeout_ms})
        )
        present = self._present(selector)[0]
        return present if state == "visible" else not present

    def fill(self, selector: str, text: str) -> dict:
        self.calls.append((ACTION_FILL, {"selector": selector, "text": text}))
        self.filled_text = text if self.fill_ok else None
        return {"filled": self.fill_ok, "length": len(text) if self.fill_ok else 0}

    # -- модель DOM -----------------------------------------------------------

    def _present(self, selector: str) -> tuple[bool, str, int]:
        if "vacancy-response-link-top-again" in selector or "view-topic" in selector:
            return self.already, "", 1 if self.already else 0
        if "vacancy-response-link-top" in selector:
            return self.button_present, "", 1
        if "account-login-form" in selector:
            return self.login_found, "", 1 if self.login_found else 0
        if "task-body" in selector:
            return self.question_count > 0 and self._form_open(), "", self.question_count
        if "task-question" in selector:
            return (
                bool(self.question_count) and self._form_open(),
                self.question_text,
                self.question_count,
            )
        if "resume-title" in selector:
            return self.picker_present and self._form_open(), "", 1
        if "magritte-select-option-" in selector:
            return self.option_present and self.panel_open, "", 1
        if "drop-base" in selector:
            return self.panel_open, "", 1
        if TEXTAREA in selector or "vacancy-response-form-letter-input" in selector:
            return self._textarea_visible(), "", 1
        if "add-cover-letter" in selector or "letter-toggle" in selector:
            return self.toggle_present and self._form_open() and not self._textarea_visible(), "", 1
        if "RESPONSE_MODAL_FORM_ID" in selector:
            # Модалка существует только в modal-shape: page-shape её не рендерит.
            return self.modal_after_click and self.apply_clicked, "", 1
        if "vacancy-response-submit" in selector:
            return self._form_open() and not self.submitted, "", 1
        if (
            SUBMIT_MARKER in selector
            or "vacancy-response-sent" in selector
            or "response-success" in selector
        ):
            return self.submitted and self.submit_markers, "", 1
        return False, "", 0

    def _form_open(self) -> bool:
        return (self.modal_after_click or self.page_form) and self.apply_clicked

    def _textarea_visible(self) -> bool:
        if not self._form_open():
            return False
        if self.page_form:
            return True
        if self.modal_after_click:
            return self.textarea_after_click or self.toggle_clicked
        return False

    def _wait_met(self, wait_for: dict | None) -> bool:
        if not wait_for:
            return True
        present = self._present(str(wait_for.get("selector", "")))[0]
        return present if wait_for.get("state") == "visible" else not present


def _resume() -> ResumeConfig:
    return ResumeConfig(
        id="resume",
        resume_url=f"https://hh.ru/resume/{RESUME_ID}",
        search=SearchFilters(text="python"),
    )


def _vacancy() -> VacancyCard:
    return VacancyCard(vacancy_id=VACANCY_ID, title="Инженер", company="YADRO", url=VACANCY_URL)


def _run(channel: FakeFormChannel, *, dry_run: bool = False, verify=None, **kwargs):
    return apply_via_live(
        channel, _resume(), _vacancy(), "Здравствуйте!", dry_run, verify=verify, **kwargs
    )


def _verify_of(status: str):
    def verify(vacancy_id: str, resume_id: str):
        assert vacancy_id == VACANCY_ID and resume_id == RESUME_ID
        return Verdict(status)

    return verify


def _actions(channel: FakeFormChannel) -> list[str]:
    return [action for action, _payload in channel.calls]


def test_action_names_and_budgets_guard() -> None:
    # Страж контракта: fill_element — примитив S4; бюджеты умещаются в
    # response_timeout сервера (30 с), иначе ответ канала сгорает по таймауту.
    from hhru_bot.live.scenarios import ACTION_FILL

    assert ACTION_FILL == "fill_element"
    assert FORM_WAIT_TIMEOUT_MS < 30_000


def test_dry_run_stops_before_any_click() -> None:
    channel = FakeFormChannel()
    result = _run(channel, dry_run=True, require_resume_select=True)

    assert (result.success, result.acted, result.skipped) == (True, False, False)
    assert "dry-run" in result.reason
    assert ACTION_CLICK not in _actions(channel)
    assert ACTION_FILL not in _actions(channel)


def test_wrong_url_and_login_fail_before_click() -> None:
    channel = FakeFormChannel(url="https://hh.ru/applicant/resumes")
    result = _run(channel)
    assert (result.success, result.acted) == (False, False)
    assert "не на странице вакансии" in result.reason

    channel = FakeFormChannel(login_found=True)
    result = _run(channel)
    assert (result.success, result.acted) == (False, False)
    assert "Сессия недействительна" in result.reason
    assert ACTION_CLICK not in _actions(channel)


def test_already_responded_marker_is_skip_not_fail() -> None:
    channel = FakeFormChannel(already=True)
    result = _run(channel)

    assert (result.success, result.skipped, result.acted) == (False, True, False)
    assert result.skip_reason == SKIP_REASONS.ALREADY_APPLIED
    assert ACTION_CLICK not in _actions(channel)


def test_missing_apply_button_fails_plain() -> None:
    channel = FakeFormChannel(button_present=False)
    result = _run(channel)

    assert (result.success, result.acted, result.uncertain) == (False, False, False)
    assert "кнопка отклика не найдена" in result.reason


def test_result_is_progress_compatible() -> None:
    # ApplyProgress.finish() классифицирует по структурным флагам —
    # ApplyLiveResult обязан работать с ним без адаптеров.
    from hhru_bot.commands.supervision import ApplyProgress

    progress = ApplyProgress()
    progress.begin_attempt()
    assert (
        progress.finish(ApplyLiveResult(RESUME_ID, VACANCY_ID, True, "ok", acted=True)) == "success"
    )
    progress.begin_attempt()
    assert (
        progress.finish(
            ApplyLiveResult(RESUME_ID, VACANCY_ID, False, "x", acted=True, uncertain=True)
        )
        == "uncertain"
    )
    progress.begin_attempt()
    assert (
        progress.finish(
            ApplyLiveResult(
                RESUME_ID,
                VACANCY_ID,
                False,
                "x",
                skipped=True,
                skip_reason=SKIP_REASONS.HAS_QUESTIONS,
            )
        )
        == "skipped"
    )
    assert (progress.applied_count, progress.uncertain_count, progress.skipped_count) == (1, 1, 1)
