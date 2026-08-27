#!/usr/bin/env python3
"""Synchronize plugin metadata from ``pyproject.toml``.

The Claude Code and Codex marketplace files are intentionally different
schemas: Claude consumes the local ``source: ./`` entry, while the Codex team
marketplace consumes a Git URL plus installation policy. They are two
supported entry points, not interchangeable copies. This script keeps their
shared release version and Codex release ref generated from the package's
single source of truth.

Run ``python3 scripts/sync_plugin_manifests.py`` after changing the project
version. CI can use ``--check`` to make a stale generated manifest fail.
Commit provenance is not written into tracked manifests: a manifest in a
floating checkout cannot contain its own commit hash. Runtime diagnostics
resolve the actual checkout/cache SHA instead.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATHS = (
    Path(".codex-plugin/plugin.json"),
    Path(".claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
    Path(".agents/plugins/marketplace.json"),
)


def project_version(root: Path = ROOT) -> str:
    """Read the only supported version source."""
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            value = tomllib.load(stream)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read project.version from {root / 'pyproject.toml'}") from exc
    if not isinstance(value, str) or not value:
        raise ValueError("project.version must be a non-empty string")
    return value


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def expected_manifests(root: Path = ROOT) -> dict[Path, dict[str, Any]]:
    """Return current manifests with all shared version fields regenerated."""
    value = project_version(root)
    manifests = {path: _load(root / path) for path in MANIFEST_PATHS}

    codex = manifests[Path(".codex-plugin/plugin.json")]
    codex["version"] = value

    claude = manifests[Path(".claude-plugin/marketplace.json")]
    claude.setdefault("metadata", {})["version"] = value
    claude["plugins"][0]["version"] = value
    manifests[Path(".claude-plugin/plugin.json")]["version"] = value

    agents = manifests[Path(".agents/plugins/marketplace.json")]
    agents.setdefault("metadata", {})["version"] = value
    agent_plugin = agents["plugins"][0]
    agent_plugin["version"] = value
    source = agent_plugin.get("source")
    if isinstance(source, dict) and source.get("source") == "url":
        source["ref"] = f"v{value}"

    return manifests


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def synchronize(root: Path = ROOT, *, check: bool = False) -> bool:
    """Write synchronized manifests, or report whether they are current."""
    expected = expected_manifests(root)
    stale: list[Path] = []
    for relative, value in expected.items():
        path = root / relative
        rendered = _dump(value)
        if path.read_text(encoding="utf-8") != rendered:
            stale.append(relative)
            if not check:
                path.write_text(rendered, encoding="utf-8")
    if stale and check:
        print("Plugin manifests are stale; run:")
        print("  python3 scripts/sync_plugin_manifests.py")
        for path in stale:
            print(f"  - {path}")
        return False
    if check:
        print("Plugin manifests are synchronized.")
    else:
        print("Plugin manifests synchronized.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="checkout root (for tests and tooling)"
    )
    parser.add_argument("--check", action="store_true", help="fail when generated files are stale")
    args = parser.parse_args(argv)
    return 0 if synchronize(args.root.resolve(), check=args.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
