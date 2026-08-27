#!/usr/bin/env python3
"""Check that release tags in the checked-in plugin marketplaces resolve remotely.

Manifest synchronization proves that generated version fields are current. It
does not prove that an installation can resolve the Git tag used by the
marketplace. This check closes that gap without changing the install-time
manifest: every URL source is queried with ``git ls-remote``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

MARKETPLACE_PATHS = (Path(".agents/plugins/marketplace.json"),)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read marketplace manifest {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"marketplace manifest must be a JSON object: {path}")
    return value


def _expected_tag(root: Path) -> str:
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read project.version from {root / 'pyproject.toml'}") from exc
    if not isinstance(version, str) or not version:
        raise ValueError("project.version must be a non-empty string")
    return f"v{version}"


def _tag_exists(url: str, ref: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--refs", url, f"refs/tags/{ref}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0 and result.stdout.strip():
        return True, ""
    detail = result.stderr.strip() or "remote ref was not found"
    return False, detail


def check_refs(root: Path) -> list[str]:
    """Return human-readable errors for URL refs missing from their remotes."""

    errors: list[str] = []
    try:
        expected_tag = _expected_tag(root)
    except ValueError as exc:
        return [str(exc)]
    for relative_path in MARKETPLACE_PATHS:
        path = root / relative_path
        try:
            manifest = _load(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        plugins = manifest.get("plugins")
        if not isinstance(plugins, list):
            errors.append(f"marketplace manifest has no plugins list: {relative_path}")
            continue
        for index, plugin in enumerate(plugins):
            if not isinstance(plugin, dict):
                errors.append(f"plugin entry {index} is not an object: {relative_path}")
                continue
            source = plugin.get("source")
            if not isinstance(source, dict) or source.get("source") != "url":
                continue
            url = source.get("url")
            ref = source.get("ref")
            label = f"{relative_path} plugins[{index}]"
            if not isinstance(url, str) or not url:
                errors.append(f"{label} has no Git URL")
                continue
            if not isinstance(ref, str) or not ref:
                errors.append(f"{label} has no Git ref")
                continue
            if ref != expected_tag:
                errors.append(
                    f"{label} tag {ref!r} does not match project version; expected {expected_tag!r}"
                )
                continue
            exists, detail = _tag_exists(url, ref)
            if not exists:
                errors.append(f"{label} tag {ref!r} does not resolve in {url}: {detail}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)

    errors = check_refs(args.root.resolve())
    if errors:
        print("Plugin marketplace refs are invalid:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Plugin marketplace refs resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
