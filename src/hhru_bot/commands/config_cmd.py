"""Read and edit ``config.yaml`` without opening a browser."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "config",
        help="Прочитать или изменить config.yaml",
        description="Показать и локально изменить значения в config.yaml.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "-p", "--path", action="store_true", help="Показать полный путь к config.yaml"
    )
    actions.add_argument("-e", "--edit", action="store_true", help="Открыть config.yaml в $EDITOR")
    actions.add_argument("-k", "--key", metavar="DOTTED_KEY", help="Получить значение по ключу")
    actions.add_argument(
        "-s", "--set", nargs=2, metavar=("KEY", "VALUE"), help="Установить значение ключа"
    )
    actions.add_argument("-u", "--unset", metavar="KEY", help="Удалить ключ")
    parser.set_defaults(func=run)


def _read(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"Файл конфига не найден: {path}") from exc


def _mapping(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Корень config.yaml должен быть YAML-словарём")
    return raw


def _lookup(raw: dict[str, Any], key: str) -> Any:
    value: Any = raw
    for part in key.split("."):
        if not part or not isinstance(value, dict) or part not in value:
            raise KeyError(key)
        value = value[part]
    return value


def _set(raw: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    if any(not part for part in parts):
        raise ValueError(f"Некорректный ключ: {key!r}")
    target = raw
    for part in parts[:-1]:
        current = target.get(part)
        if current is None:
            current = {}
            target[part] = current
        if not isinstance(current, dict):
            raise ValueError(f"Нельзя пройти через не-словарь в ключе: {key!r}")
        target = current
    target[parts[-1]] = value


def _unset(raw: dict[str, Any], key: str) -> None:
    parts = key.split(".")
    if any(not part for part in parts):
        raise ValueError(f"Некорректный ключ: {key!r}")
    target: Any = raw
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return
        target = target[part]
    if isinstance(target, dict):
        target.pop(parts[-1], None)


def _dump(raw: Any, path: Path) -> None:
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)


def _validated_replace(path: Path, raw: dict[str, Any]) -> None:
    """Validate the candidate with the production loader before replacing the file."""
    from ..config import load_config

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    candidate = Path(name)
    try:
        os.close(fd)
        _dump(raw, candidate)
        load_config(candidate)
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def _edit(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise ValueError("Не задан $EDITOR (или $VISUAL)")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _mapping(_read(path))
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".yaml", dir=path.parent)
    candidate = Path(name)
    os.close(fd)
    _dump(raw, candidate)
    result = subprocess.run([*shlex.split(editor), str(candidate)], check=False)
    if result.returncode:
        raise ValueError(
            f"Редактор завершился с кодом {result.returncode}. Правки сохранены в {candidate}"
        )
    try:
        _validated_replace(path, _mapping(_read(candidate)))
    except (ConfigError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        raise ValueError(f"{exc}. Правки сохранены в {candidate}") from exc
    candidate.unlink(missing_ok=True)


def _run(args: argparse.Namespace) -> None:
    path = Path(args.config).expanduser()
    if args.path:
        print(path.resolve())
        return
    if args.edit:
        _edit(path)
        print("[OK] Конфиг обновлён")
        return

    raw = _mapping(_read(path))
    if args.key:
        try:
            value = _lookup(raw, args.key)
        except KeyError as exc:
            raise ValueError(f"Ключ не найден: {args.key}") from exc
        if isinstance(value, dict | list):
            print(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), end="")
        else:
            print(value)
        return
    if args.set is not None:
        key, value_text = args.set
        _set(raw, key, yaml.safe_load(value_text))
        _validated_replace(path, raw)
        print("[OK] Конфиг обновлён")
        return
    if args.unset:
        _unset(raw, args.unset)
        _validated_replace(path, raw)
        print("[OK] Конфиг обновлён")
        return
    print(path.read_text(encoding="utf-8"), end="")


def run(args: argparse.Namespace) -> bool | None:
    """Run config and turn expected input/config errors into CLI failures."""
    try:
        return _run(args)
    except (ConfigError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        print(f"[FAIL] {exc}")
        return True
