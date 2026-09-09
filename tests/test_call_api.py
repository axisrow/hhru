from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import call_api
from hhru_bot.commands.call_api import CallApiError, _endpoint_url

pytestmark = pytest.mark.integration


def test_endpoint_defaults_to_hh_and_encodes_params():
    assert _endpoint_url("/employers", ["text=IT", "only_with_vacancies=true"]) == (
        "https://api.hh.ru/employers?text=IT&only_with_vacancies=true"
    )


def test_full_api_url_is_allowed():
    assert _endpoint_url("https://api.hh.ru/vacancies", ["text=C++"]) == (
        "https://api.hh.ru/vacancies?text=C%2B%2B"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/vacancies",
        "http://hh.ru/vacancies",
        "//evil.example/vacancies",
        "https://api.hh.ru.evil.example/vacancies",
    ],
)
def test_endpoint_rejects_non_allowlisted_hosts(endpoint):
    with pytest.raises(CallApiError):
        _endpoint_url(endpoint, [])


def test_parameter_must_be_key_value():
    with pytest.raises(CallApiError, match="key=value"):
        _endpoint_url("/vacancies", ["text"])


def test_run_uses_authenticated_browser_request_and_prints_body(monkeypatch, capsys):
    class Response:
        ok = True
        status = 200

        @staticmethod
        def text():
            return '{"items": []}'

    class Request:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            return Response()

    class Context:
        def __init__(self):
            self.request = Request()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        @staticmethod
        def new_page():
            return object()

    context = Context()
    monkeypatch.setattr(call_api, "HH_BASE_URL", "https://hh.ru")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _: type("Config", (), {"storage_state_file": "session.json", "user_agent": None})(),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: context)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *a, **kw: None)
    monkeypatch.setattr("hhru_bot.browser.require_authenticated_page", lambda page: None)

    call_api.run(
        argparse.Namespace(
            method="GET",
            endpoint="/employers",
            params=["text=IT"],
            config="config.yaml",
            headless=True,
        )
    )

    assert context.request.urls == ["https://api.hh.ru/employers?text=IT"]
    assert capsys.readouterr().out == '{"items": []}\n'


# --- C1 (issue-city-search-gaps): [FAIL] вместо traceback -------------------
#
# run() сама ловит ожидаемые ошибки (CallApiError, не-2xx HTTP) и печатает
# [FAIL] + возвращает True (fail-closed контракт cli.py: failed is True ->
# sys.exit(1), паттерн config_cmd). Сырой traceback в stderr больше не норма.


def _patch_browser(monkeypatch, response):
    """Мок браузерного контура call-api с заданным объектом ответа."""

    class Request:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            return response

    class Context:
        def __init__(self):
            self.request = Request()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        @staticmethod
        def new_page():
            return object()

    context = Context()
    monkeypatch.setattr(call_api, "HH_BASE_URL", "https://hh.ru")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _: type("Config", (), {"storage_state_file": "session.json", "user_agent": None})(),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: context)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *a, **kw: None)
    monkeypatch.setattr("hhru_bot.browser.require_authenticated_page", lambda page: None)
    return context


class _FailResponse:
    ok = False
    status = 403

    @staticmethod
    def text():
        return '{"errors":[{"type":"forbidden"}],"request_id":"abc123"}'


def test_run_non_2xx_prints_fail_with_error_body_and_returns_true(monkeypatch, capsys):
    _patch_browser(monkeypatch, _FailResponse())

    result = call_api.run(
        argparse.Namespace(
            method="GET",
            endpoint="/vacancies",
            params=["text=сборщик"],
            config="config.yaml",
            headless=True,
        )
    )

    assert result is True
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.out
    assert "403" in captured.out
    # тело ошибки api.hh.ru структурно и полезно для диагностики (A3 findings)
    assert "forbidden" in captured.out
    assert "abc123" in captured.out
    assert captured.err == ""  # никакого traceback


def test_run_non_2xx_body_is_single_line_and_truncated(monkeypatch, capsys):
    class Response:
        ok = False
        status = 500

        @staticmethod
        def text():
            # Перенос в конце, не в начале: префикс "line1 " сдвинул бы арифметику
            # усечения (body[:300] содержал бы только 294 "x"), а схлопывание
            # всё равно доказано проверкой len(splitlines()) == 1 ниже.
            return "x" * 400 + "\nline2"

    _patch_browser(monkeypatch, Response())

    result = call_api.run(
        argparse.Namespace(
            method="GET", endpoint="/areas", params=[], config="config.yaml", headless=True
        )
    )

    assert result is True
    out = capsys.readouterr().out
    fail_line = out.splitlines()[0]
    assert fail_line.startswith("[FAIL]")
    assert len(out.splitlines()) == 1  # тело схлопнуто в одну строку
    assert "x" * 300 in fail_line
    assert "x" * 301 not in fail_line


def test_run_invalid_endpoint_fails_before_browser(monkeypatch, capsys):
    def _no_browser(*_a, **_kw):
        raise AssertionError("браузер не должен открываться при невалидном endpoint")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _no_browser)

    result = call_api.run(
        argparse.Namespace(
            method="GET", endpoint="//evil.example/x", params=[], config=None, headless=True
        )
    )

    assert result is True
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.out
    assert captured.err == ""


def test_run_invalid_param_fails_before_browser(monkeypatch, capsys):
    def _no_browser(*_a, **_kw):
        raise AssertionError("браузер не должен открываться при невалидном параметре")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _no_browser)

    result = call_api.run(
        argparse.Namespace(
            method="GET", endpoint="/vacancies", params=["без_равно"], config=None, headless=True
        )
    )

    assert result is True
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.out
    assert "key=value" in captured.out


def test_run_non_get_method_fails_without_traceback(capsys):
    result = call_api.run(
        argparse.Namespace(method="POST", endpoint="/areas", params=[], config=None, headless=True)
    )

    assert result is True
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.out
    assert captured.err == ""


def test_run_success_returns_none(monkeypatch, capsys):
    class Response:
        ok = True
        status = 200

        @staticmethod
        def text():
            return '{"items": []}'

    _patch_browser(monkeypatch, Response())

    result = call_api.run(
        argparse.Namespace(
            method="GET", endpoint="/areas", params=[], config="config.yaml", headless=True
        )
    )

    assert result is None
    assert capsys.readouterr().out == '{"items": []}\n'
