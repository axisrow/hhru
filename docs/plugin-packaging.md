# Plugin packaging and release metadata

## One version source

`pyproject.toml` (`[project].version`) is the only version source. The four
JSON files are generated artifacts for their respective consumers:

```bash
python3 scripts/sync_plugin_manifests.py
python3 scripts/sync_plugin_manifests.py --check
```

The second command is a CI guard and must fail when a manifest is edited to a
different version. It updates these fields:

- `.codex-plugin/plugin.json:version`;
- `.claude-plugin/plugin.json:version`;
- `.claude-plugin/marketplace.json:metadata.version` and
  `plugins[0].version`;
- `.agents/plugins/marketplace.json:metadata.version` and
  `plugins[0].version`.

## Release refs

The Codex marketplace uses the release model introduced by #676 and merged in
#681: its Git source must point to the version tag (`v` plus
`pyproject.toml`'s version), rather than to a floating branch. CI runs
`python3 scripts/check_plugin_refs.py`, which resolves every URL source with
`git ls-remote` and fails if the referenced tag is absent. The check runs on
pushes (including the tag-triggered release workflow), not on pull requests:
the first release therefore requires publishing `v0.1.0` after its release
commit is selected, and subsequent version bumps require a corresponding tag
before the resulting push can pass CI.

The marketplace files are both needed. `.claude-plugin/marketplace.json` is
the Claude Code local marketplace entry (`source: "./"`), while
`.agents/plugins/marketplace.json` is the Codex team marketplace entry (a Git
URL, ref, and installation/authentication policy). They intentionally have
different schemas and are not interchangeable; only their shared release
metadata is generated.

## Skill path and provenance

`.codex-plugin/plugin.json` is metadata inside the plugin root. Codex copies
the repository as the plugin root and resolves `skills: "./skills/"` from that
root, so the path points to the checked-in top-level `skills/` directory; a
`.codex-plugin/skills/` directory is neither required nor expected.

Diagnostics include `environment.hhru.version` and
`environment.hhru.commit_sha` together. The SHA is resolved from the actual
checkout (or the package-specific `HHRU_COMMIT_SHA` supplied by a build),
rather than being copied into a tracked manifest. A consuming workflow's
ambient `GITHUB_SHA` is intentionally ignored because it identifies the
consumer repository, not necessarily hhru. This avoids a self-referential hash
in a floating branch manifest and identifies the exact installed source state.
