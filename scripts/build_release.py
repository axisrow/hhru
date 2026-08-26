#!/usr/bin/env python3
"""Build and validate the release bundle for hhru's CLI and plugins.

The Python package and the agent plugins are released from the same checked-out
tag.  This script creates the plugin/marketplace part of that release and
stamps every manifest with the tag and commit that produced it.  It deliberately
does not mutate the working tree: checked-in manifests are development
templates, while the files in the archive are the installable release files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY_URL = "https://github.com/axisrow/hhru.git"
TAG_PREFIX = "v"
RELEASE_FILE = "release.json"
MANIFESTS = (
    Path(".codex-plugin/plugin.json"),
    Path(".claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
    Path(".agents/plugins/marketplace.json"),
)
PLUGIN_FILES = (
    Path(".codex-plugin"),
    Path(".claude-plugin"),
    Path(".agents"),
    Path("commands"),
    Path("skills"),
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseError(ValueError):
    """Raised when a release cannot be proven to be self-consistent."""


def _read_version(source_root: Path) -> str:
    try:
        import tomllib

        pyproject = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
        version = pyproject["project"]["version"]
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ReleaseError("pyproject.toml does not contain project.version") from exc
    if not isinstance(version, str) or not version:
        raise ReleaseError("project.version must be a non-empty string")
    return version


def _validate_release_identity(version: str, tag: str, commit_sha: str) -> None:
    expected_tag = f"{TAG_PREFIX}{version}"
    if tag != expected_tag:
        raise ReleaseError(
            f"release tag {tag!r} does not match project version {version!r}; "
            f"expected {expected_tag!r}"
        )
    if not SHA_RE.fullmatch(commit_sha):
        raise ReleaseError("commit SHA must be a full 40-character lowercase Git SHA")


def _git(source_root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=source_root, text=True, stderr=subprocess.STDOUT
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ReleaseError(f"cannot determine Git release provenance: {detail}") from exc


def _resolve_identity(
    source_root: Path, tag: str | None, commit_sha: str | None
) -> tuple[str, str]:
    resolved_tag = tag or os.environ.get("GITHUB_REF_NAME")
    if not resolved_tag:
        resolved_tag = _git(source_root, "describe", "--exact-match", "--tags", "HEAD")
    resolved_commit = (
        commit_sha or os.environ.get("GITHUB_SHA") or _git(source_root, "rev-parse", "HEAD")
    )
    return resolved_tag, resolved_commit


def _assert_tag_points_to_commit(source_root: Path, tag: str, commit_sha: str) -> None:
    # Pull requests use a synthetic tag for packaging validation.  A real tag,
    # however, must point at the exact source commit used to build the files.
    try:
        tagged_commit = _git(source_root, "rev-parse", f"{tag}^{{commit}}")
    except ReleaseError:
        return
    if tagged_commit != commit_sha:
        raise ReleaseError(f"tag {tag} points to {tagged_commit}, not build commit {commit_sha}")


def _provenance(version: str, tag: str, commit_sha: str) -> dict[str, str]:
    return {
        "version": version,
        "release": tag,
        "tag": tag,
        "commit_sha": commit_sha,
    }


def _stamp_manifest(
    manifest: dict[str, Any], provenance: dict[str, str], relative_path: Path
) -> None:
    """Stamp a supported manifest without changing its install-time shape."""

    if relative_path.name == "marketplace.json":
        metadata = manifest.setdefault("metadata", {})
        metadata.update(provenance)
        for plugin in manifest.get("plugins", []):
            if isinstance(plugin, dict):
                plugin.update(provenance)
                source = plugin.get("source")
                if isinstance(source, dict) and source.get("source") == "url":
                    source["ref"] = provenance["tag"]
                    source["commit_sha"] = provenance["commit_sha"]
    else:
        manifest.update(provenance)


def _load_and_stamp_manifest(path: Path, provenance: dict[str, str]) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid JSON manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise ReleaseError(f"manifest must be a JSON object: {path}")
    _stamp_manifest(manifest, provenance, path)
    return manifest


def _manifest_provenance(manifest: dict[str, Any], relative_path: Path) -> dict[str, str]:
    if relative_path.name == "marketplace.json":
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise ReleaseError(f"marketplace manifest has no metadata: {relative_path}")
        values = {key: metadata.get(key) for key in ("version", "release", "tag", "commit_sha")}
    else:
        values = {key: manifest.get(key) for key in ("version", "release", "tag", "commit_sha")}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ReleaseError(f"manifest is missing release provenance: {relative_path}")
    return values  # type: ignore[return-value]


def validate_bundle(bundle_root: Path, expected: dict[str, str]) -> None:
    """Fail if any generated manifest differs from the release identity."""

    values: list[dict[str, str]] = []
    for relative_path in MANIFESTS:
        path = bundle_root / relative_path
        if not path.is_file():
            raise ReleaseError(f"release bundle is missing manifest {relative_path}")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"invalid generated manifest: {relative_path}") from exc
        if not isinstance(manifest, dict):
            raise ReleaseError(f"generated manifest must be an object: {relative_path}")
        actual = _manifest_provenance(manifest, relative_path)
        if actual != expected:
            raise ReleaseError(
                f"manifest {relative_path} was not built from "
                f"{expected['commit_sha']} / {expected['tag']}: {actual}"
            )
        if relative_path.name == "marketplace.json":
            for plugin in manifest.get("plugins", []):
                if not isinstance(plugin, dict):
                    raise ReleaseError(
                        f"marketplace plugin entry is not an object: {relative_path}"
                    )
                plugin_identity = {
                    key: plugin.get(key) for key in ("version", "release", "tag", "commit_sha")
                }
                if plugin_identity != expected:
                    raise ReleaseError(
                        f"marketplace plugin entry in {relative_path} has different provenance: "
                        f"{plugin_identity}"
                    )
                source = plugin.get("source")
                if isinstance(source, dict) and source.get("source") == "url":
                    if (
                        source.get("ref") != expected["tag"]
                        or source.get("commit_sha") != expected["commit_sha"]
                    ):
                        raise ReleaseError(
                            f"marketplace source in {relative_path} does not point to "
                            "the release tag"
                        )
        values.append(actual)

    if not values or any(value != values[0] for value in values[1:]):
        raise ReleaseError("release manifests were assembled from different commits or tags")

    skill = bundle_root / "skills/hhru/SKILL.md"
    if not skill.is_file():
        raise ReleaseError("release bundle is missing skills/hhru/SKILL.md")
    skill_text = skill.read_text(encoding="utf-8")
    if (
        "--execution-mode foreground" not in skill_text
        or "--progress-verbosity 1" not in skill_text
    ):
        raise ReleaseError("release bundle does not contain the #660 foreground/heartbeat skill")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_release(source_root: Path, output_dir: Path, tag: str, commit_sha: str) -> Path:
    version = _read_version(source_root)
    _validate_release_identity(version, tag, commit_sha)
    _assert_tag_points_to_commit(source_root, tag, commit_sha)
    provenance = _provenance(version, tag, commit_sha)

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"hhru-cc-plugin-{version}.tar.gz"
    archive_path = output_dir / archive_name

    with tempfile.TemporaryDirectory(prefix="hhru-release-") as temporary:
        staging = Path(temporary) / f"hhru-cc-plugin-{version}"
        staging.mkdir()
        for relative_path in PLUGIN_FILES:
            source = source_root / relative_path
            destination = staging / relative_path
            if not source.exists():
                raise ReleaseError(f"release source is missing {relative_path}")
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        for relative_path in MANIFESTS:
            path = staging / relative_path
            _write_json(path, _load_and_stamp_manifest(path, provenance))
        _write_json(staging / RELEASE_FILE, {"product": "hhru", **provenance})
        validate_bundle(staging, provenance)

        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(staging, arcname=staging.name)

    metadata_path = output_dir / RELEASE_FILE
    _write_json(metadata_path, {"product": "hhru", **provenance, "plugin_archive": archive_name})
    return archive_path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tag", help="release tag; defaults to GITHUB_REF_NAME or the exact tag at HEAD"
    )
    parser.add_argument(
        "--commit", dest="commit_sha", help="full commit SHA; defaults to GITHUB_SHA or HEAD"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    source_root = args.source.resolve()
    try:
        tag, commit_sha = _resolve_identity(source_root, args.tag, args.commit_sha)
        archive = build_release(source_root, args.output.resolve(), tag, commit_sha)
    except ReleaseError as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 2
    print(f"release bundle: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
