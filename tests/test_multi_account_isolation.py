"""Integration coverage for isolation between named accounts.

The account implementation already scopes local state by the account
directory.  These tests deliberately exercise two complete account fixtures
instead of replacing that boundary with a fake mapping: each account has a
different name, config path, history path, and session path.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hhru_bot import browser
from hhru_bot.accounts import AccountPaths, resolve_account_paths
from hhru_bot.cli import main
from hhru_bot.config import AppConfig, load_config
from hhru_bot.history import History
from hhru_bot.throttle import LimitReached, Throttle

pytestmark = pytest.mark.integration


def _write_account(
    data_dir: Path,
    name: str,
    *,
    resume_slug: str,
    resume_id: str,
    session_name: str,
) -> AccountPaths:
    account_dir = data_dir / "accounts" / name
    account_dir.mkdir(parents=True)
    config = account_dir / "config.yaml"
    config.write_text(
        f"""\
account:
  storage_state_file: sessions/{session_name}
throttle:
  daily_apply_limit: 1
  daily_bump_limit: 1
resumes:
  - id: {resume_slug}
    resume_url: https://hh.ru/resume/{resume_id}
    search:
      text: python
""",
        encoding="utf-8",
    )
    return resolve_account_paths(name, data_dir=data_dir)


def _accounts(tmp_path: Path) -> tuple[dict[str, AccountPaths], dict[str, AppConfig]]:
    """Create two non-overlapping account fixtures and load their configs."""
    data_dir = tmp_path / "data"
    paths = {
        "alpha": _write_account(
            data_dir,
            "alpha",
            resume_slug="alpha-python",
            resume_id="ALPHA123",
            session_name="alpha-session.json",
        ),
        "beta": _write_account(
            data_dir,
            "beta",
            resume_slug="beta-python",
            resume_id="BETA456",
            session_name="beta-session.json",
        ),
    }
    configs = {name: load_config(account_paths.config) for name, account_paths in paths.items()}
    return paths, configs


def test_accounts_resolve_distinct_history_and_session_paths(tmp_path):
    paths, configs = _accounts(tmp_path)

    assert paths["alpha"].config != paths["beta"].config
    assert paths["alpha"].history != paths["beta"].history
    assert configs["alpha"].storage_state_file != configs["beta"].storage_state_file
    assert configs["alpha"].storage_state_file.name == "alpha-session.json"
    assert configs["beta"].storage_state_file.name == "beta-session.json"
    assert configs["alpha"].resumes[0].id != configs["alpha"].resumes[0].resume_id
    assert configs["beta"].resumes[0].id != configs["beta"].resumes[0].resume_id


def test_history_and_daily_limits_do_not_cross_account_boundary(tmp_path):
    paths, configs = _accounts(tmp_path)
    alpha_history = History(paths["alpha"].history)
    beta_history = History(paths["beta"].history)
    alpha_resume_id = configs["alpha"].resumes[0].resume_id
    beta_resume_id = configs["beta"].resumes[0].resume_id

    # Use the same domain keys when looking from the other account.  This makes
    # a shared/global history database observable; the account boundary must be
    # the distinct history path, not accidental resume/vacancy key differences.
    alpha_history.record_action(alpha_resume_id, "vacancy-alpha", "apply", "success")
    alpha_history.record_action(alpha_resume_id, alpha_resume_id, "bump", "success")

    assert alpha_history.has_applied(alpha_resume_id, "vacancy-alpha")
    assert beta_history.has_applied(alpha_resume_id, "vacancy-alpha") is False
    assert beta_history.count_today("", "apply") == 0
    assert beta_history.count_today(alpha_resume_id, "bump") == 0

    alpha_throttle = Throttle(configs["alpha"].throttle, alpha_history)
    beta_throttle = Throttle(configs["beta"].throttle, beta_history)
    with pytest.raises(LimitReached):
        alpha_throttle.check_apply_limit(alpha_resume_id, dry_run=False)
    beta_throttle.check_apply_limit(beta_resume_id, dry_run=False)

    with pytest.raises(LimitReached):
        alpha_throttle.check_bump_limit(alpha_resume_id, dry_run=False)
    beta_throttle.check_bump_limit(beta_resume_id, dry_run=False)


def test_browser_context_loads_selected_account_session(tmp_path, monkeypatch):
    paths, configs = _accounts(tmp_path)
    for config in configs.values():
        config.storage_state_file.parent.mkdir(parents=True)
        config.storage_state_file.touch()

    captured: list[dict] = []

    @contextmanager
    def fake_sync_playwright():
        context = MagicMock(name="BrowserContext")
        fake_browser = MagicMock(name="Browser")

        def new_context(**kwargs):
            captured.append(kwargs)
            return context

        fake_browser.new_context.side_effect = new_context
        playwright = MagicMock(name="Playwright")
        playwright.chromium.launch.return_value = fake_browser
        yield playwright

    monkeypatch.setattr(browser, "sync_playwright", fake_sync_playwright)

    with browser.launch_context(configs["alpha"].storage_state_file, headless=True):
        pass
    with browser.launch_context(configs["beta"].storage_state_file, headless=True):
        pass

    assert [call["storage_state"] for call in captured] == [
        str(configs["alpha"].storage_state_file),
        str(configs["beta"].storage_state_file),
    ]
    assert captured[0]["storage_state"] != captured[1]["storage_state"]


def test_main_account_selection_routes_history_and_session_to_each_account(tmp_path, monkeypatch):
    paths, configs = _accounts(tmp_path)
    monkeypatch.chdir(tmp_path)

    real_history = History
    history_paths: list[Path] = []
    session_paths: list[Path] = []

    def history_factory(path):
        history_paths.append(Path(path).resolve())
        return real_history(path)

    @contextmanager
    def fake_launch_context(storage_state_file, **_kwargs):
        session_paths.append(Path(storage_state_file).resolve())

        class Context:
            def new_page(self):
                return object()

        yield Context()

    monkeypatch.setattr("hhru_bot.history.History", history_factory)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", load_config)
    monkeypatch.setattr(browser, "launch_context", fake_launch_context)

    from hhru_bot.bump import BumpResult

    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda _page, resume, dry_run: BumpResult(resume.resume_id, True, "dry-run"),
    )

    for name in ("alpha", "beta"):
        main(["--account", name, "--headless", "bump", "--dry-run"])

    assert history_paths == [paths["alpha"].history.resolve(), paths["beta"].history.resolve()]
    assert session_paths == [
        configs["alpha"].storage_state_file.resolve(),
        configs["beta"].storage_state_file.resolve(),
    ]
    assert history_paths[0] != history_paths[1]
    assert session_paths[0] != session_paths[1]
