from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import call_api
from hhru_bot.commands.call_api import CallApiError, _endpoint_url


def test_endpoint_defaults_to_hh_and_encodes_params():
    assert _endpoint_url("/employers", ["text=IT", "only_with_vacancies=true"]) == (
        "https://hh.ru/employers?text=IT&only_with_vacancies=true"
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

    assert context.request.urls == ["https://hh.ru/employers?text=IT"]
    assert capsys.readouterr().out == '{"items": []}\n'
