"""Release/tag packaging invariants."""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.build_release import ReleaseError, build_release, validate_bundle  # noqa: E402

pytestmark = pytest.mark.smoke

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_release_bundle_stamps_every_manifest_and_contains_installed_skill(tmp_path):
    archive = build_release(Path(__file__).parents[1], tmp_path, "v0.1.0", COMMIT)

    with tarfile.open(archive, "r:gz") as opened:
        opened.extractall(tmp_path / "installed", filter="data")
    bundle = next((tmp_path / "installed").iterdir())
    expected = {
        "version": "0.1.0",
        "release": "v0.1.0",
        "tag": "v0.1.0",
        "commit_sha": COMMIT,
    }

    plugin = json.loads((bundle / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert {key: plugin[key] for key in expected} == expected
    assert (bundle / "skills/hhru/SKILL.md").is_file()
    assert "--progress-verbosity 1" in (bundle / "skills/hhru/SKILL.md").read_text(encoding="utf-8")
    assert json.loads((bundle / "release.json").read_text(encoding="utf-8")) == {
        "product": "hhru",
        **expected,
    }


def test_release_validation_rejects_manifest_from_another_commit(tmp_path):
    archive = build_release(Path(__file__).parents[1], tmp_path, "v0.1.0", COMMIT)
    with tarfile.open(archive, "r:gz") as opened:
        opened.extractall(tmp_path / "installed", filter="data")
    bundle = next((tmp_path / "installed").iterdir())
    path = bundle / ".codex-plugin/plugin.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["commit_sha"] = "fedcba9876543210fedcba9876543210fedcba98"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReleaseError, match="was not built"):
        validate_bundle(
            bundle,
            {
                "version": "0.1.0",
                "release": "v0.1.0",
                "tag": "v0.1.0",
                "commit_sha": COMMIT,
            },
        )


def test_release_tag_must_match_canonical_package_version(tmp_path):
    with pytest.raises(ReleaseError, match="does not match project version"):
        build_release(Path(__file__).parents[1], tmp_path, "v9.9.9", COMMIT)
