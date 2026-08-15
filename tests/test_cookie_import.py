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


def test_explicit_profile_path_wins_over_profiles_root(tmp_path: Path, monkeypatch):
    import hhru_bot.cookie_import as cookie_import_mod

    profiles_root = tmp_path / "Chrome"
    profiles_root.mkdir()
    monkeypatch.setattr(cookie_import_mod, "DEFAULT_CHROME_PROFILES_ROOT", profiles_root)

    local_profile = tmp_path / "custom-profile"
    local_profile.mkdir()
    assert resolve_chrome_profile(local_profile) == local_profile
    assert resolve_chrome_profile(local_profile.resolve()) == local_profile.resolve()

    # несуществующее имя не подменяется молча на корень Chrome — ошибку
    # выдаст read_chrome_cookies с исходным путём
    missing = Path("no-such-profile")
    assert resolve_chrome_profile(missing) == missing


def test_default_profile_is_chrome_default(monkeypatch, tmp_path: Path):
    import hhru_bot.cookie_import as cookie_import_mod

    monkeypatch.setattr(cookie_import_mod, "DEFAULT_CHROME_PROFILES_ROOT", tmp_path)
    assert resolve_chrome_profile(None) == tmp_path / "Default"
