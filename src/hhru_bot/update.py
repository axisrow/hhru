"""Keep the installed CLI and Codex plugin on one repository revision.

The marketplace snapshot is the release selector.  We refresh it first, take
the revision of the resulting checkout, and use that immutable revision for
the CLI installation and for the plugin verification.  In particular, a
successful ``pip install`` is not sufficient to report success: the plugin is
reinstalled when its cache is not the same revision and both sides are checked
before returning.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

DEFAULT_SOURCE = "https://github.com/axisrow/hhru.git"
DEFAULT_REF = "main"
MARKETPLACE = "hhru"
PLUGIN = "hhru-cc-plugin"
PACKAGE = "hhru-bot"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class UpdateError(RuntimeError):
    """An update did not reach a verified, consistent state."""


@dataclass(frozen=True)
class ReleaseIdentity:
    version: str
    commit: str
    source: str
    ref: str


@dataclass(frozen=True)
class UpdateResult:
    release: ReleaseIdentity
    cli_source: str
    plugin_source: str


@dataclass(frozen=True)
class _Completed:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _run(args: list[str], *, check: bool = True) -> _Completed:
    """Run an external lifecycle command with captured, deterministic output."""
    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise UpdateError(f"не удалось запустить {' '.join(args)}: {exc}") from exc
    result = _Completed(tuple(args), completed.returncode, completed.stdout, completed.stderr)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise UpdateError(
            f"команда {' '.join(args)!r} завершилась с кодом {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return result


def _json_output(text: str) -> dict:
    """Parse JSON even when Codex printed a warning before its JSON result."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise UpdateError(f"Codex вернул невалидный JSON: {text.strip()!r}")


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = _run(["git", "-C", str(path), *args], check=check)
    return result.stdout.strip()


def _git_commit(path: Path) -> str | None:
    commit = _git(path, "rev-parse", "HEAD", check=False)
    return commit if _SHA_RE.fullmatch(commit) else None


def _git_remote(path: Path) -> str:
    remote = _git(path, "remote", "get-url", "origin", check=False)
    if not remote:
        raise UpdateError(f"у checkout {path} нет remote origin")
    return remote


def _ensure_clean_checkout(path: Path) -> None:
    """Do not call an editable checkout provenance-safe while it is dirty."""
    result = _run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        check=False,
    )
    if result.returncode:
        raise UpdateError(f"не удалось проверить чистоту editable checkout: {path}")
    if result.stdout.strip():
        raise UpdateError(
            f"editable checkout {path} содержит незакоммиченные изменения; "
            "сначала сохраните или отмените их"
        )


def _same_repository(left: str, right: str) -> bool:
    """Compare common HTTPS/SSH spellings without weakening source checks."""

    def normalize(value: str) -> str:
        value = value.removesuffix(".git").rstrip("/")
        if value.startswith("git@"):
            value = value.replace(":", "/", 1).removeprefix("git@")
        value = value.removeprefix("https://").removeprefix("ssh://")
        return value.removeprefix("git@").lower()

    return normalize(left) == normalize(right)


def _repo_root() -> Path | None:
    """Return the checkout root for an editable install, if this is one."""
    candidate = Path(__file__).resolve().parents[2]
    return (
        candidate
        if (candidate / "pyproject.toml").is_file() and (candidate / ".git").exists()
        else None
    )


def _file_url_path(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def editable_root() -> Path | None:
    """Find the checkout behind an editable package installation."""
    root = _repo_root()
    if root is not None:
        return root
    try:
        direct_url = metadata.distribution(PACKAGE).read_text("direct_url.json")
    except (metadata.PackageNotFoundError, FileNotFoundError):
        return None
    if not direct_url:
        return None
    try:
        info = json.loads(direct_url)
    except json.JSONDecodeError:
        return None
    if not info.get("dir_info", {}).get("editable"):
        return None
    return _file_url_path(str(info.get("url", "")))


def _manifest_version(root: Path) -> str:
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject.get("project", {})
        version = project.get("version")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpdateError(
            f"не удалось прочитать версию в {root / 'pyproject.toml'}: {exc}"
        ) from exc

    # #673 may replace the literal project.version with a generated value.  The
    # fallback keeps this flow compatible with that change without making the
    # updater another source of truth.
    if not isinstance(version, str):
        try:
            source = (root / "src" / "hhru_bot" / "_version.py").read_text(encoding="utf-8")
        except OSError as exc:
            raise UpdateError("источник релиза не сообщает версию CLI") from exc
        match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", source)
        version = match.group(1) if match else None
    if not isinstance(version, str) or not version:
        raise UpdateError("источник релиза не сообщает версию CLI")

    try:
        plugin = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"не удалось прочитать manifest Codex plugin: {exc}") from exc
    plugin_version = plugin.get("version")
    if plugin_version != version:
        raise UpdateError(
            f"версия CLI {version!r} и plugin {plugin_version!r} расходятся в выбранном commit"
        )
    return version


def _plugin_commit(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    commit = _git_commit(path)
    if commit:
        return commit
    # A future Codex cache may not retain .git.  Release metadata is preferred
    # over a version-only comparison, because the latter was the original drift
    # bug (old and new components both reported 0.1.0).
    for name in (".hhru-release.json", "hhru-release.json", "release.json"):
        try:
            data = json.loads((path / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        commit = data.get("commit") or data.get("commit_sha")
        if isinstance(commit, str) and _SHA_RE.fullmatch(commit):
            return commit
    return None


def _tree_digest(path: Path) -> str:
    """Hash a checkout without its VCS internals for gitless Codex caches."""
    digest = hashlib.sha256()
    files = sorted(
        file
        for file in path.rglob("*")
        if file.is_file() and ".git" not in file.relative_to(path).parts
    )
    for file in files:
        relative = file.relative_to(path).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(file.read_bytes())
    return digest.hexdigest()


def _revision_tree_digest(source_root: Path, release: ReleaseIdentity) -> str:
    """Hash the plugin source revision, even when it differs from marketplace HEAD."""
    available = _run(
        ["git", "-C", str(source_root), "cat-file", "-e", f"{release.commit}^{{commit}}"],
        check=False,
    )
    if available.returncode:
        _run(["git", "-C", str(source_root), "fetch", "--quiet", release.source, release.commit])
    try:
        archive = subprocess.run(
            ["git", "-C", str(source_root), "archive", release.commit],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UpdateError(
            f"не удалось прочитать содержимое commit plugin {release.commit}"
        ) from exc
    with tempfile.TemporaryDirectory(prefix="hhru-plugin-source-") as directory:
        extracted = Path(directory)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
                bundle.extractall(extracted, filter="data")
        except (OSError, tarfile.TarError) as exc:
            raise UpdateError("не удалось распаковать source revision plugin") from exc
        return _tree_digest(extracted)


def _verified_plugin_commit(
    path: Path | None,
    source_root: Path,
    expected: str,
    source_digest: str | None = None,
) -> str | None:
    commit = _plugin_commit(path)
    if path is None or not path.exists():
        return None
    try:
        content_matches = _tree_digest(path) == (source_digest or _tree_digest(source_root))
    except OSError as exc:
        raise UpdateError(f"не удалось проверить содержимое plugin cache: {path}") from exc
    if not content_matches:
        return None
    if commit is None or commit == expected:
        return expected
    return None


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def _configured_marketplace_ref() -> str | None:
    config_path = _codex_home() / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    marketplace = config.get("marketplaces", {}).get(MARKETPLACE, {})
    ref = marketplace.get("ref")
    return ref if isinstance(ref, str) else None


def _github_repository(source: str) -> str | None:
    """Extract ``owner/repository`` from a GitHub remote URL."""
    if source.startswith("git@github.com:"):
        path = source.removeprefix("git@github.com:")
    else:
        parsed = urlparse(source)
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    return "/".join(parts) if len(parts) == 2 and all(parts) else None


def _latest_release_ref(source: str) -> str | None:
    """Return the highest published ``vX.Y.Z`` release tag.

    Git tags can exist before their release workflow passes its publish gate.
    GitHub's releases API is therefore the authority here, rather than
    ``git ls-remote --tags``.
    """
    repository = _github_repository(source)
    if repository is None:
        return None
    request = Request(
        f"https://api.github.com/repos/{repository}/releases?per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "hhru-update",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            releases = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"не удалось получить опубликованные релизы {repository}: {exc}") from exc
    if not isinstance(releases, list):
        raise UpdateError(f"GitHub вернул неожиданный список релизов {repository}")

    refs: list[tuple[tuple[int, ...], str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease") or not release.get("published_at"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        match = re.fullmatch(r"v(\d+)(?:\.(\d+)){0,2}", tag)
        if match:
            refs.append((tuple(int(part) for part in tag[1:].split(".")), tag))
    return max(refs)[1] if refs else None


def _marketplace_plugin_source(root: Path) -> tuple[str, str, str]:
    """Read the exact source ref selected by the Codex marketplace manifest."""
    try:
        marketplace = json.loads(
            (root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
        )
        plugin = next(item for item in marketplace["plugins"] if item.get("name") == PLUGIN)
        source = plugin["source"]
        url = source["url"]
        ref = source["ref"]
        version = plugin["version"]
    except (KeyError, OSError, StopIteration, TypeError, json.JSONDecodeError) as exc:
        raise UpdateError("marketplace не сообщает source/ref/version hhru plugin") from exc
    if not all(isinstance(value, str) and value for value in (url, ref, version)):
        raise UpdateError("marketplace содержит неполный source/ref/version hhru plugin")
    return url, ref, version


def _resolve_ref_commit(source_root: Path, source: str, ref: str) -> str:
    if _SHA_RE.fullmatch(ref):
        return ref
    local_source = _git_remote(source_root)
    if _same_repository(local_source, source):
        local = _git(source_root, "rev-parse", f"{ref}^{{commit}}", check=False)
        if _SHA_RE.fullmatch(local):
            return local
    result = _run(
        [
            "git",
            "ls-remote",
            source,
            ref,
            f"refs/heads/{ref}",
            f"refs/tags/{ref}",
            f"refs/tags/{ref}^{{}}",
        ]
    )
    lines = result.stdout.splitlines()
    for line in lines:
        commit, _separator, remote_ref = line.partition("\t")
        if remote_ref.endswith("^{}") and _SHA_RE.fullmatch(commit):
            return commit
    for line in lines:
        commit, _separator, _remote_ref = line.partition("\t")
        if _SHA_RE.fullmatch(commit):
            return commit
    raise UpdateError(f"не удалось разрешить ref {ref!r} источника plugin")


def _installed_plugin(result: dict) -> dict | None:
    return next(
        (
            item
            for item in result.get("installed", [])
            if item.get("name") == PLUGIN and item.get("marketplaceName") == MARKETPLACE
        ),
        None,
    )


def _cache_path(record: dict | None) -> Path | None:
    if not record:
        return None
    version = record.get("version")
    if not isinstance(version, str):
        return None
    root = _codex_home() / "plugins" / "cache" / MARKETPLACE / PLUGIN / version
    return root if root.exists() else None


def _ensure_marketplace(codex: str, source: str, ref: str) -> None:
    listed = _run([codex, "plugin", "marketplace", "list"], check=False)
    if listed.returncode == 0 and any(
        line.strip().startswith(f"{MARKETPLACE} ") for line in listed.stdout.splitlines()
    ):
        configured_ref = _configured_marketplace_ref()
        if configured_ref and configured_ref != ref:
            _run([codex, "plugin", "marketplace", "remove", MARKETPLACE, "--json"])
        else:
            return
    _run(
        [
            codex,
            "plugin",
            "marketplace",
            "add",
            source,
            "--ref",
            ref,
            "--json",
        ]
    )


def _select_release(codex: str, source: str, editable: Path | None) -> tuple[ReleaseIdentity, Path]:
    configured_ref = _configured_marketplace_ref()
    selected_ref = _latest_release_ref(source) or configured_ref or DEFAULT_REF
    _ensure_marketplace(codex, source, selected_ref)
    upgraded = _run([codex, "plugin", "marketplace", "upgrade", MARKETPLACE, "--json"])
    payload = _json_output(upgraded.stdout)
    errors = payload.get("errors")
    if errors:
        raise UpdateError(f"обновление marketplace не завершено: {errors}")
    roots = payload.get("upgradedRoots")
    if not isinstance(roots, list) or len(roots) != 1:
        raise UpdateError("Codex не сообщил checkout обновлённого marketplace")
    root = Path(str(roots[0])).expanduser()
    commit = _git_commit(root)
    if not commit:
        raise UpdateError(f"невозможно доказать commit marketplace: {root}")
    actual_source = _git_remote(root)
    if not _same_repository(actual_source, source):
        raise UpdateError(
            f"marketplace hhru указывает на неожиданный источник {actual_source!r}; "
            f"ожидался {source!r}"
        )
    plugin_source, plugin_ref, version = _marketplace_plugin_source(root)
    plugin_commit = _resolve_ref_commit(root, plugin_source, plugin_ref)
    if editable is not None and _git_commit(editable) != plugin_commit:
        raise UpdateError(
            "editable CLI и marketplace plugin выбрали разные commit; выполните git pull "
            "и повторите hhru update"
        )
    return ReleaseIdentity(version, plugin_commit, plugin_source, plugin_ref), root


def _install_cli(release: ReleaseIdentity, editable: Path | None) -> str:
    if editable is not None:
        if _git_commit(editable) != release.commit:
            raise UpdateError("editable CLI не соответствует выбранному commit")
        return f"editable:{editable}"
    requirement = f"git+{release.source}@{release.commit}"
    _run([sys.executable, "-m", "pip", "install", "--upgrade", requirement])
    return requirement


def _verify_cli(release: ReleaseIdentity, editable: Path | None) -> None:
    if editable is not None:
        editable_version = _manifest_version(editable)
        if editable_version != release.version:
            raise UpdateError(
                f"editable CLI сообщает версию {editable_version!r}, ожидалась {release.version!r}"
            )
        if _git_commit(editable) == release.commit:
            return
        raise UpdateError(f"editable CLI не соответствует commit {release.commit}")
    try:
        installed_version = metadata.version(PACKAGE)
        direct_url = metadata.distribution(PACKAGE).read_text("direct_url.json")
    except metadata.PackageNotFoundError as exc:
        raise UpdateError("после обновления пакет CLI не найден") from exc
    if installed_version != release.version:
        raise UpdateError(
            f"CLI сообщает версию {installed_version!r}, ожидалась {release.version!r}"
        )
    try:
        info = json.loads(direct_url) if direct_url else {}
    except json.JSONDecodeError as exc:
        raise UpdateError("CLI provenance содержит невалидный direct_url.json") from exc
    vcs_commit = info.get("vcs_info", {}).get("commit_id")
    if isinstance(vcs_commit, str):
        if vcs_commit != release.commit:
            raise UpdateError(f"CLI установлен из commit {vcs_commit}, ожидался {release.commit}")
        return
    root = editable or _file_url_path(str(info.get("url", "")))
    if root is not None and _git_commit(root) == release.commit:
        return
    raise UpdateError("CLI обновлён, но его commit provenance не удалось подтвердить")


def _update_plugin(
    codex: str,
    release: ReleaseIdentity,
    source_root: Path,
    source_digest: str | None = None,
) -> str:
    listed = _run([codex, "plugin", "list", "--marketplace", MARKETPLACE, "--json"])
    record = _installed_plugin(_json_output(listed.stdout))
    path = _cache_path(record)
    if _verified_plugin_commit(path, source_root, release.commit, source_digest) != release.commit:
        if record is not None:
            _run([codex, "plugin", "remove", f"{PLUGIN}@{MARKETPLACE}", "--json"])
        installed = _run([codex, "plugin", "add", f"{PLUGIN}@{MARKETPLACE}", "--json"])
        payload = _json_output(installed.stdout)
        path = Path(str(payload.get("installedPath", ""))).expanduser()
        if not path.exists():
            raise UpdateError("Codex не сообщил существующий путь установленного plugin")
    if _verified_plugin_commit(path, source_root, release.commit, source_digest) != release.commit:
        raise UpdateError(
            f"plugin cache не соответствует commit {release.commit}; повторите hhru update"
        )
    return f"{path} @ {release.commit}"


def update(*, codex: str = "codex") -> UpdateResult:
    """Update and verify CLI plus Codex plugin as one operation."""
    editable = editable_root()
    if editable is not None:
        _ensure_clean_checkout(editable)
    source = _git_remote(editable) if editable is not None else DEFAULT_SOURCE
    release, marketplace_root = _select_release(codex, source, editable)
    cli_source = _install_cli(release, editable)
    _verify_cli(release, editable)
    plugin_digest = _revision_tree_digest(marketplace_root, release)
    plugin_source = _update_plugin(codex, release, marketplace_root, plugin_digest)
    return UpdateResult(release, cli_source, plugin_source)
