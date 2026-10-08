"""overlay-census: таблица, дамп и честные [FAIL] недоступного канала (#1232)."""

from __future__ import annotations

import argparse
import json

import pytest

from hhru_bot.commands import overlay_census
from hhru_bot.live.scenarios import ChannelError, PrimitiveError

pytestmark = pytest.mark.unit

OVERLAYS = [
    {
        "id": "overlay-1",
        "type": "modal",
        "disposition": "safe",
        "closeControls": 2,
        "text": "Чтобы откликнуться, поменяйте видимость резюме",
    },
    {
        "id": "overlay-2",
        "type": "notification",
        "disposition": "ambiguous",
        "closeControls": 0,
        "text": "Ваш регион — Москва? Да, верно",
    },
]


class FakeChannel:
    """LiveChannel-двойник: start/wait_client/чтения census по контракту."""

    def __init__(
        self,
        *,
        overlays: list[dict] | None = OVERLAYS,
        start_error: ChannelError | None = None,
        client_error: ChannelError | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.start_error = start_error
        self.client_error = client_error
        self.read_error = read_error
        self.overlays = overlays or []
        self.closed = False

    def start(self) -> str:
        if self.start_error is not None:
            raise self.start_error
        return "ws://127.0.0.1:8765"

    def wait_client(self) -> None:
        if self.client_error is not None:
            raise self.client_error

    def get_state(self) -> dict:
        if self.read_error is not None:
            raise self.read_error
        return {"url": "https://hh.ru/vacancy/12345678", "title": "Вакансия"}

    def list_overlays(self) -> list[dict]:
        if self.read_error is not None:
            raise self.read_error
        return list(self.overlays)

    def close(self) -> None:
        self.closed = True


def _args(**overrides) -> argparse.Namespace:
    values = {"port": 8765, "wait_seconds": 0.1}
    values.update(overrides)
    return argparse.Namespace(**values)


def _run(monkeypatch, tmp_path, channel: FakeChannel, **overrides):
    monkeypatch.setattr(overlay_census, "LiveChannel", lambda **_: channel)
    monkeypatch.setattr(overlay_census, "LOG_DIR", tmp_path)
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", captured.append)
    result = overlay_census.run(_args(**overrides))
    return result, "\n".join(captured), captured


def test_success_prints_table_and_writes_dump(monkeypatch, tmp_path) -> None:
    result, out, _ = _run(monkeypatch, tmp_path, FakeChannel())

    assert result is False
    assert "overlay-1" in out and "safe" in out
    assert "поменяйте видимость" in out
    assert "Ваш регион" in out
    dump = next(tmp_path.glob("overlay_census_*.json"))
    payload = json.loads(dump.read_text())
    assert payload["url"] == "https://hh.ru/vacancy/12345678"
    assert [o["id"] for o in payload["overlays"]] == ["overlay-1", "overlay-2"]
    assert "[OK]" in out and "дамп" in out


def test_empty_overlays_is_ok_with_dump(monkeypatch, tmp_path) -> None:
    result, out, _ = _run(monkeypatch, tmp_path, FakeChannel(overlays=[]))

    assert result is False
    assert "оверлеев нет" in out
    payload = json.loads(next(tmp_path.glob("overlay_census_*.json")).read_text())
    assert payload["overlays"] == []


def test_non_dict_entries_skipped(monkeypatch, tmp_path) -> None:
    channel = FakeChannel(overlays=["мусор", dict(OVERLAYS[0])])  # type: ignore[list-item]
    result, out, _ = _run(monkeypatch, tmp_path, channel)

    assert result is False
    assert "мусор" not in out
    payload = json.loads(next(tmp_path.glob("overlay_census_*.json")).read_text())
    assert [o["id"] for o in payload["overlays"]] == ["overlay-1"]


def test_bind_failure_is_fail_with_hint(monkeypatch, tmp_path) -> None:
    channel = FakeChannel(start_error=ChannelError("не удалось занять порт 8765"))
    result, out, _ = _run(monkeypatch, tmp_path, channel)

    assert result is True
    assert "[FAIL]" in out
    assert "run_extension_chrome.sh" in out
    assert channel.closed
    assert not list(tmp_path.glob("overlay_census_*.json"))


def test_extension_not_connected_is_fail(monkeypatch, tmp_path) -> None:
    channel = FakeChannel(client_error=ChannelError("расширение не подключилось"))
    result, out, _ = _run(monkeypatch, tmp_path, channel)

    assert result is True
    assert "[FAIL]" in out and "run_extension_chrome.sh" in out
    assert channel.closed


def test_tab_not_answering_is_fail(monkeypatch, tmp_path) -> None:
    channel = FakeChannel(read_error=PrimitiveError("timeout", "ответ не получен", True))
    result, out, _ = _run(monkeypatch, tmp_path, channel)

    assert result is True
    assert "[FAIL]" in out
    assert channel.closed
    assert not list(tmp_path.glob("overlay_census_*.json"))


def test_dump_write_failure_is_fail(monkeypatch, tmp_path) -> None:
    class ReadOnlyDir:
        def mkdir(self, *a, **kw) -> None:
            raise OSError("read-only")

    monkeypatch.setattr(overlay_census, "LiveChannel", lambda **_: FakeChannel())
    monkeypatch.setattr(overlay_census, "LOG_DIR", ReadOnlyDir())
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", captured.append)
    assert overlay_census.run(_args()) is True
    assert "[FAIL] overlay-census: дамп не записан" in captured[-1]


def test_register_wires_command() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    overlay_census.register(subparsers)
    args = parser.parse_args(["overlay-census"])
    assert args.func is overlay_census.run
    assert args.port == 8765
