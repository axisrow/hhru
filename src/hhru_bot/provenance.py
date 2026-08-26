"""Build identity checks for the CLI and the Codex plugin lifecycle.

The release job is the eventual source of the package/plugin identity (#673).
Until that source lands, this module deliberately accepts the identity emitted
by either git (editable/check-out installs) or a manifest/provenance file.  The
doctor therefore does not silently turn a missing SHA into a successful check.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_NAME = "hhru-cc-plugin"
MARKETPLACE_NAME = "hhru"
RECOVERY_COMMAND = "codex plugin marketplace upgrade hhru --json"


@dataclass(frozen=True)
class ComponentIdentity:
    """Version/provenance observed for one lifecycle component."""

    name: str
    version: str | None
    release: str | None
    commit_sha: str | None
    location: Path | None = None
    source: str | None = None

    @property
    def complete(self) -> bool:
        # An untagged development checkout legitimately has no release tag;
        # its full commit SHA is still a sufficient provenance anchor.
        return bool(self.version and self.commit_sha)

    def describe(self) -> str:
        version = self.version or "неизвестна"
        release = self.release or "неизвестен"
        sha = self.commit_sha or "неизвестен"
        return f"version={version}, release={release}, sha={sha}"


@dataclass(frozen=True)
class DoctorResult:
    """Comparison result for the three lifecycle components."""

    components: tuple[ComponentIdentity, ...]
    drift: bool
    reasons: tuple[str, ...]


def run_git(path: Path, *args: str) -> str | None:
    """Run a read-only git query, returning ``None`` outside a checkout."""

    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _git_root(path: Path) -> Path | None:
    root = run_git(path, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else None


def _source_version(root: Path) -> str | None:
    """Read the checkout's declared version without using installed metadata."""

    try:
        payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = payload.get("project")
    return _first_string((project.get("version"),)) if isinstance(project, dict) else None


def _git_identity(name: str, path: Path, *, manifest_version: str | None = None):
    root = _git_root(path)
    if root is None:
        return None
    sha = run_git(root, "rev-parse", "HEAD")
    if sha is None:
        return None
    # An editable install has no reliable wheel metadata.  Read its declared
    # source version, then use git for release/SHA provenance.  A git
    # description is only a last-resort version for a checkout without a
    # pyproject, never a replacement for a semantic package version.
    version = (
        manifest_version
        or _source_version(root)
        or run_git(root, "describe", "--tags", "--always", "HEAD")
    )
    release = run_git(root, "describe", "--tags", "--exact-match", "HEAD")
    return ComponentIdentity(name, version or manifest_version, release, sha, root, "git")


def _first_string(values: Iterable[Any]) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _provenance_values(*objects: Any) -> tuple[str | None, str | None]:
    """Extract release/tag and SHA from current and future manifest shapes."""

    release_values: list[Any] = []
    sha_values: list[Any] = []
    pending = list(objects)
    while pending:
        obj = pending.pop(0)
        if not isinstance(obj, dict):
            continue
        nested = obj.get("provenance")
        if isinstance(nested, dict):
            pending.append(nested)
        release_values.extend(obj.get(key) for key in ("release", "tag", "release_tag"))
        sha_values.extend(obj.get(key) for key in ("commit_sha", "git_commit_sha", "commit", "sha"))
        vcs_info = obj.get("vcs_info")
        if isinstance(vcs_info, dict):
            sha_values.append(vcs_info.get("commit_id"))
        source = obj.get("source")
        if isinstance(source, dict):
            release_values.extend(source.get(key) for key in ("release", "tag"))
            sha_values.extend(
                source.get(key) for key in ("commit_sha", "git_commit_sha", "commit", "sha")
            )
    return _first_string(release_values), _first_string(sha_values)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_json_text(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_identity(name: str, root: Path, manifest: Path) -> ComponentIdentity:
    payload = _load_json(manifest) or {}
    plugin: dict[str, Any] = {}
    plugins = payload.get("plugins")
    if isinstance(plugins, list):
        plugin = next(
            (
                item
                for item in plugins
                if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
            ),
            {},
        )
    version = _first_string(
        (
            plugin.get("version"),
            payload.get("version"),
            (payload.get("metadata") or {}).get("version")
            if isinstance(payload.get("metadata"), dict)
            else None,
        )
    )
    release, sha = _provenance_values(
        payload, plugin, payload.get("metadata"), plugin.get("source")
    )
    return ComponentIdentity(name, version, release, sha, root, "manifest")


def _manifest_for(root: Path, *, marketplace: bool = False) -> Path | None:
    candidates = (
        (
            root / ".claude-plugin" / "marketplace.json",
            root / ".agents" / "plugins" / "marketplace.json",
            root / ".codex-plugin" / "plugin.json",
        )
        if marketplace
        else (root / ".codex-plugin" / "plugin.json",)
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _identity_from_root(name: str, root: Path, *, marketplace: bool = False) -> ComponentIdentity:
    manifest = _manifest_for(root, marketplace=marketplace)
    manifest_identity = _manifest_identity(name, root, manifest) if manifest else None
    identity = _git_identity(
        name, root, manifest_version=manifest_identity.version if manifest_identity else None
    )
    if identity is not None:
        return identity
    if manifest_identity is not None:
        return manifest_identity
    return ComponentIdentity(name, None, None, None, root, "missing")


def _package_location() -> Path:
    # __file__ is in the actual imported package, including an editable install.
    return Path(__file__).resolve()


def cli_identity() -> ComponentIdentity:
    """Inspect the installed CLI, preferring its checkout when editable."""

    path = _package_location()
    package_root = path.parent
    identity = _git_identity("installed CLI", package_root)
    if identity is not None:
        return identity
    try:
        version = importlib.metadata.version("hhru-bot")
        distribution = importlib.metadata.distribution("hhru-bot")
        direct_url = _load_json_text(distribution.read_text("direct_url.json"))
        release, sha = _provenance_values(direct_url)
    except importlib.metadata.PackageNotFoundError:
        version, release, sha = None, None, None
    # #673 can replace this module's generated values without changing the
    # doctor.  Keep the names permissive so both a generated ``__tag__`` and a
    # generated ``__release__`` remain valid inputs during the transition.
    try:
        from . import _version as source_version

        version = version or _first_string((getattr(source_version, "__version__", None),))
        release = release or _first_string(
            (getattr(source_version, "__release__", None), getattr(source_version, "__tag__", None))
        )
        sha = sha or _first_string(
            (
                getattr(source_version, "__commit_sha__", None),
                getattr(source_version, "__git_commit_sha__", None),
            )
        )
    except ImportError:
        pass
    return ComponentIdentity("installed CLI", version, release, sha, package_root, "package")


def _default_marketplace_path() -> Path | None:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    candidates = [
        codex_home / "plugins" / "marketplaces" / MARKETPLACE_NAME,
        Path("~/.claude/plugins/marketplaces/hhru").expanduser(),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def marketplace_identity(path: Path | None = None) -> ComponentIdentity:
    path = path.expanduser() if path else _default_marketplace_path()
    if path is None:
        return ComponentIdentity("marketplace snapshot", None, None, None, None, "missing")
    if path.is_file():
        return _manifest_identity("marketplace snapshot", path.parent.parent, path)
    return _identity_from_root("marketplace snapshot", path, marketplace=True)


def _plugin_cache_candidate(cache: Path) -> Path | None:
    if cache.is_file():
        return cache.parent.parent if cache.name == "plugin.json" else None
    if (cache / ".codex-plugin" / "plugin.json").is_file():
        return cache
    candidates = sorted(
        (path.parent.parent for path in cache.glob("*/.codex-plugin/plugin.json")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _default_plugin_cache_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return codex_home / "plugins" / "cache" / "hhru" / PLUGIN_NAME


def plugin_cache_identity(path: Path | None = None) -> ComponentIdentity:
    cache = path.expanduser() if path else _default_plugin_cache_path()
    candidate = _plugin_cache_candidate(cache) if cache.exists() else None
    if candidate is None:
        return ComponentIdentity("installed plugin cache", None, None, None, cache, "missing")
    return _identity_from_root("installed plugin cache", candidate)


def collect_identities(
    *, marketplace: Path | None = None, plugin_cache: Path | None = None
) -> tuple[ComponentIdentity, ...]:
    """Collect the three states compared by ``hhru diagnostics doctor``."""

    return (cli_identity(), marketplace_identity(marketplace), plugin_cache_identity(plugin_cache))


def compare_identities(components: Iterable[ComponentIdentity]) -> DoctorResult:
    components = tuple(components)
    reasons: list[str] = []
    for component in components:
        if not component.complete:
            reasons.append(f"{component.name}: provenance неполная ({component.describe()})")
    if components:
        for field, label in (
            ("version", "version"),
            ("release", "release/tag"),
            ("commit_sha", "commit SHA"),
        ):
            values = {getattr(component, field) for component in components}
            if len(values) > 1:
                reasons.append(
                    f"разный {label}: "
                    + "; ".join(f"{c.name}={getattr(c, field) or 'неизвестен'}" for c in components)
                )
    return DoctorResult(components, bool(reasons), tuple(reasons))


def run_doctor(
    *, marketplace: Path | None = None, plugin_cache: Path | None = None
) -> DoctorResult:
    return compare_identities(
        collect_identities(marketplace=marketplace, plugin_cache=plugin_cache)
    )
