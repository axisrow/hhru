"""Протокол live-канала: envelope/ответ/allowlist, чистая логика (#1159)."""

from __future__ import annotations

import json

import pytest

from hhru_bot.live import (
    ALLOWED_ACTIONS,
    PROTOCOL_VERSION,
    Command,
    error_response,
    ok_response,
    parse_envelope,
    parse_response,
    serialize,
)
from hhru_bot.live.protocol import (
    BAD_ENVELOPE,
    BAD_PAYLOAD,
    BAD_RESPONSE,
    UNKNOWN_ACTION,
    UNSUPPORTED_VERSION,
    ProtocolError,
)

pytestmark = pytest.mark.unit


def _envelope(**overrides: object) -> str:
    obj: dict = {"v": PROTOCOL_VERSION, "id": "c1", "action": "list_overlays", "payload": {}}
    obj.update(overrides)
    return json.dumps(obj)


class TestEnvelope:
    def test_valid_envelope_parses(self):
        cmd = parse_envelope(_envelope(action="dismiss_overlay", payload={"id": "overlay-3"}))
        assert cmd == Command(id="c1", action="dismiss_overlay", payload={"id": "overlay-3"})

    def test_numeric_id_is_allowed(self):
        assert parse_envelope(_envelope(id=7)).id == 7

    def test_not_json_is_bad_envelope(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope("{not json")
        assert exc.value.code == BAD_ENVELOPE
        assert exc.value.command_id is None

    def test_non_object_json_is_bad_envelope(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope("[1,2]")
        assert exc.value.code == BAD_ENVELOPE

    def test_missing_id_is_bad_envelope(self):
        obj = json.loads(_envelope())
        del obj["id"]
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(json.dumps(obj))
        assert exc.value.code == BAD_ENVELOPE

    @pytest.mark.parametrize("bad_id", ["", True, None, ["c1"]])
    def test_invalid_id_values(self, bad_id):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(id=bad_id))
        assert exc.value.code == BAD_ENVELOPE

    def test_missing_version_is_bad_envelope(self):
        obj = json.loads(_envelope())
        del obj["v"]
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(json.dumps(obj))
        assert exc.value.code == BAD_ENVELOPE

    def test_unknown_version_is_explicit_error(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(v=99))
        assert exc.value.code == UNSUPPORTED_VERSION
        # id прочитан — отказ адресован конкретной команде, не тишине.
        assert exc.value.command_id == "c1"

    @pytest.mark.parametrize("bad_v", [True, 1.0, "1"])
    def test_version_must_be_exact_int(self, bad_v):
        # bool исключён явно (True == 1 в Python), float и строка — по типу.
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(v=bad_v))
        assert exc.value.code == UNSUPPORTED_VERSION

    def test_unknown_action_is_explicit_error(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(action="click_apply_button"))
        assert exc.value.code == UNKNOWN_ACTION

    def test_non_dict_payload_is_bad_payload(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(payload=["x"]))
        assert exc.value.code == BAD_PAYLOAD
        assert exc.value.command_id == "c1"


class TestAllowlist:
    def test_allowlist_mirrors_extension_exactly(self):
        # Транспорт не расширяет allowlist молча: ровно те действия, что в
        # extensions/hhru-live/content.js (ACTION_ALLOWLIST) — три этапа 1
        # (#930) + три примитива исполнителя #1160 + fill_element (#1162).
        assert set(ALLOWED_ACTIONS) == {
            "list_overlays",
            "dismiss_overlay",
            "check_element",
            "get_page_state",
            "wait_element",
            "click_element",
            "fill_element",
        }

    def test_get_page_state_takes_no_payload(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(action="get_page_state", payload={"x": 1}))
        assert exc.value.code == BAD_PAYLOAD

    def test_wait_element_requires_single_target_state_timeout(self):
        base = {"state": "visible", "timeoutMs": 1000}
        with pytest.raises(ProtocolError):
            parse_envelope(_envelope(action="wait_element", payload=base))
        with pytest.raises(ProtocolError):
            parse_envelope(
                _envelope(
                    action="wait_element",
                    payload={**base, "selector": "a", "dataQa": "b"},
                )
            )
        ok = parse_envelope(_envelope(action="wait_element", payload={**base, "selector": "a"}))
        assert ok.payload["timeoutMs"] == 1000

    @pytest.mark.parametrize("bad_timeout", [0, -1, True, 1.5, "1000", None])
    def test_wait_element_rejects_bad_timeout(self, bad_timeout):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(
                _envelope(
                    action="wait_element",
                    payload={"selector": "a", "state": "visible", "timeoutMs": bad_timeout},
                )
            )
        assert exc.value.code == BAD_PAYLOAD

    def test_wait_element_state_is_closed_vocabulary(self):
        with pytest.raises(ProtocolError):
            parse_envelope(
                _envelope(
                    action="wait_element",
                    payload={"selector": "a", "state": "attached", "timeoutMs": 1000},
                )
            )

    def test_click_element_validates_wait_for_and_allow_apply(self):
        target = {"selector": "button", "allowApply": True}
        ok = parse_envelope(
            _envelope(
                action="click_element",
                payload={
                    **target,
                    "waitFor": {"dataQa": "x", "state": "hidden", "timeoutMs": 2000},
                },
            )
        )
        assert ok.payload["allowApply"] is True
        # waitFor без таймаута / с двумя целями / allowApply не-булево — отказ.
        with pytest.raises(ProtocolError):
            parse_envelope(
                _envelope(
                    action="click_element",
                    payload={**target, "waitFor": {"dataQa": "x", "state": "hidden"}},
                )
            )
        with pytest.raises(ProtocolError):
            parse_envelope(
                _envelope(
                    action="click_element",
                    payload={
                        **target,
                        "waitFor": {
                            "dataQa": "x",
                            "selector": "y",
                            "state": "hidden",
                            "timeoutMs": 2000,
                        },
                    },
                )
            )
        with pytest.raises(ProtocolError):
            parse_envelope(
                _envelope(action="click_element", payload={**target, "allowApply": "yes"})
            )

    def test_fill_element_requires_text_and_single_target(self):
        ok = parse_envelope(
            _envelope(action="fill_element", payload={"selector": "textarea", "text": "письмо"})
        )
        assert ok.payload["text"] == "письмо"
        with pytest.raises(ProtocolError):
            parse_envelope(_envelope(action="fill_element", payload={"selector": "textarea"}))
        with pytest.raises(ProtocolError):
            parse_envelope(
                _envelope(
                    action="fill_element",
                    payload={"selector": "a", "text": "x", "state": "visible"},
                )
            )
        with pytest.raises(ProtocolError) as long_exc:
            parse_envelope(
                _envelope(
                    action="fill_element",
                    payload={"selector": "a", "text": "x" * 10_001},
                )
            )
        assert long_exc.value.code == BAD_PAYLOAD

    def test_list_overlays_rejects_any_payload(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(payload={"selector": "div"}))
        assert exc.value.code == BAD_PAYLOAD

    def test_get_page_state_rejects_any_payload(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(action="get_page_state", payload={"url": "x"}))
        assert exc.value.code == BAD_PAYLOAD

    def test_get_page_state_accepts_empty(self):
        cmd = parse_envelope(_envelope(action="get_page_state"))
        assert cmd.action == "get_page_state"

    def test_wait_element_accepts_contract_payload(self):
        # camelCase timeoutMs — имя поля сообщения исполнителя (executor.js).
        cmd = parse_envelope(
            _envelope(
                action="wait_element",
                payload={"selector": "[data-qa='x']", "state": "visible", "timeoutMs": 1500},
            )
        )
        assert cmd.payload["timeoutMs"] == 1500

    def test_wait_element_rejects_snake_case_timeout(self):
        # Фикс дрейфа #1160: сервер шлет только camelCase-контракт исполнителя.
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(
                _envelope(
                    action="wait_element",
                    payload={"selector": "x", "state": "visible", "timeout_ms": 1500},
                )
            )
        assert exc.value.code == BAD_PAYLOAD

    def test_wait_element_requires_valid_state(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(
                _envelope(
                    action="wait_element",
                    payload={"selector": "x", "state": "attached", "timeoutMs": 100},
                )
            )
        assert exc.value.code == BAD_PAYLOAD

    def test_wait_element_requires_positive_timeout(self):
        for bad in (0, -5, True, "1500", None):
            with pytest.raises(ProtocolError) as exc:
                parse_envelope(
                    _envelope(
                        action="wait_element",
                        payload={"selector": "x", "state": "visible", "timeoutMs": bad},
                    )
                )
            assert exc.value.code == BAD_PAYLOAD

    def test_wait_element_requires_exactly_one_target(self):
        for payload in (
            {"state": "visible", "timeoutMs": 100},
            {"selector": "a", "dataQa": "b", "state": "visible", "timeoutMs": 100},
            {"selector": "", "state": "visible", "timeoutMs": 100},
        ):
            with pytest.raises(ProtocolError) as exc:
                parse_envelope(_envelope(action="wait_element", payload=payload))
            assert exc.value.code == BAD_PAYLOAD

    def test_click_element_accepts_contract_payload(self):
        cmd = parse_envelope(
            _envelope(
                action="click_element",
                payload={
                    "dataQa": "resume-update-button",
                    "waitFor": {"state": "hidden", "timeoutMs": 500, "dataQa": "hint"},
                },
            )
        )
        assert cmd.payload["waitFor"]["timeoutMs"] == 500

    def test_click_element_requires_wait_for(self):
        # Исполнитель отклоняет клик без объявленного условия (wait_required).
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(action="click_element", payload={"selector": "a"}))
        assert exc.value.code == BAD_PAYLOAD

    def test_click_element_wait_for_requires_target_and_state(self):
        for wait_for in (
            {"timeoutMs": 500},
            {"state": "visible", "timeoutMs": 500},
            {"state": "nope", "timeoutMs": 500, "selector": "x"},
        ):
            with pytest.raises(ProtocolError) as exc:
                parse_envelope(
                    _envelope(
                        action="click_element",
                        payload={"selector": "a", "waitFor": wait_for},
                    )
                )
            assert exc.value.code == BAD_PAYLOAD

    def test_dismiss_overlay_requires_overlay_id(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(action="dismiss_overlay", payload={}))
        assert exc.value.code == BAD_PAYLOAD

    def test_dismiss_overlay_rejects_unknown_field(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(
                _envelope(
                    action="dismiss_overlay",
                    payload={"id": "overlay-1", "force": True},
                )
            )
        assert exc.value.code == BAD_PAYLOAD

    def test_dismiss_overlay_accepts_optional_selector(self):
        cmd = parse_envelope(
            _envelope(
                action="dismiss_overlay",
                payload={"id": "overlay-1", "selector": "div.toast"},
            )
        )
        assert cmd.payload["selector"] == "div.toast"

    def test_check_element_requires_nonempty_selector(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(action="check_element", payload={"selector": ""}))
        assert exc.value.code == BAD_PAYLOAD
        cmd = parse_envelope(
            _envelope(action="check_element", payload={"selector": "[data-qa='x']"})
        )
        assert cmd.action == "check_element"


class TestResponses:
    def test_ok_response_shape(self):
        resp = ok_response("c1", {"overlays": []})
        assert resp == {"id": "c1", "status": "ok", "result": {"overlays": []}}

    def test_error_response_shape_with_and_without_detail(self):
        assert error_response("c1", "no_client") == {
            "id": "c1",
            "status": "error",
            "result": {"code": "no_client"},
        }
        assert error_response(None, "bad_envelope", "битый json") == {
            "id": None,
            "status": "error",
            "result": {"code": "bad_envelope", "detail": "битый json"},
        }

    def test_parse_response_accepts_valid(self):
        resp = parse_response('{"id":"c1","status":"error","result":{"code":"x"}}')
        assert resp["status"] == "error"

    @pytest.mark.parametrize(
        "raw",
        [
            "не json",
            "42",
            '{"status":"ok","result":{}}',  # нет id
            '{"id":"c1","result":{}}',  # нет status
            '{"id":"c1","status":"maybe","result":{}}',
            '{"id":"c1","status":"ok"}',  # нет result
        ],
    )
    def test_parse_response_rejects_invalid_shape(self, raw):
        with pytest.raises(ProtocolError) as exc:
            parse_response(raw)
        assert exc.value.code == BAD_RESPONSE

    def test_serialize_is_single_line_ascii_safe_json(self):
        line = serialize(ok_response("c1", {"text": "отклик отправлен"}))
        assert "\n" not in line
        assert json.loads(line)["result"]["text"] == "отклик отправлен"


class TestCommandRegistration:
    def test_live_serve_registered_with_port_flag(self):
        from hhru_bot.cli import build_parser

        ns = build_parser().parse_args(["live-serve", "--port", "8765"])
        assert ns.port == 8765
        assert callable(ns.func)

    def test_default_port_matches_extension_bridge(self):
        # LIVE_SERVE_URL расширения захардкожен (background.js): дефолт
        # ephemeral 0 давал канал, который клиент никогда не увидит.
        from hhru_bot.cli import build_parser
        from hhru_bot.commands.live_serve import LIVE_SERVE_DEFAULT_PORT

        assert build_parser().parse_args(["live-serve"]).port == LIVE_SERVE_DEFAULT_PORT == 8765

    @pytest.mark.parametrize("bad_port", ["99999", "-1", "65536"])
    def test_port_out_of_range_rejected_at_argparse(self, bad_port, capsys):
        # Вне диапазона bind() бросает OverflowError (сырой traceback);
        # диапазон ловится типом аргумента, до всякого сокета.
        from hhru_bot.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["live-serve", "--port", bad_port])
        assert "0..65535" in capsys.readouterr().err
