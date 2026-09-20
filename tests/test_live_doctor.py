"""live-doctor: вердикты сверок handshake-диагностики, чистая логика (#1163)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hhru_bot.commands import live_doctor
from hhru_bot.live import ALLOWED_ACTIONS, PROTOCOL_VERSION

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((REPO_ROOT / "extensions" / "hhru-live" / "manifest.json").read_text())
BACKGROUND_JS = (REPO_ROOT / "extensions" / "hhru-live" / "background.js").read_text()


def _hello(**overrides) -> dict:
    """Handshake-диагностика, какой её шлёт background.js (announceHello)."""
    hello = {
        "kind": "hello",
        "v": PROTOCOL_VERSION,
        "actions": sorted(ALLOWED_ACTIONS),
        "permissions": sorted(live_doctor.EXPECTED_PERMISSIONS),
        "hostPermissions": sorted(live_doctor.EXPECTED_HOST_PERMISSIONS),
    }
    hello.update(overrides)
    return hello


class TestCheckVersion:
    def test_match_is_ok(self):
        ok, message = live_doctor.check_version(_hello())
        assert ok
        assert f"v{PROTOCOL_VERSION}" in message

    def test_mismatch_names_both_sides(self):
        ok, message = live_doctor.check_version(_hello(v=PROTOCOL_VERSION + 1))
        assert not ok
        assert f"расширение v{PROTOCOL_VERSION + 1}" in message
        assert f"сервер v{PROTOCOL_VERSION}" in message

    def test_bool_is_not_a_version(self):
        # True == 1 в Python: версия совпадает только по типу И значению
        # (тот же инвариант, что в protocol.parse_envelope).
        ok, _ = live_doctor.check_version(_hello(v=True))
        assert not ok

    def test_missing_hello_fails_with_pointer_to_step_2(self):
        ok, message = live_doctor.check_version(None)
        assert not ok
        assert "см. п.2" in message


class TestCheckAllowlist:
    def test_equal_sets_is_ok(self):
        ok, message = live_doctor.check_allowlist(_hello())
        assert ok
        assert "совпадают" in message

    def test_extension_only_actions_named(self):
        ok, message = live_doctor.check_allowlist(_hello(actions=["list_overlays", "extra_action"]))
        assert not ok
        assert "extra_action" in message
        assert "allowlist.py" in message

    def test_server_only_actions_named(self):
        ok, message = live_doctor.check_allowlist(_hello(actions=[]))
        assert not ok
        assert "расширение не объявляет" in message

    def test_diff_in_both_directions_lists_both(self):
        ok, message = live_doctor.check_allowlist(_hello(actions=["extra_action"]))
        assert not ok
        # Серверная часть (ALLOWED_ACTIONS непуста по построению) минус
        # {extra_action} — «расширение не объявляет» тоже в сообщении.
        assert "расширение не объявляет" in message

    def test_unreadable_actions_fail_closed(self):
        ok, _ = live_doctor.check_allowlist(_hello(actions="list_overlays"))
        assert not ok

    def test_missing_hello_fails(self):
        ok, message = live_doctor.check_allowlist(None)
        assert not ok
        assert "см. п.2" in message


class TestCheckPermissions:
    def test_exact_expected_set_is_ok(self):
        ok, message = live_doctor.check_permissions(_hello())
        assert ok
        assert "минимальны и достаточны" in message

    def test_extra_permission_is_not_minimal(self):
        ok, message = live_doctor.check_permissions(_hello(permissions=["storage", "tabs"]))
        assert not ok
        assert "tabs" in message
        assert "не минимальны" in message

    def test_missing_permission_is_not_sufficient(self):
        ok, message = live_doctor.check_permissions(_hello(permissions=[]))
        assert not ok
        assert "недостаточны" in message

    def test_host_permissions_checked_too(self):
        ok, message = live_doctor.check_permissions(
            _hello(hostPermissions=["https://evil.example/*"])
        )
        assert not ok
        assert "host_permissions" in message

    def test_missing_hello_fails(self):
        ok, _ = live_doctor.check_permissions(None)
        assert not ok


class TestConstantsGuardRepoFiles:
    """Стражи дрейфа констант doctor'а от файлов расширения."""

    def test_expected_permissions_mirror_manifest(self):
        assert live_doctor.EXPECTED_PERMISSIONS == set(MANIFEST["permissions"])
        assert live_doctor.EXPECTED_HOST_PERMISSIONS == set(MANIFEST["host_permissions"])

    def test_default_port_matches_extension_live_serve_url(self):
        match = re.search(r"const LIVE_SERVE_URL = 'ws://127\.0\.0\.1:(\d+)'", BACKGROUND_JS)
        assert match, "в background.js нет LIVE_SERVE_URL — обновите страж и doctor"
        assert live_doctor.DEFAULT_PORT == int(match.group(1))
