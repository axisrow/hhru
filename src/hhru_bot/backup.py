"""Safe, portable backups of one local hhru data directory."""

from __future__ import annotations

import os
import sqlite3
import stat
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from shutil import copyfile

import yaml


class BackupError(ValueError):
    """The backup archive or destination is not safe to use."""


def _root(config: Path, history: Path) -> Path:
    if config.parent != history.parent:
        raise BackupError("config и history должны находиться в одной директории")
    return config.parent


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def _configured_storage_from_raw(config: Path, root: Path, raw: object) -> Path | None:
    if not isinstance(raw, dict):
        return None
    account = raw.get("account", {})
    if not isinstance(account, dict):
        return None
    value = account.get("storage_state_file")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BackupError("Некорректный account.storage_state_file")
    path = (config.parent / value).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise BackupError("storage_state_file выходит за пределы data") from exc
    return path


def _configured_storage_path(config: Path, root: Path) -> Path | None:
    try:
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BackupError(f"Не удалось прочитать конфиг для backup: {config}") from exc
    return _configured_storage_from_raw(config, root, raw)


def _storage_archive_name(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return f"storage_state/{path.name}"
    if relative.parts and relative.parts[0] == "storage_state":
        return relative.as_posix()
    return f"storage_state/{path.name}"


def create_backup(
    config: str | Path,
    history: str | Path,
    output: str | Path,
    *,
    require_config: bool = True,
) -> Path:
    """Create a gzip tar archive with config, session state and a consistent DB."""
    config, history, output = Path(config), Path(history), Path(output)
    root = _root(config, history)
    configured_storage = _configured_storage_path(config, root) if config.is_file() else None
    output_resolved = output.resolve()
    managed = {
        config.resolve(),
        history.resolve(),
        *(
            history.with_name(history.name + suffix).resolve()
            for suffix in ("-wal", "-shm", "-journal")
        ),
    }
    if configured_storage is not None:
        managed.add(configured_storage)
    storage = (root / "storage_state").resolve()
    output_key = str(output_resolved).casefold()
    managed_keys = {str(path).casefold() for path in managed}
    storage_key = str(storage).casefold()
    if (
        output_key in managed_keys
        or output_key == storage_key
        or output_key.startswith(storage_key + os.sep)
    ):
        raise BackupError("Путь архива совпадает с управляемым файлом состояния")
    if require_config and not config.is_file():
        raise BackupError(f"Файл конфига не найден: {config}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hhru-backup-") as tmp:
        snapshot = Path(tmp) / "history.db"
        if history.exists():
            _sqlite_snapshot(history, snapshot)
        fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        temporary = Path(temp_name)
        try:
            os.chmod(fd, 0o600)
            stream = os.fdopen(fd, "wb")
            fd = -1
            with stream:
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    if config.is_file():
                        archive.add(config, arcname="config.yaml", recursive=False)
                    elif not require_config:
                        archive.addfile(tarfile.TarInfo("config.missing"))
                    if snapshot.exists():
                        archive.add(snapshot, arcname="history.db", recursive=False)
                    storage_dir = root / "storage_state"
                    included: set[Path] = set()
                    included_names: set[str] = set()
                    if storage_dir.is_dir() and not storage_dir.is_symlink():
                        for item in sorted(storage_dir.rglob("*")):
                            if item.is_file() and not item.is_symlink():
                                included.add(item.resolve())
                                included_names.add(item.relative_to(root).as_posix())
                                archive.add(
                                    item, arcname=item.relative_to(root).as_posix(), recursive=False
                                )
                    if (
                        configured_storage is not None
                        and configured_storage.is_file()
                        and configured_storage not in included
                    ):
                        canonical_name = _storage_archive_name(configured_storage, root)
                        if canonical_name in included_names:
                            raise BackupError("Имя настроенной сессии конфликтует с storage_state")
                        archive.add(
                            configured_storage,
                            arcname=canonical_name,
                            recursive=False,
                        )
            os.replace(temporary, output)
        finally:
            if fd != -1:
                os.close(fd)
            temporary.unlink(missing_ok=True)
    return output


def _member_name(member: tarfile.TarInfo) -> str:
    name = member.name
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise BackupError(f"Небезопасный путь в архиве: {name!r}")
    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
        raise BackupError(f"Недопустимый тип записи в архиве: {name!r}")
    if name in {"config.yaml", "history.db"} and member.isdir():
        raise BackupError(f"Ожидался файл, получена директория: {name!r}")
    if name not in {"config.yaml", "config.missing", "history.db"} and not name.startswith(
        "storage_state/"
    ):
        raise BackupError(f"Недопустимый файл в архиве: {name!r}")
    return name


def inspect_backup(archive_path: str | Path) -> list[str]:
    with tarfile.open(archive_path, "r:*") as archive:
        names = [_member_name(member) for member in archive.getmembers()]
    if len(names) != len(set(names)):
        raise BackupError("Архив содержит повторяющиеся записи")
    if "config.yaml" not in names and "config.missing" not in names:
        raise BackupError("В архиве отсутствует config.yaml")
    return names


def restore_backup(
    archive_path: str | Path,
    config: str | Path,
    history: str | Path,
    *,
    dry_run: bool = True,
) -> list[str]:
    """Validate and restore an archive; dry-run is the safe default."""
    archive_path, config, history = Path(archive_path), Path(config), Path(history)
    root = _root(config, history).resolve()
    config, history = config.resolve(), history.resolve()
    previous_storage = _configured_storage_path(config, root) if config.exists() else None

    names = inspect_backup(archive_path)
    if dry_run:
        return names
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rollback = root / f".before-restore-{stamp}.tar.gz"
    if config.exists() or history.exists() or (root / "storage_state").is_dir():
        create_backup(config, history, rollback, require_config=False)
    with tempfile.TemporaryDirectory(prefix="hhru-restore-", dir=root.parent) as tmp:
        staging = Path(tmp) / root.name
        staging.mkdir()
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive.getmembers():
                name = _member_name(member)
                if member.isdir() or name == "config.missing":
                    continue
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None
                target.write_bytes(source.read())
                os.chmod(target, 0o600)
        desired_storage = None
        staged_config = staging / "config.yaml"
        if staged_config.exists():
            try:
                raw = yaml.safe_load(staged_config.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise BackupError("Некорректный config.yaml в архиве") from exc
            desired_storage = _configured_storage_from_raw(config, root, raw)

        storage_targets = {
            _storage_archive_name(path, root): path
            for path in (previous_storage, desired_storage)
            if path is not None
        }

        def target_for(name: str) -> Path:
            if name == "config.yaml":
                return config
            if name == "history.db":
                return history
            return storage_targets.get(name, root / name)

        staged_history = staging / "history.db"
        if staged_history.exists():
            # Re-materialize via SQLite's backup API instead of replacing an
            # archive payload blindly, and reject malformed databases.
            with tempfile.NamedTemporaryFile(dir=tmp, suffix=".db") as checked:
                checked_path = Path(checked.name)
            _sqlite_snapshot(staged_history, checked_path)
            checked_path.replace(staged_history)
            os.chmod(staged_history, 0o600)
        originals = Path(tmp) / "originals"
        replaced: list[Path] = []
        original_keys: dict[Path, Path] = {}
        try:
            for name in names:
                source = staging / name
                target = target_for(name)
                try:
                    target.resolve(strict=False).relative_to(root.resolve())
                except ValueError as exc:
                    raise BackupError(f"Путь назначения выходит за пределы data: {name!r}") from exc
                if not source.is_file():
                    continue
                # Never follow a pre-existing symlink while constructing the
                # destination path.  os.replace itself is atomic for each file.
                parent = target.parent
                while parent != root:
                    if parent.is_symlink():
                        raise BackupError(f"Каталог назначения является symlink: {parent}")
                    parent = parent.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    saved = originals / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    if target.is_symlink() or not target.is_file():
                        raise BackupError(f"Файл назначения имеет небезопасный тип: {target}")
                    copyfile(target, saved)
                    os.chmod(saved, stat.S_IMODE(target.stat().st_mode))
                    original_keys[target] = saved
                source.replace(target)
                replaced.append(target)
            archived = set(names)
            managed = {"config.yaml", "history.db"}
            storage = root / "storage_state"
            if storage.is_dir() and not storage.is_symlink():
                managed.update(
                    item.relative_to(root).as_posix()
                    for item in storage.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
            if (
                previous_storage is not None
                and desired_storage is not None
                and previous_storage != desired_storage
                and previous_storage.is_file()
            ):
                saved = originals / "previous-configured-session"
                copyfile(previous_storage, saved)
                os.chmod(saved, stat.S_IMODE(previous_storage.stat().st_mode))
                original_keys[previous_storage] = saved
                previous_storage.unlink()
                replaced.append(previous_storage)
            for configured_storage in (previous_storage, desired_storage):
                if configured_storage is not None:
                    managed.add(_storage_archive_name(configured_storage, root))
            for name in sorted(managed - archived):
                target = target_for(name)
                if not target.exists() and not target.is_symlink():
                    continue
                if target.is_symlink() or not target.is_file():
                    raise BackupError(f"Файл назначения имеет небезопасный тип: {target}")
                saved = originals / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                copyfile(target, saved)
                os.chmod(saved, stat.S_IMODE(target.stat().st_mode))
                original_keys[target] = saved
                target.unlink()
                replaced.append(target)
        except Exception:
            # A multi-file restore cannot rename one directory without also
            # replacing unrelated logs. Roll back every file already replaced.
            for target in reversed(replaced):
                saved = original_keys.get(target, originals / target.relative_to(root))
                if saved.exists():
                    saved.replace(target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return names
