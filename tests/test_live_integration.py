"""LiveChannel поверх реального сервера #1159 с фейковым WS-клиентом (#1164).

Интеграционный слой тестовой стратегии канала: НЕ Chrome — минимальный
RFC 6455-клиент из test_live_serve в роли расширения hhru-live. Покрывает то,
что серверные тесты (test_live_serve) и сценарные на FakeChannel
(test_bump_live) не проверяют по отдельности: маппинг «примитив LiveChannel ->
envelope -> ответ -> результат/PrimitiveError» на настоящем сокете и
stdin-пайпе, включая reconnect/heartbeat, отказ dangerous-команды и
неизвестное действие глазами клиента сценария.

Формы ответов фейка — РЕАЛЬНЫЕ формы исполнителя S2 (#1186, зафиксированы
живым прогоном и tests/live/test_live_channel_read.py): stage-1 ответы
вложены в 'page'/'element', ответы executor.js — в 'result' (двойной
уровень), ошибки расширения идут с ключом 'error', а не 'code'. Плоский
словарь здесь молча проходил бы fallback-ветки распаковки — и регрессия
вложенности стала бы невидимой для CI.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from hhru_bot.live import PROTOCOL_VERSION
from hhru_bot.live.protocol import (
    BAD_PAYLOAD,
    CLIENT_DISCONNECTED,
    NO_CLIENT,
    TIMEOUT,
    UNKNOWN_ACTION,
)
from hhru_bot.live.scenarios import (
    ACTION_CHECK,
    ACTION_GET_STATE,
    ChannelError,
    LiveChannel,
    PrimitiveError,
)
from test_live_serve import ExtensionClientStub, _assert_no_text_frame

pytestmark = pytest.mark.integration

BUTTON = "[data-qa='resume-update-button']"
HINT = "[data-qa='resume-update-button-disabled']"


@pytest.fixture()
def channel():
    ch = LiveChannel(client_timeout=5.0)
    ch.start()
    assert ch._server is not None
    # Дефолтный response_timeout 30 с годится только боевому пути; тесты с
    # таймаутом сужают его сами (атрибут читается в _call на каждый вызов).
    ch._server.response_timeout = 5.0
    yield ch
    ch.close()


def _connect(channel) -> ExtensionClientStub:
    """Подключить фейковое расширение и дождаться его в сервере."""
    client = ExtensionClientStub(channel._server.port)
    channel.wait_client()
    return client


def _responder(client: ExtensionClientStub, results: list[dict], seen: list[dict]) -> None:
    """Ответчик в роли расширения: читает команду, отвечает results по очереди.

    id эхом из полученного envelope; последний result повторяется (заглушка
    на случай лишнего чтения). Пинги WS отвечаются внутри recv_frame.
    """

    def run() -> None:
        for result in (*results, results[-1]):
            try:
                _, payload = client.recv_frame()
            except (TimeoutError, ConnectionError, OSError):
                return
            obj = json.loads(payload)
            seen.append(obj)
            client.send_text(json.dumps({"id": obj["id"], "status": "ok", "result": result}))

    threading.Thread(target=run, daemon=True).start()


def test_get_state_roundtrip_over_real_socket(channel):
    client = _connect(channel)
    seen: list[dict] = []
    # Форма РЕАЛЬНОГО ответа исполнителя (content.js get_page_state через
    # background.js: {ok:true, page:{...}} -> result={page:{...}}), #1186:
    # плоский словарь здесь молча проскочил бы мимо распаковки 'page' в
    # LiveChannel.get_state, и регрессия вложенности была бы невидима CI.
    page = {"url": "https://hh.ru/applicant/resumes", "title": "Резюме", "readyState": "complete"}
    _responder(client, [{"page": page}], seen)
    try:
        assert channel.get_state() == page
    finally:
        client.close()
    # Контракт S1/S2 в пересланном envelope: версия, имя действия исполнителя.
    assert seen[0]["v"] == PROTOCOL_VERSION
    assert seen[0]["action"] == ACTION_GET_STATE
    assert seen[0]["payload"] == {}


def test_check_element_roundtrip_unwraps_element(channel):
    # Реальная форма stage-1 (content.js: {ok:true, element:{...}}):
    # found/visible живут ВНУТРИ 'element', и гейт формы входа в сценариях
    # читает их только после распаковки LiveChannel.check (#1186).
    client = _connect(channel)
    seen: list[dict] = []
    element = {"found": True, "visible": True, "covered": False, "matchCount": 1, "text": "Вход"}
    _responder(client, [{"element": element}], seen)
    try:
        assert channel.check("[data-qa='login-form']") == element
    finally:
        client.close()
    assert seen[0]["action"] == ACTION_CHECK


def test_wait_primitive_maps_wait_met_to_bool(channel):
    client = _connect(channel)
    seen: list[dict] = []
    # Исполнитель кладёт итог в result.wait.met и оборачивает его в 'result'
    # (executor.js waitElement: {ok:true, result:{action, wait:{...}}}):
    # ДВОЙНОЙ уровень вложенности, снимаемый _executor_result + 'wait'.
    wait = {"state": "visible", "timeoutMs": 1500, "met": True, "elapsedMs": 12}
    result = {"action": "wait_element", "state": "visible", "wait": wait, "finalState": {}}
    missed = {**result, "wait": {**wait, "met": False, "elapsedMs": 1500}}
    _responder(client, [{"result": result}, {"result": missed}], seen)
    try:
        assert channel.wait(BUTTON, "visible", 1500) is True
        assert channel.wait(BUTTON, "visible", 1500) is False
    finally:
        client.close()
    # camelCase-контракт исполнителя доезжает до WS без переименований.
    assert seen[0]["payload"] == {"selector": BUTTON, "state": "visible", "timeoutMs": 1500}


def test_click_forwards_declared_wait_for(channel):
    client = _connect(channel)
    seen: list[dict] = []
    # Реальная форма ответа клика (executor.js clickElement): полезная нагрузка
    # во вложенном 'result', факт исполнения условия — в result.wait.met.
    click = {
        "action": "click_element",
        "policy": {"verdict": "allowed", "context": "page"},
        "target": {"tag": "button", "dataQa": "resume-update-button", "text": "Поднять"},
        "clicked": True,
        "wait": {"state": "visible", "timeoutMs": 15000, "met": True, "elapsedMs": 340},
        "finalState": {"url": "https://hh.ru/applicant/resumes"},
    }
    _responder(client, [{"result": click}], seen)
    try:
        result = channel.click(BUTTON, {"selector": HINT, "state": "visible", "timeoutMs": 15_000})
    finally:
        client.close()
    assert result == click
    assert seen[0]["action"] == "click_element"
    assert seen[0]["payload"]["waitFor"] == {
        "selector": HINT,
        "state": "visible",
        "timeoutMs": 15_000,
    }


def test_extension_refusal_is_not_forwarded(channel):
    # Отказ dangerous-команды политикой расширения: код клиента — данные,
    # транспорт помечает исход как «клик не прошёл» (forwarded=False).
    client = _connect(channel)

    def refuse() -> None:
        _, payload = client.recv_frame()
        obj = json.loads(payload)
        client.send_text(
            json.dumps(
                {
                    "id": obj["id"],
                    "status": "error",
                    "result": {"code": "action_not_allowed", "detail": "policy: dangerous"},
                }
            )
        )

    threading.Thread(target=refuse, daemon=True).start()
    with pytest.raises(PrimitiveError) as exc:
        channel.check(BUTTON)
    client.close()
    assert exc.value.code == "action_not_allowed"
    assert exc.value.forwarded is False


def test_extension_error_shape_error_key_is_not_lost(channel):
    # Ошибки ИСПОЛНИТЕЛЯ приходят без ключа 'code' (background.js удаляет
    # только ok: {ok:false, error:'policy_refused', policy:{...}} -> result
    # {'error': ...}); код читается из 'error', текст отказа не теряется
    # и не считается «команда ушла в браузер» (#1186).
    client = _connect(channel)

    def refuse() -> None:
        _, payload = client.recv_frame()
        obj = json.loads(payload)
        client.send_text(
            json.dumps(
                {
                    "id": obj["id"],
                    "status": "error",
                    "result": {
                        "error": "policy_refused",
                        "policy": {"verdict": "refused", "reason": "dangerous"},
                    },
                }
            )
        )

    threading.Thread(target=refuse, daemon=True).start()
    with pytest.raises(PrimitiveError) as exc:
        channel.click(BUTTON, {"selector": HINT, "state": "visible", "timeoutMs": 1000})
    client.close()
    assert exc.value.code == "policy_refused"
    assert exc.value.forwarded is False
    assert "policy_refused" in str(exc.value)


def test_unknown_action_fails_closed_before_forward(channel):
    client = _connect(channel)
    try:
        with pytest.raises(PrimitiveError) as exc:
            channel._call("self_destruct", {})
        assert exc.value.code == UNKNOWN_ACTION
        assert exc.value.forwarded is False
        # Fail-closed: исполнителю не ушло ничего (сокет ещё открыт).
        _assert_no_text_frame(client, 0.3)
    finally:
        client.close()


def test_bad_payload_rejected_without_forward(channel):
    client = _connect(channel)
    try:
        with pytest.raises(PrimitiveError) as exc:
            channel._call(ACTION_GET_STATE, {"url": "https://example.com"})
        assert exc.value.code == BAD_PAYLOAD
        assert exc.value.forwarded is False
        _assert_no_text_frame(client, 0.3)
    finally:
        client.close()


def test_client_disconnect_mid_command_is_forwarded_unknown(channel):
    # Ответа не будет: обрыв посреди команды — «действие могло выполниться».
    client = _connect(channel)

    def go_silent() -> None:
        client.recv_frame()
        client.close()

    threading.Thread(target=go_silent, daemon=True).start()
    with pytest.raises(PrimitiveError) as exc:
        channel.get_state()
    assert exc.value.code == CLIENT_DISCONNECTED
    assert exc.value.forwarded is True


def test_response_lost_is_forwarded_unknown(channel):
    # #1181: background.js доставил команду во вкладку, но ответ потерян
    # (порт закрылся — клик мог случиться, страница ушла в навигацию). Код
    # response_lost обязан читаться как «исход неизвестен», а не как отказ.
    client = _connect(channel)

    def lose() -> None:
        _, payload = client.recv_frame()
        obj = json.loads(payload)
        client.send_text(
            json.dumps({"id": obj["id"], "status": "error", "result": {"error": "response_lost"}})
        )

    threading.Thread(target=lose, daemon=True).start()
    with pytest.raises(PrimitiveError) as exc:
        channel.get_state()
    client.close()
    assert exc.value.code == "response_lost"
    assert exc.value.forwarded is True


def test_silent_client_times_out_as_forwarded_unknown(channel):
    client = _connect(channel)
    channel._server.response_timeout = 0.3  # расширение приняло команду и молчит
    try:
        with pytest.raises(PrimitiveError) as exc:
            channel.get_state()
    finally:
        client.close()
    assert exc.value.code == TIMEOUT
    assert exc.value.forwarded is True


def test_heartbeat_drops_silent_client_then_no_client(channel):
    # Патч ДО подключения: живой select() уже спит по дефолтным интервалам
    # (15с) и не проснётся без fd-события — поздний патч он не увидит.
    channel._server.heartbeat_interval = 0.1
    channel._server.heartbeat_timeout = 0.4
    client = _connect(channel)
    # Клиент не отвечает на пинги (никто не читает сокет): сервер обязан
    # объявить полуоткрытый сокет потерянным и освободить слот.
    deadline = time.monotonic() + 5
    while channel._server._client is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert channel._server._client is None, "молчащий клиент не выброшен по heartbeat"
    with pytest.raises(PrimitiveError) as exc:
        channel.get_state()
    client.close()
    assert exc.value.code == NO_CLIENT
    assert exc.value.forwarded is False


def test_reconnect_after_drop_serves_next_command(channel):
    first = _connect(channel)
    first.close()
    deadline = time.monotonic() + 5
    while channel._server._client is not None and time.monotonic() < deadline:
        time.sleep(0.05)

    client = _connect(channel)  # то же расширение переподключилось
    seen: list[dict] = []
    _responder(client, [{"page": {"url": "https://hh.ru/applicant/resumes"}}], seen)
    try:
        assert channel.get_state()["url"] == "https://hh.ru/applicant/resumes"
    finally:
        client.close()
    assert seen[0]["id"] == "c1"  # счётчик команд не привязан к соединению


def test_wait_client_times_out_without_extension():
    ch = LiveChannel(client_timeout=0.3)
    ch.start()
    try:
        with pytest.raises(ChannelError, match="не подключилось"):
            ch.wait_client()
    finally:
        ch.close()
