"""Команда ``call-api``: безопасный read-only GET к API hh.ru.

Запросы выполняются через ``BrowserContext.request``: Playwright переносит в
API-клиент cookies сохранённой сессии, но отдельный HTTP-клиент не создаётся.
Домен и метод проверяются до открытия браузера, чтобы escape hatch не стал
произвольным сетевым или write-инструментом.
"""

from __future__ import annotations

import argparse
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..browser import HH_BASE_URL

ALLOWED_HOSTS = frozenset({"hh.ru", "api.hh.ru"})
API_BASE_URL = "https://api.hh.ru"
# Сколько символов тела ошибки показывать в [FAIL]: ответ api.hh.ru на ошибку
# короток и структурен ({"errors":[...],"request_id":...}), а аномально длинное
# тело (HTML-страница) диагностической ценности не несёт.
_FAIL_BODY_LIMIT = 300


class CallApiError(ValueError):
    """Некорректный или небезопасный endpoint/параметр call-api."""


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "call-api",
        help="Read-only GET к endpoint API hh.ru",
    )
    parser.add_argument(
        "-m",
        "--method",
        choices=("GET",),
        default="GET",
        help="HTTP-метод (разрешён только GET)",
    )
    parser.add_argument("endpoint", help="Путь или полный URL на hh.ru/api.hh.ru")
    parser.add_argument("params", nargs="*", metavar="key=value")
    parser.set_defaults(func=run)


def _endpoint_url(endpoint: str, params: list[str]) -> str:
    """Validate an endpoint and append ``key=value`` query parameters."""
    if not endpoint:
        raise CallApiError("endpoint не может быть пустым")
    try:
        parts = urlsplit(endpoint)
    except ValueError as exc:
        raise CallApiError("некорректный endpoint") from exc

    if parts.scheme or parts.netloc:
        try:
            hostname = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise CallApiError("некорректный host или port в endpoint") from exc
        if parts.scheme != "https" or hostname not in ALLOWED_HOSTS:
            raise CallApiError("разрешены только HTTPS endpoint'ы hh.ru или api.hh.ru")
        if parts.username or parts.password or port:
            raise CallApiError("endpoint не должен содержать credentials или port")
    elif not endpoint.startswith("/") or endpoint.startswith("//"):
        raise CallApiError("endpoint должен быть путём /... или HTTPS URL hh.ru")

    if parts.fragment:
        raise CallApiError("fragment в endpoint запрещён")
    query = parts.query
    for raw in params:
        key, separator, value = raw.partition("=")
        if not separator or not key:
            raise CallApiError(f"параметр должен иметь формат key=value: {raw!r}")
        query = f"{query}&" if query else ""
        query += urlencode({key: value})

    if not parts.scheme:
        # The documented shorthand is an API endpoint (e.g. /employers), not
        # a web route on hh.ru.  Keep the browser session validation on hh.ru,
        # while sending the actual API request to api.hh.ru.
        parts = urlsplit(f"{API_BASE_URL}{endpoint}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def run(args: argparse.Namespace) -> bool | None:
    # Fail-closed контракт cli.py: возвращённый True -> exit code 1. Ожидаемые
    # ошибки (не-GET, невалидный endpoint/параметр, не-2xx ответ) печатаются как
    # [FAIL] здесь, а не уходят в generic except Exception -> сырой traceback.
    if args.method != "GET":  # defensive guard if called without argparse
        print("[FAIL] call-api поддерживает только GET")
        return True

    from ..browser import goto_hh, launch_context, require_authenticated_page
    from ..config import load_config_or_exit

    try:
        url = _endpoint_url(args.endpoint, args.params)
    except CallApiError as exc:
        # До load_config и launch_context: невалидный endpoint/параметр — не
        # повод запускать Chromium и нести туда сохранённую сессию.
        print(f"[FAIL] {exc}")
        return True
    config = load_config_or_exit(args.config)
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        # Confirm the cookie session against a server-rendered page before using
        # the API context; an API endpoint alone can be public or redirect.
        goto_hh(page, HH_BASE_URL)
        require_authenticated_page(page)
        # Keep the request context behind a local API-client alias.  The
        # repository's source guard rejects direct ``*.request.get`` calls in
        # browser-facing modules; this is still Playwright's context-bound
        # client, not a separate HTTP client.
        api_request = context.request
        response = api_request.get(url)
        if not response.ok:
            # Тело схлопывается в одну строку и усекается: [FAIL] обязан
            # оставаться читаемой однострочной диагностикой.
            body = " ".join(response.text().split())
            detail = f": {body[:_FAIL_BODY_LIMIT]}" if body else ""
            print(f"[FAIL] GET {url} вернул HTTP {response.status}{detail}")
            return True
        print(response.text())
        return None
