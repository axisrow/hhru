"""Маппинг сценария bump-live на примитивы S2 (#1161).

Сценарий гоняется с FakeChannel — порядок и аргументы вызовов обязаны
повторять боевой путь bump.py (порядок = контракт ревью cooldown/лимитов).
FakeChannel — stateful-модель страницы: click() мутирует флаги (hh.ru снимает
кнопку и показывает кулдаун-хинт), wait() читает их, как реальный DOM.
Транспорта #1159 здесь нет и не нужно: LiveChannel покрывается его
собственными тестами после мержа S1.
"""

from __future__ import annotations

import pytest

from hhru_bot.bump import BUMP_HINT_TIMEOUT_MS, BUMP_TIMEOUT_MS
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.live.scenarios import (
    ACTION_CHECK,
    ACTION_CLICK,
    ACTION_GET_STATE,
    ACTION_WAIT,
    FORWARD_UNKNOWN_CODES,
    MARKER_GONE_TIMEOUT_MS,
    MARKER_TIMEOUT_MS,
    WAIT_STATE_HIDDEN,
    WAIT_STATE_VISIBLE,
    ChannelError,
    PrimitiveError,
    bump_via_live,
)

pytestmark = pytest.mark.unit

LIST_URL = "https://hh.ru/applicant/resumes"


class FakeChannel:
    """Stateful-модель списка /applicant/resumes глазами примитивов S2.

    Флаги — отрисованное состояние страницы; click() применяет post_click
    (реальное поведение hh.ru после поднятия: хинт вместо кнопки). click_error
    имитирует отказ канала/исполнителя ДО мутации страницы.
    """

    def __init__(
        self,
        *,
        url: str = LIST_URL,
        login_found: bool = False,
        list_present: bool = True,
        card_present: bool = True,
        hint_present: bool = False,
        button_present: bool = True,
        post_click: tuple[bool, bool] = (True, False),
        click_error: PrimitiveError | None = None,
        wait_error: PrimitiveError | None = None,
        get_state_error: Exception | None = None,
    ) -> None:
        self.url = url
        self.login_found = login_found
        self.list_present = list_present
        self.card_present = card_present
        self.hint_present = hint_present
        self.button_present = button_present
        self.post_click = post_click
        self.click_error = click_error
        self.wait_error = wait_error
        self.get_state_error = get_state_error
        self._clicked = False
        self.calls: list[tuple[str, dict]] = []

    def get_state(self) -> dict:
        self.calls.append((ACTION_GET_STATE, {}))
        if self.get_state_error is not None:
            raise self.get_state_error
        return {"url": self.url}

    def check(self, selector: str) -> dict:
        self.calls.append((ACTION_CHECK, {"selector": selector}))
        return {"found": self.login_found, "visible": self.login_found}

    def click(self, selector: str, wait_for: dict | None = None, allow_apply: bool = False) -> dict:
        self.calls.append(
            (ACTION_CLICK, {"selector": selector, "waitFor": wait_for, "allowApply": allow_apply})
        )
        if self.click_error is not None:
            raise self.click_error
        self._clicked = True
        self.hint_present, self.button_present = self.post_click
        return {"clicked": True, "wait": {"met": bool(wait_for)}}

    def wait(self, selector: str, state: str, timeout_ms: int) -> bool:
        self.calls.append(
            (ACTION_WAIT, {"selector": selector, "state": state, "timeoutMs": timeout_ms})
        )
        if self.wait_error is not None and self._clicked:
            raise self.wait_error
        present = self._present(selector)
        return present if state == WAIT_STATE_VISIBLE else not present

    def _present(self, selector: str) -> bool:
        if "resume-update-button-disabled" in selector:
            return self.hint_present
        if "resume-update-button" in selector:
            return self.button_present
        if selector.startswith("[data-qa^='resume-card-link-']"):
            return self.list_present
        if "resume-card-link-" in selector:
            return self.card_present
        return True


def _resume() -> ResumeConfig:
    return ResumeConfig(
        id="resume", resume_url="https://hh.ru/resume/abc123", search=SearchFilters(text="python")
    )


def _actions(channel: FakeChannel) -> list[str]:
    return [action for action, _payload in channel.calls]


def test_action_names_match_s2_contract() -> None:
    # Страж единой точки маппинга: при расхождении имён с реальным S2 правится
    # и таблица в scenarios.py, и этот тест (одним коммитом на перебазировке).
    # get_page_state — имя действия этапа 1 (#930) в ACTION_ALLOWLIST расширения.
    assert (ACTION_GET_STATE, ACTION_CHECK, ACTION_CLICK, ACTION_WAIT) == (
        "get_page_state",
        "check_element",
        "click_element",
        "wait_element",
    )
    assert (WAIT_STATE_VISIBLE, WAIT_STATE_HIDDEN) == ("visible", "hidden")
    assert FORWARD_UNKNOWN_CODES == frozenset(
        {"timeout", "client_disconnected", "bad_response", "unexpected_message", "response_lost"}
    )


def test_success_path_maps_to_primitives_in_battle_order() -> None:
    channel = FakeChannel()  # дефолты: страница готова, клик даёт кулдаун-хинт
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.reason, result.acted, result.uncertain) == (
        True,
        "success",
        True,
        False,
    )
    # Порядок боевого bump.py: URL -> форма входа -> список -> карточка ->
    # hint -> кнопка -> клик -> позитивный маркер -> добивочное чтение кнопки
    # (#1184). Селекторы кнопки/hint скоупятся карточкой резюме
    # (мульти-резюме аккаунта).
    waits = [payload for action, payload in channel.calls if action == ACTION_WAIT]
    assert _actions(channel) == [
        ACTION_GET_STATE,
        ACTION_CHECK,
        ACTION_WAIT,
        ACTION_WAIT,
        ACTION_WAIT,
        ACTION_WAIT,
        ACTION_CLICK,
        ACTION_WAIT,
        ACTION_WAIT,
    ]
    assert waits[-2]["timeoutMs"] == MARKER_TIMEOUT_MS
    assert waits[-1]["timeoutMs"] == MARKER_GONE_TIMEOUT_MS
    assert waits[-1]["state"] == WAIT_STATE_HIDDEN
    click_call = next(payload for action, payload in channel.calls if action == ACTION_CLICK)
    assert "resume-update-button" in click_call["selector"]
    assert "resume-card-link-abc123" in click_call["selector"]


def test_dry_run_stops_before_click() -> None:
    channel = FakeChannel()
    result = bump_via_live(channel, _resume(), dry_run=True)

    assert (result.success, result.reason, result.acted) == (True, "dry-run", False)
    assert ACTION_CLICK not in _actions(channel)


def test_wrong_url_fails_before_any_mutation() -> None:
    channel = FakeChannel(url="https://hh.ru/search/vacancy")
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, False, False)
    assert "не на списке резюме" in result.reason
    assert _actions(channel) == [ACTION_GET_STATE]


def test_login_form_fails() -> None:
    channel = FakeChannel(login_found=True)
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted) == (False, False)
    assert "Сессия недействительна" in result.reason


def test_disabled_hint_means_too_early_without_click() -> None:
    channel = FakeChannel(hint_present=True)
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, False, False)
    assert result.reason == "hh.ru сообщает, что поднимать ещё рано"
    assert ACTION_CLICK not in _actions(channel)


def test_unrendered_list_and_missing_card_are_distinguished() -> None:
    # Список не отрисовался (гидрация) — не «удалено».
    channel = FakeChannel(list_present=False)
    result = bump_via_live(channel, _resume(), dry_run=False)
    assert (result.success, result.acted) == (False, False)
    assert "не отрисовался" in result.reason

    # Список отрисовался, карточки нет — «удалено или недоступно».
    channel = FakeChannel(card_present=False)
    result = bump_via_live(channel, _resume(), dry_run=False)
    assert (result.success, result.acted) == (False, False)
    assert "не найдено в списке" in result.reason


def test_missing_button_fails() -> None:
    channel = FakeChannel(button_present=False)
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted) == (False, False)
    assert result.reason == "кнопка поднятия резюме не найдена на странице"


def test_policy_refused_click_is_failed_not_uncertain() -> None:
    # Отказ исполнителя до клика — мутации не было, повтор возможен.
    channel = FakeChannel(
        click_error=PrimitiveError("action_not_allowed", "policy: dangerous", forwarded=False)
    )
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, False, False)
    assert "клик не выполнен" in result.reason


def test_click_timeout_is_uncertain_acting() -> None:
    # Команда ушла в браузер, ответа нет — fail-closed #176: acted+uncertain.
    channel = FakeChannel(click_error=PrimitiveError("timeout", "нет ответа", forwarded=True))
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, True, True)
    assert "исход неопределён" in result.reason


def test_cooldown_hint_racing_after_precheck_is_not_success() -> None:
    # Гонка #1184: хинт смонтировался в окне между pre-check и кликом
    # (кулдаун наступил ровно сейчас) — hh.ru оставляет кнопку disabled,
    # клик ничего не поднимает, а пост-клик условие «хинт виден» метится
    # мгновенно. post_click=(True, True) — ровно эта страница: хинт есть,
    # кнопка НЕ снята. Ложный success запрещён: acted+uncertain (fail-closed).
    channel = FakeChannel(post_click=(True, True))
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, True, True)
    assert "кнопка поднятия не снята" in result.reason
    waits = [payload for action, payload in channel.calls if action == ACTION_WAIT]
    assert waits[-1]["state"] == WAIT_STATE_HIDDEN


def test_marker_absent_button_gone_is_success() -> None:
    # Хинт не появился, но кнопка исчезла — hh.ru зарегистрировал поднятие.
    channel = FakeChannel(post_click=(False, False))
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (True, True, False)
    assert "кнопка поднятия исчезла" in result.reason


def test_marker_absent_button_still_there_is_uncertain() -> None:
    # Кнопка на месте и хинта нет — выдуманный успех запрещён (#1161).
    channel = FakeChannel(post_click=(False, True))
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, True, True)
    assert "не подтвердились" in result.reason


def test_post_click_transport_failure_is_uncertain() -> None:
    # Обрыв после клика: wait отказывает — подтверждение не прочитано,
    # исход обязан быть uncertain независимо от факта клика.
    channel = FakeChannel(
        wait_error=PrimitiveError("client_disconnected", "соединение потеряно", forwarded=True)
    )
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, True, True)
    assert "подтверждение не прочитано" in result.reason


def test_transport_error_before_click_is_plain_failure() -> None:
    channel = FakeChannel(
        get_state_error=PrimitiveError("no_client", "нет клиента", forwarded=False)
    )
    result = bump_via_live(channel, _resume(), dry_run=False)

    assert (result.success, result.acted, result.uncertain) == (False, False, False)
    assert "канал/исполнитель" in result.reason

    channel = FakeChannel(get_state_error=ChannelError("сервер остановился"))
    result = bump_via_live(channel, _resume(), dry_run=False)
    assert (result.success, result.acted) == (False, False)


def test_placeholder_resume_fails_without_channel_calls() -> None:
    resume = ResumeConfig(
        id="resume", resume_url="https://hh.ru/resume/XXXXXXXX", search=SearchFilters(text="x")
    )
    channel = FakeChannel()
    result = bump_via_live(channel, resume, dry_run=False)

    assert (result.success, result.acted) == (False, False)
    assert channel.calls == []
    assert "плейсхолдер" in result.reason


def test_marker_timeouts_documented_budgets() -> None:
    # Пост-клик бюджет шире боевого ожидания карточки: сеть + гидрация.
    assert MARKER_TIMEOUT_MS > BUMP_TIMEOUT_MS
    assert 0 < MARKER_GONE_TIMEOUT_MS <= BUMP_HINT_TIMEOUT_MS * 10


def test_default_port_matches_extension_bridge() -> None:
    # Расширение подключается только к ws://127.0.0.1:8765 (LIVE_SERVE_URL,
    # background.js): дефолт ephemeral 0 дал бы канал, которого клиент никогда
    # не увидит, и каждый запуск сгорал бы в 120-секундном wait_client.
    from hhru_bot.commands.bump_live import DEFAULT_LIVE_PORT
    from hhru_bot.commands.live_serve import LIVE_SERVE_DEFAULT_PORT

    assert DEFAULT_LIVE_PORT == LIVE_SERVE_DEFAULT_PORT == 8765
