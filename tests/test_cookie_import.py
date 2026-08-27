from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hhru_bot.cookie_import import (
    build_storage_state,
    chrome_expires_to_playwright,
    chrome_samesite_to_playwright,
    resolve_chrome_profile,
    write_storage_state,
)

pytestmark = pytest.mark.integration


def row(**overrides):
    value = {
        "host_key": ".hh.ru",
        "name": "hhtoken",
        "value": "secret",
        "path": "/",
        "expires_utc": 0,
        "is_httponly": 1,
        "is_secure": 1,
        "samesite": -1,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("source", "expected"),
    [(0, -1), (11_644_473_600_000_000, 0), (11_644_473_601_000_000, 1)],
)
def test_chrome_expiration_conversion(source, expected):
    assert chrome_expires_to_playwright(source) == expected


def test_chrome_expiration_rejects_overflow():
    with pytest.raises(ValueError):
        chrome_expires_to_playwright(10**400)


def test_samesite_mapping():
    assert [chrome_samesite_to_playwright(value) for value in (-1, 0, 1, 2)] == [
        "Lax",
        "None",
        "Lax",
        "Strict",
    ]


def test_storage_state_shape_and_private_domain_guard():
    state = build_storage_state([row(), row(host_key="mail.example.com", name="password")])
    assert state == {
        "cookies": [
            {
                "name": "hhtoken",
                "value": "secret",
                "domain": ".hh.ru",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def test_backup_does_not_overwrite_existing_backup(tmp_path: Path):
    destination = tmp_path / "hh_session.json"
    backup = tmp_path / "hh_session.json.bak"
    destination.write_text('{"old": 1}', encoding="utf-8")
    backup.write_text("keep", encoding="utf-8")

    created_backup = write_storage_state({"cookies": [], "origins": []}, destination)

    assert created_backup == tmp_path / "hh_session.json.bak.1"
    assert backup.read_text(encoding="utf-8") == "keep"
    assert json.loads(destination.read_text(encoding="utf-8")) == {"cookies": [], "origins": []}


def test_write_failure_does_not_corrupt_existing_session(tmp_path: Path, monkeypatch):
    # Codex review (PR #168, cycle 3): write_storage_state писал прямо в
    # destination.write_text(), которое truncate'ит файл ДО записи нового
    # содержимого. Обрыв записи (диск заполнен, kill, OSError) на середине
    # оставлял активную сессию повреждённой/пустой, хотя бэкап уже был
    # сделан — запись должна быть atomic (temp-файл + os.replace), чтобы
    # сбой оставлял старую сессию нетронутой. Мокаем os.replace (последний
    # шаг atomic-записи) исключением — если реализация пишет через temp-файл
    # и заменяет destination только на успехе, обрыв на этом шаге не должен
    # тронуть исходный destination.
    destination = tmp_path / "hh_session.json"
    destination.write_text('{"old": 1}', encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr("os.replace", _boom)

    with pytest.raises(OSError):
        write_storage_state({"cookies": [], "origins": []}, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == {"old": 1}


def test_write_does_not_widen_session_file_permissions(tmp_path: Path):
    # Codex re-review (PR #168, post-rebase): the temp-file-then-os.replace()
    # atomic write (added for the previous finding) creates a NEW inode with
    # the process umask, discarding the existing session file's restrictive
    # mode (0600) — silently broadening a bearer-token secret to
    # group/world-readable. The active session must stay owner-only
    # regardless of what umask happens to be in effect.
    destination = tmp_path / "hh_session.json"
    destination.write_text('{"old": 1}', encoding="utf-8")
    destination.chmod(0o600)

    write_storage_state({"cookies": [], "origins": []}, destination)

    mode = destination.stat().st_mode & 0o777
    assert mode == 0o600, f"storage_state_file permissions widened to {oct(mode)}"


def test_new_account_session_and_directories_are_private(tmp_path: Path):
    destination = tmp_path / "data" / "accounts" / "work" / "storage_state" / "hh_session.json"
    write_storage_state({"cookies": [], "origins": []}, destination)

    if os.name != "nt":
        assert (destination.stat().st_mode & 0o777) == 0o600
        assert (destination.parent.stat().st_mode & 0o777) == 0o700
        assert (destination.parents[1].stat().st_mode & 0o777) == 0o700


def test_temp_file_is_never_world_readable_while_written(tmp_path: Path, monkeypatch):
    # Codex + /review re-review (PR #168): a previous fix called
    # tmp.chmod(0o600) AFTER tmp.write_text() had already created the file
    # (and written the hhtoken plaintext) under the process umask — a race
    # window where a world-readable umask (022) leaves the secret briefly
    # group/world-readable before chmod locks it down. The temp file must be
    # created with 0600 from the moment the inode exists (os.open with the
    # mode argument), not hardened afterward with a separate chmod() call.
    # Spy on os.open to capture the mode the fd was actually created with.
    destination = tmp_path / "hh_session.json"
    old_umask = os.umask(0o022)
    try:
        real_open = os.open
        observed_modes: list[int] = []

        def _spy_open(path, flags, mode=0o777, *args, **kwargs):
            fd = real_open(path, flags, mode, *args, **kwargs)
            if str(path).endswith(".tmp"):
                observed_modes.append(os.fstat(fd).st_mode & 0o777)
            return fd

        monkeypatch.setattr(os, "open", _spy_open)

        write_storage_state({"cookies": [], "origins": []}, destination)

        assert observed_modes, "expected write_storage_state to create the temp file via os.open"
        assert observed_modes[0] == 0o600, (
            f"temp file's fd was created with mode {oct(observed_modes[0])}, not 0o600 — "
            "secret was briefly world/group-readable before any later chmod"
        )
    finally:
        os.umask(old_umask)


def test_write_does_not_follow_preplanned_symlink_on_tmp_name(tmp_path: Path):
    # #171: temp-файл создавался по предсказуемому фиксированному имени
    # `<destination>.tmp` через os.open(O_CREAT|O_TRUNC) без O_EXCL — на
    # multi-user хосте атакующий мог заранее положить симлинк с этим именем
    # и получить секрет сессии (hhtoken) в свой файл с правами атакующего.
    # mkstemp() создаёт файл с непредсказуемым именем и O_EXCL — симлинк
    # по угаданному имени больше не преследуется.
    destination = tmp_path / "storage" / "hh_session.json"
    destination.parent.mkdir()
    attacker_target = tmp_path / "attacker_readable.json"
    attacker_target.write_text("junk", encoding="utf-8")
    attacker_target.chmod(0o666)
    os.symlink(attacker_target, destination.with_name(destination.name + ".tmp"))

    write_storage_state({"cookies": [], "origins": []}, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == {"cookies": [], "origins": []}
    assert attacker_target.read_text(encoding="utf-8") == "junk", (
        "секрет сессии утёк в файл-цель симлинка"
    )
    assert (destination.stat().st_mode & 0o777) == 0o600


def test_no_leftover_tmp_files_after_write(tmp_path: Path):
    destination = tmp_path / "hh_session.json"

    write_storage_state({"cookies": [], "origins": []}, destination)

    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_profile_name_resolves_from_chrome_profiles_root(tmp_path: Path, monkeypatch):
    # find-one-shot-20260815: `--profile Default` резолвился от cwd и падал
    # "No such file or directory: 'Default/Cookies'", хотя профиль есть в
    # стандартном корне Chrome. Имя профиля без cwd-пути должно резолвиться
    # от ~/Library/Application Support/Google/Chrome.
    import hhru_bot.cookie_import as cookie_import_mod

    profiles_root = tmp_path / "Chrome"
    (profiles_root / "Default").mkdir(parents=True)
    (profiles_root / "Profile 1").mkdir(parents=True)
    monkeypatch.setattr(cookie_import_mod, "DEFAULT_CHROME_PROFILES_ROOT", profiles_root)

    assert resolve_chrome_profile(Path("Default")) == profiles_root / "Default"
    assert resolve_chrome_profile(Path("Profile 1")) == profiles_root / "Profile 1"


def test_bare_profile_name_resolves_under_chrome_root_even_if_not_yet_created(
    tmp_path: Path, monkeypatch
):
    # cycle-review PR #173 round 3 (claude/review): the docstring and
    # --profile help text both advertise "Profile 1" as a bare name that
    # resolves under the Chrome profiles root the same way "Default" does.
    # The old fallback guard only special-cased the literal string "Default"
    # (`profile.name == DEFAULT_CHROME_PROFILE_NAME`), so any other bare
    # profile name that did not already exist under the Chrome root — e.g. a
    # not-yet-materialized "Profile 1" directory — silently fell through to a
    # plain cwd-relative Path instead, contradicting the documented contract.
    import hhru_bot.cookie_import as cookie_import_mod

    profiles_root = tmp_path / "Chrome"
    profiles_root.mkdir()
    monkeypatch.setattr(cookie_import_mod, "DEFAULT_CHROME_PROFILES_ROOT", profiles_root)

    # Nothing exists under profiles_root yet, and no cwd-relative path either.
    assert resolve_chrome_profile(Path("Profile 1")) == profiles_root / "Profile 1"


def test_explicit_profile_path_wins_over_profiles_root(tmp_path: Path, monkeypatch):
    import hhru_bot.cookie_import as cookie_import_mod

    profiles_root = tmp_path / "Chrome"
    profiles_root.mkdir()
    monkeypatch.setattr(cookie_import_mod, "DEFAULT_CHROME_PROFILES_ROOT", profiles_root)

    local_profile = tmp_path / "custom-profile"
    local_profile.mkdir()
    assert resolve_chrome_profile(local_profile) == local_profile
    assert resolve_chrome_profile(local_profile.resolve()) == local_profile.resolve()

    # A path with more than one segment (not a bare profile name) is never
    # redirected under the Chrome root, existing or not — the caller meant it
    # literally relative to cwd; read_chrome_cookies gives a clear error.
    missing_multi_segment = Path("some/dir/no-such-profile")
    assert resolve_chrome_profile(missing_multi_segment) == missing_multi_segment


def test_default_profile_is_chrome_default(monkeypatch, tmp_path: Path):
    import hhru_bot.cookie_import as cookie_import_mod

    monkeypatch.setattr(cookie_import_mod, "DEFAULT_CHROME_PROFILES_ROOT", tmp_path)
    assert resolve_chrome_profile(None) == tmp_path / "Default"


def test_mode_check_failure_closes_fd_before_raising(tmp_path: Path, monkeypatch):
    # cycle-review PR #173 round 1 (claude/review): when the defence-in-depth
    # mode check (#171) finds mkstemp() returned an fd with the wrong mode,
    # it raises OSError before os.fdopen(fd, ...) ever runs — so nothing
    # takes ownership of the raw fd. The `except BaseException` handler only
    # unlinked the temp file, never os.close()d the fd, leaking a descriptor
    # on every trip through this branch. Spy on os.close to confirm the fd
    # from mkstemp() is actually closed once the mode check trips.
    destination = tmp_path / "hh_session.json"
    real_fstat = os.fstat
    closed_fds: list[int] = []
    real_close = os.close

    def _fake_fstat(fd, *args, **kwargs):
        result = real_fstat(fd, *args, **kwargs)
        # Report a widened mode so the defence-in-depth check trips.
        return os.stat_result((0o100644,) + tuple(result)[1:])

    def _spy_close(fd, *args, **kwargs):
        closed_fds.append(fd)
        return real_close(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fstat", _fake_fstat)
    monkeypatch.setattr(os, "close", _spy_close)

    with pytest.raises(OSError, match="0600"):
        write_storage_state({"cookies": [], "origins": []}, destination)

    assert closed_fds, "mkstemp() fd was never closed after the mode check raised"


def test_backup_does_not_follow_preplanned_dangling_symlink(tmp_path: Path):
    # cycle-review PR #173 round 1 (codex, high/0.98): the backup-name loop
    # picked a candidate with `candidate.exists()`, which returns False for a
    # dangling symlink — so a pre-planted `<destination>.bak -> /attacker`
    # symlink was accepted as the backup name and shutil.copy2() followed it,
    # writing the session content (including hhtoken) into the attacker's
    # target file. This is the same symlink-attack class #171 closed for the
    # `.tmp` path, left open here for `.bak`.
    destination = tmp_path / "hh_session.json"
    destination.write_text(json.dumps({"cookies": [{"value": "secret-hhtoken"}]}), encoding="utf-8")
    dangling_target = tmp_path / "attacker_readable.json"
    os.symlink(dangling_target, destination.with_name(destination.name + ".bak"))

    write_storage_state({"cookies": [], "origins": []}, destination)

    assert not dangling_target.exists(), "секрет сессии утёк в файл-цель dangling symlink"


def test_backup_stat_failure_closes_fd_before_raising(tmp_path: Path, monkeypatch):
    # cycle-review PR #173 round 3 (claude/review): `fd = os.open(candidate,
    # ...)` (the exclusively-created `.bak` fd) used to be handed to
    # os.fdopen() only AFTER `destination.stat()` had already run inside the
    # same try block. If that stat() call raised, the `except BaseException`
    # handler unlinked the candidate name but never closed the raw fd from
    # os.open() — os.fdopen() was the only thing on that path that owned it,
    # and it was never reached. Fail destination.stat() on its second call
    # (the first is destination.exists() at function entry) and confirm the
    # backup fd is actually closed afterwards, not leaked.
    destination = tmp_path / "hh_session.json"
    destination.write_text(json.dumps({"cookies": [{"value": "secret"}]}), encoding="utf-8")

    opened_bak_fds: list[int] = []
    real_open = os.open

    def _spy_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        if str(path).endswith(".bak"):
            opened_bak_fds.append(fd)
        return fd

    call_count = {"n": 0}
    real_stat = Path.stat

    def _selective_failing_stat(self, *args, **kwargs):
        if self == destination:
            call_count["n"] += 1
            if call_count["n"] > 1:
                raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(os, "open", _spy_open)
    monkeypatch.setattr(Path, "stat", _selective_failing_stat)

    with pytest.raises(OSError, match="simulated stat failure"):
        write_storage_state({"cookies": [], "origins": []}, destination)

    assert opened_bak_fds, "test did not exercise the backup os.open() path"
    for fd in opened_bak_fds:
        with pytest.raises(OSError):
            os.fstat(fd)  # closed fd raises EBADF; an open one would succeed


def test_relative_multi_segment_profile_path_is_not_redirected(tmp_path: Path, monkeypatch):
    # cycle-review PR #173 round 2 (claude/review): the fallback condition
    # `profile.name == DEFAULT_CHROME_PROFILE_NAME` matches only the last
    # path segment (Path.name), so a relative path like `foo/Default` (not
    # just the bare name `Default`) was silently rewritten to
    # `DEFAULT_CHROME_PROFILES_ROOT / "foo/Default"`, discarding the
    # caller's relative-to-cwd path.
    import hhru_bot.cookie_import as cookie_import_mod

    profiles_root = tmp_path / "Chrome"
    profiles_root.mkdir()
    monkeypatch.setattr(cookie_import_mod, "DEFAULT_CHROME_PROFILES_ROOT", profiles_root)

    multi_segment = Path("foo/Default")
    assert resolve_chrome_profile(multi_segment) == multi_segment


def test_backup_copy_failure_leaves_no_empty_bak_file(tmp_path: Path, monkeypatch):
    # cycle-review PR #173 round 2 (claude/review): the O_EXCL backup-name
    # loop created the `.bak` inode BEFORE shutil.copy2() actually wrote
    # content into it. If copy2() failed partway, the empty `.bak` file was
    # left on disk with no cleanup — the next run's O_EXCL check would then
    # treat that name as permanently taken, drifting backup numbering
    # forever on every retried failure.
    import hhru_bot.cookie_import as cookie_import_mod

    destination = tmp_path / "hh_session.json"
    destination.write_text('{"old": 1}', encoding="utf-8")

    def _broken_read_bytes(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cookie_import_mod.Path, "read_bytes", _broken_read_bytes)

    with pytest.raises(OSError, match="disk full"):
        write_storage_state({"cookies": [], "origins": []}, destination)

    backup_candidate = destination.with_name(destination.name + ".bak")
    assert not backup_candidate.exists(), "failed backup copy left an empty .bak file behind"


def test_backup_write_survives_symlink_swap_after_exclusive_create(tmp_path: Path, monkeypatch):
    # cycle-review PR #173 round 2 (codex, high/0.98): the backup inode was
    # created exclusively (O_CREAT|O_EXCL) and its fd closed, THEN
    # shutil.copy2(path) wrote through the (now closed) path — reopening a
    # TOCTOU window. A local attacker able to modify the storage directory
    # could unlink the freshly created backup file and replace it with a
    # symlink in the window between close() and copy2(); copy2() would
    # follow the symlink and leak the session secret into the attacker's
    # target. The fix writes through the already-open fd instead of a path
    # lookup, so there is no window left to race. Simulate an attacker
    # racing the exclusive-create step: swap the backup name for a symlink
    # right after os.open() returns, before any write happens.
    destination = tmp_path / "hh_session.json"
    destination.write_text(json.dumps({"cookies": [{"value": "secret-hhtoken"}]}), encoding="utf-8")
    backup_path = destination.with_name(destination.name + ".bak")
    attacker_target = tmp_path / "attacker_owned.json"

    real_open = os.open
    swapped = False

    def _racing_open(path, flags, mode=0o777, *args, **kwargs):
        nonlocal swapped
        fd = real_open(path, flags, mode, *args, **kwargs)
        if not swapped and str(path) == str(backup_path) and (flags & os.O_EXCL):
            # Simulate an attacker winning the race right after the
            # exclusive create: unlink the just-created inode and replace
            # the name with a symlink to an attacker-owned file. If the
            # implementation still writes by reopening `path` (as the old
            # shutil.copy2(path) call did), it would now follow this symlink.
            swapped = True
            os.unlink(path)
            os.symlink(attacker_target, path)
        return fd

    monkeypatch.setattr(os, "open", _racing_open)

    write_storage_state({"cookies": [], "origins": []}, destination)

    assert not attacker_target.exists(), "секрет сессии утёк в файл-цель symlink через TOCTOU-гонку"
    assert backup_path.is_symlink(), (
        "sanity: the race actually replaced the backup path with a symlink"
    )
