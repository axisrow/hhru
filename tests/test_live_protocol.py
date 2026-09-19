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
    def test_allowlist_mirrors_stage1_extension_exactly(self):
        # Транспорт не расширяет allowlist молча: ровно три действия
        # extensions/hhru-live/content.js (ACTION_ALLOWLIST) этапа 1.
        assert set(ALLOWED_ACTIONS) == {
            "list_overlays",
            "dismiss_overlay",
            "check_element",
        }

    def test_list_overlays_rejects_any_payload(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope(_envelope(payload={"selector": "div"}))
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

    def test_default_port_is_ephemeral(self):
        from hhru_bot.cli import build_parser

        assert build_parser().parse_args(["live-serve"]).port == 0
