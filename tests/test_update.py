"""Contract tests for the single-command CLI/plugin lifecycle (#675)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hhru_bot import update as update_module

pytestmark = pytest.mark.integration

COMMIT = "a" * 40
OLD_COMMIT = "b" * 40


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "marketplace"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "src" / "hhru_bot").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    (root / ".codex-plugin" / "plugin.json").write_text(
        '{"name": "hhru-cc-plugin", "version": "1.2.3"}', encoding="utf-8"
    )
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".agents" / "plugins" / "marketplace.json").write_text(
        '{"plugins": [{"name": "hhru-cc-plugin", "version": "1.2.3", '
        '"source": {"url": "https://example.test/hhru.git", "ref": "main"}}]}',
        encoding="utf-8",
    )
    return root


def _patch_common(monkeypatch, root: Path, *, editable=None):
    monkeypatch.setattr(update_module, "DEFAULT_SOURCE", "https://example.test/hhru.git")
    codex_home = root.parent / ".codex"
    cache = codex_home / "plugins" / "cache" / "hhru" / "hhru-cc-plugin" / "1.2.3"
    cache.mkdir(parents=True)
    monkeypatch.setattr(update_module, "_codex_home", lambda: codex_home)
    monkeypatch.setattr(update_module, "editable_root", lambda: editable)
    monkeypatch.setattr(update_module, "_git_commit", lambda _path: COMMIT)
    monkeypatch.setattr(update_module, "_git_remote", lambda _path: "https://example.test/hhru.git")
    monkeypatch.setattr(update_module, "_install_cli", lambda release, _editable: "test-cli")
    monkeypatch.setattr(update_module, "_verify_cli", lambda _release, _editable: None)
    monkeypatch.setattr(update_module, "_plugin_commit", lambda path: COMMIT if path else None)
    monkeypatch.setattr(update_module, "_tree_digest", lambda _path: "same")
    monkeypatch.setattr(update_module, "_latest_release_ref", lambda _source: None)
    monkeypatch.setattr(update_module, "_resolve_ref_commit", lambda *_args: COMMIT)
    monkeypatch.setattr(update_module, "_revision_tree_digest", lambda *_args: "same")

    state = {"installed": False}

    def run(args, *, check=True):
        command = tuple(args)
        if command[1:4] == ("plugin", "marketplace", "list"):
            return update_module._Completed(tuple(args), 0, "", "")
        if command[1:5] == ("plugin", "marketplace", "add", "https://example.test/hhru.git"):
            return update_module._Completed(tuple(args), 0, '{"marketplaceName":"hhru"}', "")
        if command[1:5] == ("plugin", "marketplace", "upgrade", "hhru"):
            payload = {"upgradedRoots": [str(root)], "errors": []}
            return update_module._Completed(tuple(args), 0, json.dumps(payload), "")
        if command[1:3] == ("plugin", "list"):
            installed = (
                '[{"name":"hhru-cc-plugin","marketplaceName":"hhru","version":"1.2.3"}]'
                if state["installed"]
                else "[]"
            )
            return update_module._Completed(tuple(args), 0, f'{{"installed":{installed}}}', "")
        if command[1:3] == ("plugin", "add"):
            state["installed"] = True
            return update_module._Completed(
                tuple(args), 0, json.dumps({"installedPath": str(cache)}), ""
            )
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(update_module, "_run", run)


def test_latest_release_uses_published_releases_not_tags(monkeypatch):
    payload = [
        {"tag_name": "v9.9.9", "draft": True, "prerelease": False, "published_at": None},
        {"tag_name": "v1.2.0", "draft": False, "prerelease": False, "published_at": "now"},
        {"tag_name": "v1.1.0", "draft": False, "prerelease": True, "published_at": None},
    ]

    class Response(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        update_module, "urlopen", lambda *_args, **_kwargs: Response(json.dumps(payload))
    )

    assert update_module._latest_release_ref("https://github.com/axisrow/hhru.git") == "v1.2.0"


def test_published_release_uses_immutable_asset_commit(monkeypatch):
    commit = "e" * 40

    class Response(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    responses = iter(
        [
            Response(
                json.dumps(
                    {
                        "tag_name": "v1.2.0",
                        "assets": [
                            {"name": "release.json", "browser_download_url": "https://asset"}
                        ],
                    }
                )
            ),
            Response(json.dumps({"tag": "v1.2.0", "commit_sha": commit})),
        ]
    )
    monkeypatch.setattr(update_module, "urlopen", lambda *_args, **_kwargs: next(responses))

    assert (
        update_module._published_release_commit("https://github.com/axisrow/hhru.git", "v1.2.0")
        == commit
    )


def test_resolve_ref_commit_peels_annotated_tag(monkeypatch, tmp_path):
    annotated = "c" * 40
    peeled = "d" * 40
    monkeypatch.setattr(update_module, "_git_remote", lambda _path: "https://other.test/hhru.git")
    monkeypatch.setattr(
        update_module,
        "_run",
        lambda _args: update_module._Completed(
            (), 0, f"{annotated}\trefs/tags/v1.2.0\n{peeled}\trefs/tags/v1.2.0^{{}}\n", ""
        ),
    )

    assert (
        update_module._resolve_ref_commit(tmp_path, "https://example.test/hhru.git", "v1.2.0")
        == peeled
    )


def test_fresh_install_updates_both_components_from_one_commit(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    _patch_common(monkeypatch, root)

    result = update_module.update()

    assert result.release == update_module.ReleaseIdentity(
        "1.2.3", COMMIT, "https://example.test/hhru.git", "main"
    )
    assert result.cli_source == "test-cli"
    assert result.plugin_source.endswith(f"@ {COMMIT}")


def test_upgrade_reuses_the_same_provenance_on_second_run(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    _patch_common(monkeypatch, root)
    calls: list[tuple[str, ...]] = []
    original_run = update_module._run

    def recording_run(args, *, check=True):
        calls.append(tuple(args))
        return original_run(args, check=check)

    monkeypatch.setattr(update_module, "_run", recording_run)
    first = update_module.update()
    second = update_module.update()

    assert first.release.commit == second.release.commit == COMMIT
    assert sum(call[1:3] == ("plugin", "add") for call in calls) == 1
    assert not any(call[1:3] == ("plugin", "remove") for call in calls)


def test_github_fallback_does_not_require_release_asset(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    _patch_common(monkeypatch, root)
    monkeypatch.setattr(update_module, "DEFAULT_SOURCE", "https://github.com/axisrow/hhru.git")
    monkeypatch.setattr(update_module, "_git_remote", lambda _path: update_module.DEFAULT_SOURCE)
    monkeypatch.setattr(update_module, "_ensure_marketplace", lambda *_args: None)

    def fail_if_queried(*_args):
        raise AssertionError("fallback branch must not request release.json")

    monkeypatch.setattr(update_module, "_published_release_commit", fail_if_queried)

    result = update_module.update()

    assert result.release.commit == COMMIT


def test_editable_install_does_not_replace_checkout_with_wheel(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    monkeypatch.setattr(update_module, "_git_commit", lambda _path: COMMIT)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("editable update must not invoke pip")

    monkeypatch.setattr(update_module, "_run", fail_if_called)
    release = update_module.ReleaseIdentity("1.2.3", COMMIT, "url", "main")

    assert update_module._install_cli(release, checkout) == f"editable:{checkout}"


def test_dirty_editable_checkout_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_module,
        "_run",
        lambda _args, **_kwargs: update_module._Completed((), 0, " M local.py\n", ""),
    )

    with pytest.raises(update_module.UpdateError, match="незакоммиченные изменения"):
        update_module._ensure_clean_checkout(tmp_path)


def test_gitless_cache_can_be_verified_against_selected_marketplace(tmp_path, monkeypatch):
    source = tmp_path / "source"
    cache = tmp_path / "cache"
    source.mkdir()
    cache.mkdir()
    (source / "skills.txt").write_text("same", encoding="utf-8")
    (cache / "skills.txt").write_text("same", encoding="utf-8")
    monkeypatch.setattr(update_module, "_plugin_commit", lambda _path: None)

    assert update_module._verified_plugin_commit(cache, source, COMMIT) == COMMIT


def test_plugin_failure_is_an_explicit_update_error(tmp_path, monkeypatch):
    path = tmp_path / "cached-plugin"
    path.mkdir()
    monkeypatch.setattr(update_module, "_codex_home", lambda: tmp_path / ".codex")
    monkeypatch.setattr(update_module, "_plugin_commit", lambda _path: OLD_COMMIT)

    def run(args, *, check=True):
        command = tuple(args)
        if command[1:4] == ("plugin", "list", "--marketplace"):
            payload = {
                "installed": [
                    {"name": "hhru-cc-plugin", "marketplaceName": "hhru", "version": "1.2.3"}
                ]
            }
            return update_module._Completed(tuple(args), 0, json.dumps(payload), "")
        if command[1:3] == ("plugin", "remove"):
            return update_module._Completed(tuple(args), 0, "{}", "")
        raise update_module.UpdateError("Codex plugin install failed")

    monkeypatch.setattr(update_module, "_run", run)
    release = update_module.ReleaseIdentity("1.2.3", COMMIT, "url", "main")

    with pytest.raises(update_module.UpdateError, match="plugin install failed"):
        update_module._update_plugin("codex", release, path)


def test_modified_cache_is_not_accepted_for_matching_git_commit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    cache = tmp_path / "cache"
    source.mkdir()
    cache.mkdir()
    (source / "skills.txt").write_text("expected", encoding="utf-8")
    (cache / "skills.txt").write_text("injected", encoding="utf-8")
    monkeypatch.setattr(update_module, "_plugin_commit", lambda _path: COMMIT)

    assert update_module._verified_plugin_commit(cache, source, COMMIT) is None


def test_command_reports_partial_failure_without_success(monkeypatch, capsys):
    from hhru_bot.commands import update as command

    monkeypatch.setattr(
        command,
        "update",
        lambda **_kwargs: (_ for _ in ()).throw(update_module.UpdateError("plugin failed")),
    )

    assert command.run(SimpleNamespace(codex="codex")) is True
    output = capsys.readouterr().out
    assert "[FAIL]" in output
    assert "повторите `hhru update`" in output
    assert "[OK]" not in output


def test_windows_launcher_reexecs_through_python_before_update(monkeypatch):
    from hhru_bot.commands import update as command

    monkeypatch.setattr(command.os, "name", "nt")
    monkeypatch.delenv(command._WINDOWS_REEXEC_ENV, raising=False)
    monkeypatch.setattr(command.sys, "executable", r"C:\Venv\python.exe")
    monkeypatch.setattr(command.sys, "_base_executable", r"C:\Python\python.exe", raising=False)
    monkeypatch.setattr(command.sys, "argv", [r"C:\Scripts\hhru.exe", "update", "--codex", "codex"])
    called = {}

    def fake_execve(interpreter, argv, environment):
        called.update(interpreter=interpreter, argv=argv, environment=environment)

    monkeypatch.setattr(command.os, "execve", fake_execve)

    assert command._reexec_windows_launcher()
    assert called["interpreter"] == r"C:\Venv\python.exe"
    assert called["argv"][2:] == ["hhru_bot.cli", "update", "--codex", "codex"]
    assert called["environment"][command._WINDOWS_REEXEC_ENV] == "1"
