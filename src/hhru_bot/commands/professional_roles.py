"""Cached read-only search and explicit refresh for hh.ru professional roles."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..professional_roles import DEFAULT_CACHE_PATH
from ..report import _ascii_table


def _positive_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("должен быть положительным")
    return parsed


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "professional-roles",
        help="Поиск по локальному кэшу профессий hh.ru и явное обновление каталога",
        description=(
            "Без --refresh читает только data/cache/professional_roles.json. "
            "--refresh открывает live-каталог hh.ru, но не выбирает профессии "
            "и не нажимает «Сохранить»."
        ),
    )
    p.add_argument(
        "--query",
        action="append",
        help="Короткое название профессии для локального поиска (можно повторять)",
    )
    p.add_argument(
        "--limit",
        type=_positive_limit,
        default=20,
        help="Максимум кандидатов в выводе",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Явно перечитать полный live-каталог и атомарно обновить локальный кэш",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..professional_roles import (
        ProfessionalRoleCacheError,
        collect_professional_role_catalog,
        load_professional_role_cache,
        professional_role_cache_is_stale,
        search_cached_professional_roles,
        write_professional_role_cache,
    )

    cache_path = Path(DEFAULT_CACHE_PATH)
    if not args.refresh and not args.query:
        print("[FAIL] Передайте --query для локального поиска или --refresh для обновления.")
        return True

    if args.refresh:
        from ..browser import BrowserLaunchError, launch_context
        from ..config import load_config_or_exit

        config = load_config_or_exit(args.config)
        # The refresh clicks category chevrons only. It never checks a role or
        # submits the filter modal, so the external account remains unchanged.
        try:
            with launch_context(
                config.storage_state_file,
                headless=args.headless,
                user_agent=config.user_agent,
            ) as context:
                catalog = collect_professional_role_catalog(context.new_page())
            write_professional_role_cache(catalog, cache_path)
        except BrowserLaunchError:
            raise
        except (OSError, ProfessionalRoleCacheError, RuntimeError) as exc:
            print(f"[FAIL] Кэш каталога не обновлён: {exc}. Предыдущий валидный снимок сохранён.")
            return True
        print(
            f"[OK] Кэш каталога профессий обновлён: "
            f"{len(catalog.categories)} категорий, {len(catalog.roles)} профессий."
        )

    if not args.query:
        return False

    try:
        catalog = load_professional_role_cache(cache_path)
    except ProfessionalRoleCacheError as exc:
        print(f"[FAIL] {exc}")
        return True
    if professional_role_cache_is_stale(catalog):
        fetched_at = catalog.fetched_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        print(
            f"[WARN] Кэш каталога старше 7 дней (снимок: {fetched_at}). "
            "Обновите: hhru professional-roles --refresh"
        )

    roles = search_cached_professional_roles(catalog, args.query, limit=args.limit)
    if not roles:
        print(
            "[INFO] Кандидаты не найдены. Используйте короткий русский термин "
            'профессии, например: hhru professional-roles --query "разработчик"'
        )
        return False
    rows = [
        [role.role_id, role.label, ", ".join(role.categories) or role.category] for role in roles
    ]
    print(_ascii_table(["role_id", "profession", "category"], rows))
    print("[INFO] Это кандидаты из каталога, а не автоматическая классификация.")
    return False
