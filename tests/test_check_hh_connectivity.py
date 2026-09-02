"""Regression tests for the read-only connectivity diagnostic."""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_hh_connectivity.sh"


def _run(tmp_path, *args, **extra_env):
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$CURL_LOG"\n'
        "printf 'http_code=200 time_connect=0.01s time_total=0.10s size=1\\n200 0.10'\n"
    )
    fake_curl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "CURL_BIN": str(fake_curl),
            "CURL_LOG": str(tmp_path / "curl.log"),
            "CHECK_HH_SLOW_THRESHOLD": "5",
        }
    )
    env.update(extra_env)
    return subprocess.run(
        [str(SCRIPT), *args], env=env, text=True, capture_output=True, check=True
    ).stdout


def test_direct_probe_is_invariant_to_proxy_environment(tmp_path):
    without_proxy = _run(tmp_path)
    with_proxy = _run(
        tmp_path,
        HTTP_PROXY="http://127.0.0.1:8118",
        HTTPS_PROXY="http://127.0.0.1:8118",
        ALL_PROXY="http://127.0.0.1:8118",
    )

    def result_lines(output):
        return [
            line
            for line in output.splitlines()
            if line.startswith(("МАРШРУТ", "СКОРОСТЬ", "BASELINE", "ВЕРДИКТ"))
        ]

    assert result_lines(without_proxy) == result_lines(with_proxy)
    assert all("--noproxy *" in line for line in (tmp_path / "curl.log").read_text().splitlines())


def test_via_proxy_is_explicit_mode(tmp_path):
    output = _run(tmp_path, "--via-proxy")

    assert "КАНАЛ ПРОБЫ: via-proxy" in output
    assert "HTTP(S)_PROXY/ALL_PROXY" in output
    assert "ВЕРДИКТ: большинство baseline быстрые" in output
    assert all(
        "--noproxy *" not in line for line in (tmp_path / "curl.log").read_text().splitlines()
    )
