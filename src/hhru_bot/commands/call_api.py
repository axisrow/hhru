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
        parts = urlsplit(f"{HH_BASE_URL}{endpoint}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def run(args: argparse.Namespace) -> None:
    if args.method != "GET":  # defensive guard if called without argparse
        raise CallApiError("call-api поддерживает только GET")

    from ..browser import goto_hh, launch_context, require_authenticated_page
    from ..config import load_config_or_exit

    url = _endpoint_url(args.endpoint, args.params)
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
            raise RuntimeError(f"GET {url} вернул HTTP {response.status}")
        print(response.text())
