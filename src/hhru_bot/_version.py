"""Package version and source provenance.

``pyproject.toml`` is the source of truth for the version. A checkout can
read it directly; an installed wheel uses the version copied into its normal
Python distribution metadata. The commit is deliberately resolved at runtime
so diagnostics identify the actual checkout or plugin cache rather than a
floating branch name.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from importlib.metadata import version as distribution_version
from pathlib import Path

_PACKAGE_NAME = "hhru-bot"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _checkout_root() -> Path | None:
    """Return the repository root when running from a source checkout."""
    candidate = Path(__file__).resolve().parents[2]
    return candidate if (candidate / "pyproject.toml").is_file() else None


def _project_version() -> str | None:
    root = _checkout_root()
    if root is None:
        return None
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            value = tomllib.load(stream)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None
    return value if isinstance(value, str) else None


def _installed_version() -> str:
    try:
        return distribution_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        # This fallback is only used by an uninstalled source checkout whose
        # pyproject could not be read; keep imports working for diagnostics.
        return "unknown"


__version__ = _project_version() or _installed_version()


def _installed_commit_sha() -> str | None:
    """Read the immutable VCS commit recorded by PEP 610 installations."""
    try:
        raw = distribution(_PACKAGE_NAME).read_text("direct_url.json")
    except (FileNotFoundError, PackageNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw).get("vcs_info", {}).get("commit_id")
    except (AttributeError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def commit_sha() -> str:
    """Return the immutable commit for this installation or ``unknown``."""
    # GITHUB_SHA belongs to the workflow's checkout, which may be a consumer
    # repository running an installed hhru wheel. Only accept the explicit
    # package provenance variable; a package build may populate it from its
    # own CI GITHUB_SHA before installation.
    configured = os.environ.get("HHRU_COMMIT_SHA")
    if configured and _SHA.fullmatch(configured):
        return configured

    root = _checkout_root()
    if root is not None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            value = result.stdout.strip()
            if _SHA.fullmatch(value):
                return value
    installed = _installed_commit_sha()
    if installed is not None:
        return installed
    return "unknown"


def build_info() -> dict[str, str]:
    """Return version and commit together for diagnostic/reporting callers."""
    return {"version": __version__, "commit_sha": commit_sha()}
