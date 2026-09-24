"""Маппинг сценария apply-live на примитивы S2/S4 (#1162).

Сценарий гоняется с FakeChannel — stateful-моделью страницы вакансии и формы
отклика (click мутирует флаги: модалка/панель/textarea/marкеры submit, как
реальный DOM после кликов). Вердикты серой зоны #207 проверяются по контракту
apply.verify: found/not_found/indeterminate — словарь не расширяется.
"""

from __future__ import annotations

import pytest

from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.history import SKIP_REASONS
from hhru_bot.live.scenarios import (
    ACTION_CHECK,
    ACTION_CLICK,
    ACTION_DISMISS_OVERLAY,
    ACTION_FILL,
    ACTION_GET_STATE,
    ACTION_LIST_OVERLAYS,
    ACTION_WAIT,
    FORM_WAIT_TIMEOUT_MS,
    PAGE_FORM_WAIT_TIMEOUT_MS,
    SUBMIT_WAIT_TIMEOUT_MS,
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
# Census модалки видимости (#1218, боевой census 2026-09-23): outer-узел safe
# с 2 close-контролами; inner-узел той же модалки — ambiguous (не тестируем
# dismiss'ом, см. test_visibility_overlay_unsafe...).
VISIBILITY_OVERLAY = {
    "id": "overlay-1",
    "type": "modal",
    "disposition": "safe",
    "closeControls": 2,
    "text": (
        "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме "
        "на «Видно всем работодателям»"
    ),
}


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
        hidden_warning: bool = False,
        picker_present: bool = True,
        option_present: bool = True,
        option_click_lost: bool = False,
        panel_closes: bool = True,
        toggle_present: bool = True,
        textarea_after_click: bool = True,
        fill_ok: bool = True,
        submit_markers: bool = True,
        apply_click_error: PrimitiveError | None = None,
        submit_click_error: PrimitiveError | None = None,
        fill_error: PrimitiveError | None = None,
        warning_check_error: PrimitiveError | None = None,
        warning_needs_form: bool = True,
        overlays: list[dict] | None = None,
        dismiss_error: PrimitiveError | None = None,
        dismiss_lost_effect: bool = False,
        overlays_error: PrimitiveError | None = None,
    ) -> None:
        self.url = url
        self.login_found = login_found
        self.already = already
        self.button_present = button_present
        self.modal_after_click = modal_after_click
        self.page_form = page_form
        self.question_count = question_count
        self.question_text = question_text
        self.hidden_warning = hidden_warning
        self.picker_present = picker_present
        self.option_present = option_present
        self.panel_closes = panel_closes
        self.toggle_present = toggle_present
        self.textarea_after_click = textarea_after_click
        self.fill_ok = fill_ok
        self.submit_markers = submit_markers
        self.apply_click_error = apply_click_error
        self.submit_click_error = submit_click_error
        self.fill_error = fill_error
        self.warning_check_error = warning_check_error
        # False = warning-узел в DOM и до открытия формы (#1215: свёрнутый
        # узел бывает и при применимой вакансии).
        self.warning_needs_form = warning_needs_form
        self.overlays = list(overlays or [])
        self.dismiss_error = dismiss_error
        # response_lost (#176): ответ потерян, но close-клик мог уйти —
        # dismiss_lost_effect моделирует «дошёл», False — «не дошёл».
        self.dismiss_lost_effect = dismiss_lost_effect
        # Отказ чтения census (#1220 DX): list_overlays рядом с отказом —
        # best-effort, мёртвый канал не меняет вердикт.
        self.overlays_error = overlays_error
        self.dismissed_ids: list[str] = []
        self.apply_clicked = False
        self.toggle_clicked = False
        self.option_clicked = False
        # Выбор не подтверждён, пока click по опции его не выставил; клик может
        # потеряться в окне гидрации (#858) — option_click_lost это моделирует.
        self.option_selected = False
        self.option_click_lost = option_click_lost
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
        # Отказ чтения warning'а видимости (ревью #1216): probe в обработчике
        # отказов обязан быть best-effort — моделируем на самом чтении.
        if self.warning_check_error is not None and "hidden-resume-warning" in selector:
            raise self.warning_check_error
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
            if not self.option_click_lost:
                self.option_selected = True
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
        if self.fill_error is not None:
            raise self.fill_error
        self.filled_text = text if self.fill_ok else None
        return {"filled": self.fill_ok, "length": len(text) if self.fill_ok else 0}

    def list_overlays(self) -> list[dict]:
        # Контракт LiveChannel.list_overlays(): развёрнутый список overlay-
        # словарей census (stage-1 ответ — один уровень, без обёрток).
        self.calls.append((ACTION_LIST_OVERLAYS, {}))
        if self.overlays_error is not None:
            raise self.overlays_error
        return [dict(overlay) for overlay in self.overlays]

    def dismiss_overlay(self, overlay_id: str) -> dict:
        if self.dismiss_error is not None:
            if self.dismiss_error.forwarded and self.dismiss_lost_effect:
                # response_lost с улетевшим кликом: эффект случился, ответ
                # потерян — канал отвечает ошибкой ПОСЛЕ применения эффекта.
                self._apply_dismiss(overlay_id)
            # Отказ исполнителя ДО клика (overlay_not_found/overlay_not_safe):
            # как у реального канала — PrimitiveError, состояние не меняется.
            raise self.dismiss_error
        self._apply_dismiss(overlay_id)
        return {
            "overlayId": overlay_id,
            "type": "modal",
            "disposition": "safe",
            "action": "clicked close control",
            "overlayGone": True,
            "elements": {"closeControls": 2},
        }

    def _apply_dismiss(self, overlay_id: str) -> None:
        self.dismissed_ids.append(overlay_id)
        # Модель боевого dismiss (#1218): модалка ушла, warning исчез,
        # перекрытые ею пикер/опция снова читаются.
        self.overlays = []
        self.hidden_warning = False
        self.option_present = True
        self.picker_present = True

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
        if "hidden-resume-warning" in selector:
            # #1214: collapsible внутри формы отклика; census-текст — требование
            # hh.ru поменять видимость (как в боевых дампах 2026-09-09).
            return (
                self.hidden_warning and (self._form_open() or not self.warning_needs_form),
                "поменяйте видимость резюме на «Видно всем работодателям»",
                1 if self.hidden_warning else 0,
            )
        if "resume-title" in selector:
            return self.picker_present and self._form_open(), "", 1
        if "aria-selected" in selector:
            # Факт выбора опции: aria-selected="true" через атрибутный матчинг.
            # Бой 2026-09-25: Magritte размонтирует drop-панель при закрытии —
            # опции (и их aria-selected) есть в DOM только при ОТКРЫТОЙ панели;
            # чтение после закрытия давало ложный «нет aria-selected».
            return self.option_selected and self.panel_open, "", 1
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
    # Страж контракта: таблица ACTION_* = ровно allowlist расширения
    # (content.js ACTION_ALLOWLIST / background.js RELAY_ACTIONS); бюджеты
    # умещаются в response_timeout сервера (30 с), иначе ответ канала
    # сгорает по таймауту.
    assert {
        ACTION_GET_STATE,
        ACTION_CHECK,
        ACTION_CLICK,
        ACTION_WAIT,
        ACTION_FILL,
        ACTION_LIST_OVERLAYS,
        ACTION_DISMISS_OVERLAY,
    } == {
        "get_page_state",
        "check_element",
        "click_element",
        "wait_element",
        "fill_element",
        "list_overlays",
        "dismiss_overlay",
    }
    assert max(FORM_WAIT_TIMEOUT_MS, PAGE_FORM_WAIT_TIMEOUT_MS, SUBMIT_WAIT_TIMEOUT_MS) < 30_000


def test_success_modal_path_maps_to_primitives() -> None:
    channel = FakeFormChannel()
    result = _run(channel, require_resume_select=False)

    assert (result.success, result.acted, result.uncertain, result.skipped) == (
        True,
        True,
        False,
        False,
    )
    assert "подтверждён" in result.reason
    # Клик кнопки отклика и submit идут с явной авторизацией apply-шага.
    clicks = [payload for action, payload in channel.calls if action == ACTION_CLICK]
    assert all(payload["allowApply"] for payload in clicks)
    assert channel.apply_clicked and channel.submitted
    assert channel.filled_text == "Здравствуйте!"
    # waitFor submit-клика — композит success-маркеров (только позитивные).
    submit_wait = clicks[-1]["waitFor"]
    assert SUBMIT_MARKER in submit_wait["selector"]
    assert "vacancy-response-link-top" not in submit_wait["selector"]
    assert submit_wait["timeoutMs"] == SUBMIT_WAIT_TIMEOUT_MS
    # Один клик на шаг: кнопка отклика, submit (пикер выключен — sole-resume).
    assert len(clicks) == 2


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


def test_lookalike_host_fails_url_gate() -> None:
    # endswith("hh.ru") пропускал бы evil-hh.ru — гейт хоста строгий
    # (ровно hh.ru или поддомен), похожий домен не проходит до клика.
    channel = FakeFormChannel(url=f"https://evil-hh.ru/vacancy/{VACANCY_ID}")
    result = _run(channel)

    assert (result.success, result.acted) == (False, False)
    assert "не на странице вакансии" in result.reason


def test_page_shape_reached_when_modal_absent() -> None:
    # Модалки нет — hh.ru открыл полную страницу /applicant/vacancy_response.
    channel = FakeFormChannel(modal_after_click=False, page_form=True)
    result = _run(channel, require_resume_select=False)

    assert (result.success, result.acted) == (True, True)
    waits = [payload for action, payload in channel.calls if action == ACTION_WAIT]
    assert any(
        payload["timeoutMs"] == PAGE_FORM_WAIT_TIMEOUT_MS and "letter-input" in payload["selector"]
        for payload in waits
    )


def _grey_case(modal: bool, page: bool, verify, *, expect):
    channel = FakeFormChannel(modal_after_click=modal, page_form=page)
    result = _run(channel, verify=verify, require_resume_select=False)
    assert (result.success, result.acted, result.uncertain) == expect
    assert not channel.submitted
    return result, channel


def test_grey_zone_no_form_not_found_is_failed_not_uncertain() -> None:
    # one-click не ушёл (not_found): вердикт сайта снимает неопределённость,
    # acted остаётся (кнопка кликнута — пауза троттла заслужена).
    result, channel = _grey_case(False, False, _verify_of("not_found"), expect=(False, True, False))
    assert "отклика в /applicant/negotiations нет" in result.reason
    assert channel.verdicts == []


def test_grey_zone_no_form_found_reconciles_success() -> None:
    # one-click реально отправил отклик: внешний источник подтверждает success.
    result, _channel = _grey_case(False, False, _verify_of("found"), expect=(True, True, False))
    assert "подтвердила отклик" in result.reason


def test_grey_zone_no_form_indeterminate_is_uncertain() -> None:
    result, _channel = _grey_case(
        False, False, _verify_of("indeterminate"), expect=(False, True, True)
    )
    assert "исход неопределён" in result.reason


def test_grey_zone_without_verifier_is_uncertain_acting() -> None:
    result, _channel = _grey_case(False, False, None, expect=(False, True, True))


def test_questions_skip_and_queue_channel_never_answers() -> None:
    channel = FakeFormChannel(question_count=2)
    result = _run(channel, require_resume_select=False)

    assert (result.success, result.skipped, result.acted) == (False, True, False)
    assert result.skip_reason == SKIP_REASONS.HAS_QUESTIONS
    assert result.question_texts == ["Ваш опыт с LLM?"]
    assert channel.filled_text is None and not channel.submitted
    assert "вопросы в очередь" in result.reason


def test_hidden_resume_warning_is_skip_with_human_reason() -> None:
    # Боевой случай #1214 (testing, 2026-09-23): скрытое резюме недоступно
    # в пикере формы, warning видимости в DOM. Вердикт — skip (vacancy не
    # «сгорает»), флаги чистые; клик по переключателю видимости — мутация,
    # не выполняется никогда.
    channel = FakeFormChannel(hidden_warning=True, option_present=False)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.skipped, result.acted, result.uncertain) == (
        False,
        True,
        False,
        False,
    )
    assert result.skip_reason == SKIP_REASONS.RESUME_VISIBILITY
    assert "видимость" in result.reason and "вручную" in result.reason
    assert "поменяйте видимость резюме" in result.reason
    # Только клик кнопки отклика и триггера пикера: дальше формы не идём.
    clicks = [payload for action, payload in channel.calls if action == ACTION_CLICK]
    assert len(clicks) == 2
    assert channel.filled_text is None and not channel.submitted


def test_warning_absent_at_failed_selection_keeps_site_verdict() -> None:
    # Опции нет, но и warning нет: вердикт сайта ("нет в пикере"), не skip.
    channel = FakeFormChannel(option_present=False)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.skipped, result.uncertain) == (False, False, False)
    assert "нет в пикере" in result.reason


def test_warning_present_but_selectable_resume_continues() -> None:
    # Ревью PR #1215 (P1): warning-узел бывает в DOM и при применимой вакансии
    # (зеркало apply/steps.py) — сам по себе он НЕ терминален. Пока выбор
    # резюме удаётся, flow продолжается до submit, warning даже не читается.
    channel = FakeFormChannel(hidden_warning=True)
    result = _run(channel, verify=_verify_of("found"))

    assert result.success
    assert channel.submitted  # flow дошёл до отправки, warning не помешал
    assert all(
        "hidden-resume-warning" not in payload["selector"]
        for action, payload in channel.calls
        if action == ACTION_CHECK
    )


def test_warning_present_without_picker_is_skip() -> None:
    # Вторая точка провала выбора: триггера пикера нет вовсе, warning есть —
    # тот же человеческий skip видимости.
    channel = FakeFormChannel(picker_present=False, hidden_warning=True)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.skipped, result.acted, result.uncertain) == (True, False, False)
    assert result.skip_reason == SKIP_REASONS.RESUME_VISIBILITY


def test_visibility_modal_dismissed_flow_reaches_submit() -> None:
    # Боевой случай #1214/#1218: модалка «поменяйте видимость» поверх формы,
    # опция резюме недоступна. safe-overlay закрывается close-контролом —
    # warning исчезает, штатный флоу доходит до submit (пикер выбирает
    # публичное резюме). Переключатель видимости не кликается никем:
    # в calls только известные клики флоу.
    channel = FakeFormChannel(
        hidden_warning=True,
        option_present=False,
        overlays=[dict(VISIBILITY_OVERLAY)],
    )
    result = _run(channel, verify=_verify_of("found"))

    assert (result.success, result.acted, result.skipped) == (True, True, False)
    assert result.skip_reason == ""
    assert channel.dismissed_ids == ["overlay-1"]
    assert channel.submitted and channel.filled_text == "Здравствуйте!"
    clicks = [payload for action, payload in channel.calls if action == ACTION_CLICK]
    # Кнопка отклика → триггер(открыть) → опция → триггер(закрыть) → submit.
    assert len(clicks) == 5
    assert "vacancy-response-link-top" in clicks[0]["selector"]
    assert "vacancy-response-submit" in clicks[4]["selector"]


def test_visibility_modal_dismiss_returns_picker_trigger() -> None:
    # Вторая точка провала (#1218): триггера пикера нет, пока открыта модалка
    # видимости; dismiss возвращает форму — флоу продолжается до submit.
    channel = FakeFormChannel(
        picker_present=False,
        hidden_warning=True,
        overlays=[dict(VISIBILITY_OVERLAY)],
    )
    result = _run(channel, verify=_verify_of("found"))

    assert result.success and channel.submitted
    assert channel.dismissed_ids == ["overlay-1"]


def test_visibility_modal_not_listed_is_skip_like_1216() -> None:
    # Overlay с текстом модалки не перечислен: dismiss не выполняется —
    # честный skip #1216 без изменений.
    channel = FakeFormChannel(
        hidden_warning=True,
        option_present=False,
        overlays=[],
    )
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.skipped, result.acted, result.uncertain) == (True, False, False)
    assert result.skip_reason == SKIP_REASONS.RESUME_VISIBILITY
    assert channel.dismissed_ids == []
    assert channel.filled_text is None and not channel.submitted


@pytest.mark.parametrize(
    "overlay_patch",
    [{"disposition": "ambiguous"}, {"closeControls": 0}],
    ids=["unsafe", "no-close-control"],
)
def test_visibility_overlay_unsafe_or_controlless_is_never_dismissed(
    overlay_patch: dict,
) -> None:
    # inner-узел модалки — ambiguous (боевой census 2026-09-23), узел без
    # close-контролов закрывать нечем: dismiss'у не подлежат. Сценарий даже
    # не пытается (жёсткий гейт у исполнителя, но попыток быть не должно
    # вовсе) — skip #1216.
    channel = FakeFormChannel(
        hidden_warning=True,
        option_present=False,
        overlays=[{**VISIBILITY_OVERLAY, **overlay_patch}],
    )
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.skipped, result.acted) == (True, False)
    assert result.skip_reason == SKIP_REASONS.RESUME_VISIBILITY
    assert channel.dismissed_ids == []


@pytest.mark.parametrize("forwarded", [False, True], ids=["refused", "response-lost"])
def test_visibility_dismiss_failure_is_skip_not_fail(forwarded: bool) -> None:
    # Отказ dismiss'а (executor/канал) — НЕ ошибка сценария: после ЛЮБОЙ
    # сделанной попытки warning перечитывается; здесь моделируется «эффекта
    # не было» — warning остался, решает прежний skip #1216, флаги чистые.
    channel = FakeFormChannel(
        hidden_warning=True,
        option_present=False,
        overlays=[dict(VISIBILITY_OVERLAY)],
        dismiss_error=PrimitiveError("overlay_not_found", "", forwarded=forwarded),
    )
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.skipped, result.acted, result.uncertain) == (True, False, False)
    assert result.skip_reason == SKIP_REASONS.RESUME_VISIBILITY
    assert not channel.submitted


def test_visibility_dismiss_response_lost_but_modal_gone_continues() -> None:
    # Ревью #1219 (#176-семантика): response_lost — ответ dismiss'а потерян,
    # но close-клик мог уйти и модалка закрылась. Перечитка warning'а после
    # ЛЮБОЙ сделанной попытки dismiss это ловит: флоу продолжается до submit,
    # vacancy не уходит в лишний skip с «поменяйте видимость вручную».
    channel = FakeFormChannel(
        hidden_warning=True,
        option_present=False,
        overlays=[dict(VISIBILITY_OVERLAY)],
        dismiss_error=PrimitiveError("response_lost", "", forwarded=True),
        dismiss_lost_effect=True,
    )
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.acted, result.skipped) == (True, True, False)
    assert result.skip_reason == ""
    assert channel.dismissed_ids == ["overlay-1"]
    assert channel.submitted and channel.filled_text == "Здравствуйте!"


def test_policy_refusal_inside_visibility_modal_is_skip() -> None:
    # Бой 2026-09-23 (#1214): policy_refused на шаге ВНУТРИ модалки видимости
    # приходит раньше чекпоинтов пикера — warning на форме переводит отказ
    # в честный skip (переключатель видимости — мутация, не кликается).
    channel = FakeFormChannel(
        hidden_warning=True,
        fill_error=PrimitiveError(
            "policy_refused", "ambiguous; overlay=modal/None", forwarded=False
        ),
    )
    result = _run(channel, require_resume_select=False, verify=_verify_of("not_found"))

    assert (result.skipped, result.acted, result.uncertain) == (True, False, False)
    assert result.skip_reason == SKIP_REASONS.RESUME_VISIBILITY
    assert "видимость" in result.reason and "вручную" in result.reason
    assert channel.filled_text is None and not channel.submitted


def test_forwarded_interruption_keeps_grey_zone_despite_warning() -> None:
    # Ревью PR #1216: forwarded-исход (включая response_lost submit-клика)
    # не превращается в acted=False skip даже при warning на форме — отклик
    # мог дойти до hh.ru (#176/#207), решает внешний источник серой зоны.
    # Warning при этом не читается вовсе.
    channel = FakeFormChannel(
        hidden_warning=True,
        fill_error=PrimitiveError("response_lost", "ответ потерян", forwarded=True),
    )
    result = _run(channel, require_resume_select=False, verify=_verify_of("not_found"))

    assert (result.success, result.skipped, result.acted, result.uncertain) == (
        False,
        False,
        True,
        False,
    )
    assert result.skip_reason == ""
    assert "внешняя проверка" in result.reason
    assert all(
        "hidden-resume-warning" not in payload["selector"]
        for action, payload in channel.calls
        if action == ACTION_CHECK
    )


@pytest.mark.parametrize(
    "probe_error",
    [
        PrimitiveError("timeout", "нет ответа", forwarded=True),
        PrimitiveError("policy_refused", "ambiguous", forwarded=False),
    ],
    ids=["forwarded-timeout", "refused-read"],
)
def test_warning_probe_failure_degrades_to_site_verdict(probe_error: PrimitiveError) -> None:
    # Ревью PR #1216: probe warning'а в обработчике отказов best-effort —
    # отказ чтения не выходит из apply_via_live исключением (исключение из
    # except-блока соседними ветками не ловится), решает прежний вердикт.
    channel = FakeFormChannel(
        hidden_warning=True,
        fill_error=PrimitiveError("policy_refused", "ambiguous", forwarded=False),
        warning_check_error=probe_error,
    )
    result = _run(channel, require_resume_select=False, verify=_verify_of("not_found"))

    assert (result.success, result.skipped, result.acted, result.uncertain) == (
        False,
        False,
        False,
        False,
    )
    assert "шаг формы не выполнен" in result.reason
    assert not channel.submitted


def test_warning_before_form_open_gets_honest_skip_not_crash() -> None:
    # Ревью PR #1216 round 2: warning-узел бывает в DOM и ДО открытия формы
    # (#1215 — свёрнутый узел при применимой вакансии). Не-forwarded отказ
    # клика кнопки отклика при таком узле не должен поднимать NameError из
    # ещё не исполненного def dismiss-хелпера — probe best-effort находит
    # warning и возвращает честный skip, флаги чистые.
    channel = FakeFormChannel(
        hidden_warning=True,
        warning_needs_form=False,
        apply_click_error=PrimitiveError("policy_refused", "dangerous", forwarded=False),
    )
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.skipped, result.acted, result.uncertain) == (
        False,
        True,
        False,
        False,
    )
    assert result.skip_reason == SKIP_REASONS.RESUME_VISIBILITY
    assert channel.dismissed_ids == []


def test_refusal_attaches_overlay_census() -> None:
    # Бой 2026-09-24 (#1220): policy_refused называет только ВЕРХНИЙ overlay
    # клика, соседние остаются неназванными, а состояние модалки «пассивно
    # невоспроизводимо». Обработчик отказов достраивает census list_overlays
    # (id/type/disposition/closeControls/текст) — следующий бой сам назовёт
    # каждый overlay, без отдельного census-скрипта.
    channel = FakeFormChannel(
        fill_error=PrimitiveError("policy_refused", "ambiguous", forwarded=False),
        overlays=[
            {
                "id": "overlay-3",
                "type": "modal",
                "disposition": "ambiguous",
                "closeControls": 0,
                "text": "Тестировщик 180 000 ₽",
            },
            {
                "id": "overlay-4",
                "type": "notification",
                "disposition": "safe",
                "closeControls": 1,
                "text": "Рекомендуете ли вы своего работодателя?",
            },
        ],
    )
    result = _run(channel, require_resume_select=False, verify=_verify_of("not_found"))

    assert (
        "оверлеи: overlay-3 modal/ambiguous close=0: Тестировщик 180 000 ₽ | "
        "overlay-4 notification/safe close=1: Рекомендуете ли вы своего работодателя?"
    ) in result.reason
    assert (result.acted, result.uncertain) == (False, False)


def test_refusal_census_without_overlays_and_dead_channel() -> None:
    # Пустой реестр и мёртвый канал — суффикс деградирует, вердикт прежний
    # (best-effort, ревью PR #1216: отказ probe не выходит из обработчика).
    channel = FakeFormChannel(
        fill_error=PrimitiveError("policy_refused", "ambiguous", forwarded=False)
    )
    result = _run(channel, require_resume_select=False, verify=_verify_of("not_found"))
    assert "; оверлеи: нет;" in result.reason

    dead = FakeFormChannel(
        fill_error=PrimitiveError("policy_refused", "ambiguous", forwarded=False),
        overlays_error=PrimitiveError("client_disconnected", "обрыв", forwarded=False),
    )
    result = _run(dead, require_resume_select=False, verify=_verify_of("not_found"))
    assert "шаг формы не выполнен" in result.reason
    assert "оверлеи" not in result.reason
    assert (result.acted, result.uncertain) == (False, False)


def test_policy_detail_prints_overlay_text() -> None:
    # #1214 DX: текст оверлея уже приходит в policy-payload (classify кладёт
    # text ≤500), но печатался только type/disposition — оператор не видел
    # ЧТО его блокирует.
    from hhru_bot.live.scenarios import _policy_detail

    detail = _policy_detail(
        {
            "reason": "ambiguous",
            "overlay": {
                "type": "modal",
                "disposition": None,
                "text": "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на «Видно всем работодателям»",
            },
        }
    )
    assert "ambiguous" in detail and "overlay=modal/None" in detail
    assert "поменяйте видимость резюме" in detail


def test_picker_option_missing_blocks_submit() -> None:
    channel = FakeFormChannel(option_present=False)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.uncertain) == (False, False)
    assert "нет в пикере" in result.reason
    assert not channel.submitted


def test_panel_must_close_before_submit() -> None:
    # Панель перекрывает submit физически: не закрылась — отправка запрещена.
    channel = FakeFormChannel(panel_closes=False)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.acted) == (False, False)
    assert "не закрылась" in result.reason
    assert not channel.submitted


def test_picker_flow_clicks_option_and_closes_panel() -> None:
    channel = FakeFormChannel()
    result = _run(channel, verify=_verify_of("found"))

    assert result.success
    clicks = [payload for action, payload in channel.calls if action == ACTION_CLICK]
    # Кнопка отклика → триггер(открыть) → опция → триггер(закрыть) → submit.
    assert "vacancy-response-link-top" in clicks[0]["selector"]
    assert "resume-title" in clicks[1]["selector"]
    assert f"magritte-select-option-{RESUME_ID}" in clicks[2]["selector"]
    assert "resume-title" in clicks[3]["selector"]
    close_wait = clicks[3]["waitFor"]
    assert close_wait["state"] == "hidden" and "drop-base" in close_wait["selector"]


def test_sole_resume_account_skips_picker() -> None:
    channel = FakeFormChannel(picker_present=False)
    result = _run(channel, require_resume_select=False)
    assert result.success
    assert not any(
        "resume-title" in payload["selector"] for a, payload in channel.calls if a == ACTION_CHECK
    )


def test_letter_toggle_expanded_when_textarea_absent() -> None:
    channel = FakeFormChannel(textarea_after_click=False)
    result = _run(channel, require_resume_select=False)

    assert (result.success, channel.filled_text) == (True, "Здравствуйте!")
    assert channel.toggle_clicked


def test_no_textarea_anywhere_is_fail_closed_before_submit() -> None:
    # Ни textarea, ни тоггла: отклик без письма не отправляем — fail-closed
    # ДО submit, финализирует внешний источник.
    channel = FakeFormChannel(textarea_after_click=False, toggle_present=False)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.uncertain) == (False, False)
    assert "fail-closed" in result.reason
    assert not channel.submitted and channel.filled_text is None


def test_fill_read_back_mismatch_blocks_submit() -> None:
    channel = FakeFormChannel(fill_ok=False)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.uncertain) == (False, False)
    assert "read-back" in result.reason
    assert not channel.submitted


def test_fill_refusal_keeps_flags_clean() -> None:
    # Не-forwarded отказ записи (оба textarea в DOM → ambiguous): hh.ru ничего
    # не получил — флаги чистые, решает вердикт сайта, а не acted+uncertain.
    channel = FakeFormChannel(
        fill_error=PrimitiveError("ambiguous_target", "2 совпадения", forwarded=False)
    )
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.acted, result.uncertain) == (False, False, False)
    assert not channel.submitted and channel.filled_text is None


def test_unconfirmed_resume_selection_blocks_submit() -> None:
    # Клик по опции потерялся (окно гидрации #858): aria-selected не появится —
    # submit запрещён, решает внешний источник (иначе hh.ru приложил бы
    # дефолтное резюме, 11/11 боевых фактов #1144).
    channel = FakeFormChannel(option_click_lost=True)
    result = _run(channel, verify=_verify_of("not_found"))

    assert (result.success, result.acted) == (False, False)
    assert "не подтверждён" in result.reason
    assert not channel.submitted


def test_selection_confirmed_by_aria_selected_read() -> None:
    # Факт выбора читается атрибутным селектором до submit — клик по опции и
    # скрытие панели выбор не доказывают.
    channel = FakeFormChannel()
    result = _run(channel, verify=_verify_of("found"))

    assert result.success
    checks = [
        str(payload.get("selector", ""))
        for action, payload in channel.calls
        if action == ACTION_CHECK
    ]
    # Факт выбора читается атрибутным матчингом aria-selected на опции.
    assert f"[data-qa='magritte-select-option-{RESUME_ID}'][aria-selected='true']" in checks


def test_submit_channel_death_is_uncertain_acting() -> None:
    # Отказ канала при submit (#176): команда ушла в браузер — acted+uncertain;
    # внешний not_found снимает неопределённость, но acted остаётся.
    channel = FakeFormChannel(
        submit_click_error=PrimitiveError("timeout", "нет ответа", forwarded=True)
    )
    result = _run(channel, verify=_verify_of("not_found"))
    assert (result.success, result.acted, result.uncertain) == (False, True, False)

    channel = FakeFormChannel(
        submit_click_error=PrimitiveError("timeout", "нет ответа", forwarded=True)
    )
    result = _run(channel, verify=None)
    assert (result.acted, result.uncertain) == (True, True)
    assert "исход неопределён" in result.reason


def test_apply_click_channel_death_goes_to_grey_zone() -> None:
    channel = FakeFormChannel(
        apply_click_error=PrimitiveError("client_disconnected", "обрыв", forwarded=True)
    )
    result = _run(channel, verify=_verify_of("found"))

    assert (result.success, result.acted) == (True, True)


def test_apply_click_policy_refusal_is_plain_fail_without_verify() -> None:
    # Отказ исполнителя ДО клика: отклик физически невозможен — обычный fail,
    # внешний источник не нужен.
    calls: list[str] = []

    def verify(vacancy_id: str, resume_id: str):
        calls.append(vacancy_id)
        return Verdict("found")

    channel = FakeFormChannel(
        apply_click_error=PrimitiveError("policy_refused", "dangerous", forwarded=False)
    )
    result = _run(channel, verify=verify)

    assert (result.success, result.acted, result.uncertain) == (False, False, False)
    assert "не выполнен" in result.reason
    assert calls == []


def test_grey_zone_verifier_crash_is_uncertain() -> None:
    def verify(vacancy_id: str, resume_id: str):
        raise RuntimeError("negotiations не прочитаны")

    channel = FakeFormChannel(modal_after_click=False, page_form=False)
    result = _run(channel, verify=verify, require_resume_select=False)

    assert (result.success, result.acted, result.uncertain) == (False, True, True)
    assert "внешняя проверка упала" in result.reason


def test_result_is_progress_compatible() -> None:
    # ApplyProgress.finish() классифицирует по структурным флагам.
    from hhru_bot.commands.supervision import ApplyProgress

    cases = [
        (ApplyLiveResult(RESUME_ID, VACANCY_ID, True, "ok", acted=True), "success"),
        (
            ApplyLiveResult(RESUME_ID, VACANCY_ID, False, "x", acted=True, uncertain=True),
            "uncertain",
        ),
        (
            ApplyLiveResult(
                RESUME_ID,
                VACANCY_ID,
                False,
                "x",
                skipped=True,
                skip_reason=SKIP_REASONS.HAS_QUESTIONS,
            ),
            "skipped",
        ),
    ]
    progress = ApplyProgress()
    for result, expected in cases:
        progress.begin_attempt()
        assert progress.finish(result) == expected
    assert (progress.applied_count, progress.uncertain_count, progress.skipped_count) == (1, 1, 1)
