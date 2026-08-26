#!/usr/bin/env python3
"""Генератор справочника CLI-команд в README из argparse.

Источник правды — код (cli.build_parser + команды/). Скрипт обходит дерево
парсера и рендерит markdown-блок, который вставляется между маркерами::

    <!-- BEGIN CLI REF -->
    ...
    <!-- END CLI REF -->

в README.md. CI гоняет ``gen_cli_docs.py --check`` и падает (``git diff``-стиль),
если кто-то добавил флаг/команду, но забыл перегенерить доку — авто-синхронизация
кода и документации по модели FastAPI/OpenAPI.

Ноль внешних зависимостей — только stdlib. Импорт build_parser идёт через src/
в sys.path (работает и из checkout без `pip install`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path, PurePath

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
BEGIN = "<!-- BEGIN CLI REF -->"
END = "<!-- END CLI REF -->"


def _opts(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Опции парсера (с option_strings), без служебного -h/--help."""
    return [a for a in parser._actions if a.option_strings and a.dest != "help"]


def _fmt_opt(action: argparse.Action, *, trim_help: bool = False) -> str:
    flag = ", ".join(action.option_strings)
    meta = ""
    if action.nargs != 0 and action.type not in (None, bool):
        meta = f" {getattr(action, 'metavar', None) or action.dest.upper()}"
    if isinstance(action.default, PurePath):
        # Keep generated docs identical on POSIX and Windows. ``repr(Path)``
        # otherwise changes from ``PosixPath`` to ``WindowsPath``.
        default_repr = f"PosixPath({action.default.as_posix()!r})"
    else:
        default_repr = repr(action.default)
    default = "" if not action.default else f" (по умолчанию: {default_repr})"
    help_text = (action.help or "").strip() if trim_help else action.help or ""
    if trim_help:
        description = f" — {help_text}" if help_text else ""
    else:
        description = f" — {help_text}"
    return f"`{flag}{meta}`{description}{default}"


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def render() -> str:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from hhru_bot.cli import build_parser  # noqa: E402

    parser = build_parser()
    lines: list[str] = [BEGIN, ""]
    lines.append("**Глобальные флаги** (до имени команды):")
    lines.append("")
    for a in _opts(parser):
        lines.append(f"- {_fmt_opt(a)}")
    lines.append("")
    lines.append("**Команды**:")
    lines.append("")

    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name in sorted(sub_action.choices):
        sub: argparse.ArgumentParser = sub_action.choices[name]
        desc = (sub.description or "").strip()
        lines.append(f"### `{name}`")
        if desc:
            lines.append("")
            lines.append(desc)
        lines.append("")
        opts = _opts(sub)
        if opts:
            for a in opts:
                lines.append(f"- {_fmt_opt(a)}")
        else:
            lines.append("- (без аргументов)")
        lines.append("")
        nested = _subparsers(sub)
        if nested is not None:
            for nested_name in sorted(nested.choices):
                nested_parser = nested.choices[nested_name]
                lines.append(f"#### `{name} {nested_name}`")
                nested_desc = (nested_parser.description or "").strip()
                if nested_desc:
                    lines.append("")
                    lines.append(nested_desc)
                lines.append("")
                nested_opts = _opts(nested_parser)
                if nested_opts:
                    for a in nested_opts:
                        lines.append(f"- {_fmt_opt(a, trim_help=True)}")
                else:
                    lines.append("- (без аргументов)")
                lines.append("")
    lines.append(END)
    return "\n".join(lines)


def _splice(readme: str, block: str) -> str:
    start = readme.find(BEGIN)
    end = readme.find(END)
    if start == -1 or end == -1 or end < start:
        raise SystemExit(f"README: маркеры {BEGIN}/{END} не найдены или перепутаны")
    return readme[:start] + block + readme[end + len(END) :]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Генератор CLI-справочника в README")
    ap.add_argument(
        "--check",
        action="store_true",
        help="Сравнить с README, выйти ненулём при рассинхроне",
    )
    ap.add_argument("--readme", default=str(README), help="Путь к README.md")
    args = ap.parse_args(argv)

    readme_path = Path(args.readme)
    readme = readme_path.read_text(encoding="utf-8")
    block = render()
    updated = _splice(readme, block)
    if args.check:
        if updated != readme:
            print("CLI REF рассинхронизирован с argparse. Перезапустите:", file=sys.stderr)
            print("  python3 scripts/gen_cli_docs.py", file=sys.stderr)
            return 1
        print("CLI REF синхронизирован.")
        return 0
    readme_path.write_text(updated, encoding="utf-8")
    print(f"CLI REF обновлён в {readme_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
