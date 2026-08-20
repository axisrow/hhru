from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import clear_negotiations as command
from hhru_bot.history import History

pytestmark = pytest.mark.integration


def _args(**overrides):
    values = dict(
        topic=None,
        vacancy=None,
        resume=None,
        account_wide=False,
        dry_run=False,
        force=False,
        config="unused",
        history="unused",
        headless=True,
        max_pages=5,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_vacancy_force_is_always_rejected():
    with pytest.raises(SystemExit) as exc:
        command.run(_args(vacancy="123", force=True, dry_run=True))
    assert exc.value.code == 1


def test_resume_force_is_plan_only(capsys):
    command.run(_args(resume="work", force=True))
    assert "Только план" in capsys.readouterr().out


def test_topic_without_force_is_rejected_noninteractive(monkeypatch):
    monkeypatch.setattr(command, "confirm_write", lambda *args, **kwargs: False)
    with pytest.raises(SystemExit) as exc:
        command.run(_args(topic="77"))
    assert exc.value.code == 1


@pytest.mark.parametrize("bad_max_pages", [0, -1, -5])
def test_max_pages_validation_rejects_non_positive_before_config(bad_max_pages):
    """Codex review round 3 (PR #196): --max-pages <= 0 must not be a silent no-op.

    range(max_pages) yields zero iterations for max_pages<=0, so
    fetch_responses() would collect zero pages without ever checking auth or
    raising an indeterminate-state error. clear_negotiations then sees
    cards=[], prints only a [WARN], and returns success — a destructive
    account-wide run that inspected nothing reports exit 0. This is pure
    input validation, unrelated to the discovery-ambiguity trade-off; it
    must fail closed in _validate(), before load_config_or_exit()/opening a
    browser is even attempted (otherwise the exit 1 the previous version of
    this test observed came from a missing config file, not the guard).
    """
    with pytest.raises(SystemExit) as exc:
        command._validate(_args(account_wide=True, force=True, max_pages=bad_max_pages))
    assert exc.value.code == 1


@pytest.mark.parametrize("bad_max_pages", [0, -1, -5])
def test_account_wide_rejects_non_positive_max_pages(bad_max_pages, tmp_path):
    """End-to-end: run() must fail before any config/browser access is attempted."""

    def _boom(*args, **kwargs):
        raise AssertionError("load_config_or_exit must not be reached for invalid --max-pages")

    import hhru_bot.config

    original = hhru_bot.config.load_config_or_exit
    hhru_bot.config.load_config_or_exit = _boom
    try:
        with pytest.raises(SystemExit) as exc:
            command.run(_args(account_wide=True, force=True, max_pages=bad_max_pages))
        assert exc.value.code == 1
    finally:
        hhru_bot.config.load_config_or_exit = original


class _Locator:
    def __init__(self, count=1, *, clicked=None, children=None, click_error=None):
        self._count = count
        self.clicked = clicked
        self.children = children or {}
        self.click_error = click_error

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def click(self):
        if self.click_error is not None:
            raise self.click_error
        if self.clicked is not None:
            self.clicked.append(True)

    def wait_for(self, *, timeout: float | None = None, state: str | None = None):  # noqa: ARG002
        return None

    def locator(self, selector):
        assert selector in self.children, f"unexpected child selector: {selector}"
        return self.children[selector]


class _Page:
    """Mirrors the production DOM: card-scoped locators (control/confirm/
    success) live under NEGOTIATION_ITEM, not directly on the page, so a
    test can distinguish a card-scoped read from an unscoped page-wide one.
    """

    def __init__(self, *, control_count=1, confirm_count=0, success_count=1, click_error=None):
        self.clicked = []
        self._control = _Locator(control_count, clicked=self.clicked, click_error=click_error)
        self._confirm = _Locator(confirm_count)
        self._success = _Locator(success_count)
        self._card = _Locator(
            1,
            children={
                command.NEGOTIATION_WITHDRAW: self._control,
                command.NEGOTIATION_WITHDRAW_CONFIRM: self._confirm,
                command.NEGOTIATION_WITHDRAW_SUCCESS: self._success,
            },
        )

    def locator(self, selector):
        if selector == command.NEGOTIATION_ITEM:
            return self._card
        raise AssertionError(f"unexpected page-level selector: {selector}")

    def content(self):
        return "<html />"


class _Throttle:
    def wait(self, reason):
        pass


def _patch_withdraw_page(monkeypatch):
    monkeypatch.setattr(command, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(command, "topic_refs", lambda html: [type("Ref", (), {"topic_id": "77"})()])


def test_successful_withdraw_is_audited(tmp_path, monkeypatch):
    _patch_withdraw_page(monkeypatch)
    page = _Page()
    history = History(tmp_path / "history.db")
    failed = command._run_topics(
        _args(force=True), ["77"], page=page, history=history, throttle=_Throttle()
    )
    with history._connect() as conn:
        row = conn.execute("SELECT resume_id, vacancy_id, action, status FROM actions").fetchone()
    assert tuple(row) == (command.ACCOUNT_SCOPE, "77", "withdraw", "success")
    assert failed is False
    assert page.clicked == [True]


def test_withdraw_without_positive_confirmation_is_uncertain(tmp_path, monkeypatch):
    _patch_withdraw_page(monkeypatch)
    page = _Page(success_count=0)
    history = History(tmp_path / "history.db")
    failed = command._run_topics(
        _args(force=True), ["77"], page=page, history=history, throttle=_Throttle()
    )
    assert failed is True
    with history._connect() as conn:
        assert conn.execute("SELECT status FROM actions").fetchone()[0] == "uncertain"


def test_withdraw_click_error_is_recorded_as_uncertain(tmp_path, monkeypatch):
    _patch_withdraw_page(monkeypatch)
    page = _Page(click_error=RuntimeError("connection reset"))
    history = History(tmp_path / "history.db")
    failed = command._run_topics(
        _args(force=True), ["77"], page=page, history=history, throttle=_Throttle()
    )
    assert failed is True
    with history._connect() as conn:
        assert conn.execute("SELECT status FROM actions").fetchone()[0] == "uncertain"


def test_withdraw_reserves_a_durable_barrier_before_the_destructive_click(tmp_path, monkeypatch):
    """A process crash between the destructive click and the audit write
    committing must not clear the retry barrier — the same class of gap
    #245 closed for ``apply`` via ``begin_action``/``finalize_action``
    (reserve an ``uncertain`` row *before* the browser side effect, then
    finalize it in place).  ``clear_negotiations`` must reserve that barrier
    before ``_withdraw_topic`` runs, not only write history after it
    returns — otherwise a crash right after the click (browser vanishes,
    process killed, OOM) leaves zero history rows and a restarted run
    re-clicks the same, already-withdrawn topic.

    This test proves the barrier is durable *before* the click: it makes
    the browser side hang/crash unrecoverably (raises a bare ``BaseException``
    subclass that a defensive ``except Exception`` around the click cannot
    swallow, standing in for "the process died here") and then asserts the
    reserved row is already visible in history — i.e. it must have been
    written before ``_withdraw_topic`` was ever called, not after.
    """
    _patch_withdraw_page(monkeypatch)
    history = History(tmp_path / "history.db")

    class _ProcessKilled(BaseException):
        pass

    def _crash_during_click(page, topic):
        # Simulate the process dying mid-click: no return value, no
        # exception an `except Exception` handler could observe.
        raise _ProcessKilled("simulated crash during the destructive click")

    monkeypatch.setattr(command, "_withdraw_topic", _crash_during_click)
    page = _Page()
    with pytest.raises(_ProcessKilled):
        command._run_topics(
            _args(force=True), ["77"], page=page, history=history, throttle=_Throttle()
        )

    # The barrier must already be durable in history — reserved BEFORE
    # `_withdraw_topic` ran, not written only after it returns.
    with history._connect() as conn:
        row = conn.execute(
            "SELECT status FROM actions WHERE vacancy_id = ? AND action = 'withdraw'",
            ("77",),
        ).fetchone()
    assert row is not None, (
        "no audit row was reserved before the destructive click — a crash here "
        "leaves no retry barrier, so a restarted run will click the same topic again"
    )
    assert row[0] == "uncertain"


def test_withdraw_without_unique_button_does_not_click(tmp_path, monkeypatch):
    _patch_withdraw_page(monkeypatch)
    page = _Page(control_count=0)
    history = History(tmp_path / "history.db")
    command._run_topics(_args(force=True), ["77"], page=page, history=history, throttle=_Throttle())
    assert page.clicked == []


def test_withdraw_refuses_when_topic_filter_unproven(tmp_path, monkeypatch):
    """?topic= filtering the SSR list is unverified; if the same read also
    surfaces an unrelated topic, a single remaining DOM card can no longer
    be trusted as this topic's card (it may just be an unfiltered list that
    happens to render one card) — must refuse, never click on the guess.
    """
    monkeypatch.setattr(command, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(
        command,
        "topic_refs",
        lambda html: [
            type("Ref", (), {"topic_id": "77"})(),
            type("Ref", (), {"topic_id": "88"})(),
        ],
    )
    page = _Page()
    history = History(tmp_path / "history.db")
    failed = command._run_topics(
        _args(force=True), ["77"], page=page, history=history, throttle=_Throttle()
    )
    assert failed is True
    assert page.clicked == []


def test_withdraw_success_marker_is_scoped_to_card_not_page(tmp_path, monkeypatch):
    """A page-wide success-marker read could be satisfied by an unrelated
    negotiation's marker (e.g. a prior withdrawal still rendered on the
    same page during _run_topics' page-reuse loop) and falsely report
    success for a topic that was never actually withdrawn. The mock _Page
    only exposes NEGOTIATION_WITHDRAW_SUCCESS as a child of the card
    locator (not page-level), so this only passes if the production code
    reads it via card.locator(...), not page.locator(...).
    """
    _patch_withdraw_page(monkeypatch)
    page = _Page(success_count=1)
    history = History(tmp_path / "history.db")
    failed = command._run_topics(
        _args(force=True), ["77"], page=page, history=history, throttle=_Throttle()
    )
    assert failed is False
    with history._connect() as conn:
        assert conn.execute("SELECT status FROM actions").fetchone()[0] == "success"


def test_withdraw_refuses_when_confirmation_dialog_already_open(tmp_path, monkeypatch):
    """A confirmation dialog already present before the click may belong to
    a stale attempt on a previous topic (_run_topics reuses the same page
    across iterations) — must refuse rather than click the withdraw control
    into an already-non-actionable page state.
    """
    _patch_withdraw_page(monkeypatch)
    page = _Page(confirm_count=1)
    history = History(tmp_path / "history.db")
    failed = command._run_topics(
        _args(force=True), ["77"], page=page, history=history, throttle=_Throttle()
    )
    assert failed is True
    assert page.clicked == []


def test_run_topic_returns_true_on_failed_withdraw(tmp_path, monkeypatch):
    _patch_withdraw_page(monkeypatch)
    page = _Page(success_count=0)

    class Context:
        def new_page(self):
            return page

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    history = History(tmp_path / "history.db")
    monkeypatch.setattr(command, "confirm_write", lambda *args, **kwargs: True)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *args, **kwargs: Context())
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda *args, **kwargs: type(
            "Cfg", (), {"storage_state_file": "unused", "user_agent": None, "throttle": None}
        )(),
    )
    monkeypatch.setattr("hhru_bot.history.History", lambda *args, **kwargs: history)
    monkeypatch.setattr("hhru_bot.throttle.Throttle", lambda *args, **kwargs: _Throttle())

    assert command.run(_args(topic="77", force=True)) is True


def test_account_wide_rejects_when_max_pages_truncated(tmp_path, monkeypatch):
    """Codex review (PR #196): hitting --max-pages must not look like completeness.

    fetch_responses() silently stops after --max-pages pages even if more
    negotiations exist on hh.ru. Withdrawing only what was found and
    reporting success on a destructive, irreversible operation would leave a
    silent remainder. clear_negotiations must re-check _has_next_page() and
    fail closed instead.
    """
    from hhru_bot.responses import ResponseItem

    class Page:
        pass

    class Context:
        def new_page(self):
            return Page()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    cards = [ResponseItem(vacancy_id="1", status="read", topic="tp1")]
    history = History(tmp_path / "history.db")
    monkeypatch.setattr(command, "confirm_write", lambda *args, **kwargs: True)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *args, **kwargs: Context())
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda *args, **kwargs: type(
            "Cfg", (), {"storage_state_file": "unused", "user_agent": None, "throttle": None}
        )(),
    )
    monkeypatch.setattr("hhru_bot.history.History", lambda *args, **kwargs: history)
    monkeypatch.setattr("hhru_bot.throttle.Throttle", lambda *args, **kwargs: None)
    monkeypatch.setattr(command, "fetch_responses", lambda page, max_pages: cards)
    monkeypatch.setattr(command, "_has_next_page", lambda page, page_num: True)

    with pytest.raises(SystemExit) as exc:
        command.run(_args(account_wide=True, force=True, max_pages=5))
    assert exc.value.code == 1
    with history._connect() as conn:
        rows = conn.execute("SELECT vacancy_id FROM actions").fetchall()
    assert rows == []  # nothing withdrawn before the completeness check failed


def test_account_wide_empty_discovery_warns_but_does_not_hard_fail(tmp_path, monkeypatch, capsys):
    """Codex review round 2 (PR #196): empty discovery must not be silent.

    fetch_responses() returning [] on the first page is indistinguishable
    from a discovery failure (stale selector/render timeout) per its own
    docstring. Refusing outright would falsely block a legitimately empty
    account, so this must warn visibly instead of failing hard or silently
    reporting success with no explanation.
    """

    class Page:
        pass

    class Context:
        def new_page(self):
            return Page()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    history = History(tmp_path / "history.db")
    monkeypatch.setattr(command, "confirm_write", lambda *args, **kwargs: True)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *args, **kwargs: Context())
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda *args, **kwargs: type(
            "Cfg", (), {"storage_state_file": "unused", "user_agent": None, "throttle": None}
        )(),
    )
    monkeypatch.setattr("hhru_bot.history.History", lambda *args, **kwargs: history)
    monkeypatch.setattr("hhru_bot.throttle.Throttle", lambda *args, **kwargs: None)
    monkeypatch.setattr(command, "fetch_responses", lambda page, max_pages: [])

    failed = command.run(_args(account_wide=True, force=True))
    assert failed is False  # no false refusal for a legitimately empty account
    assert "[WARN]" in capsys.readouterr().err


def test_account_wide_skipped_cards_flip_exit_status(tmp_path, monkeypatch):
    """Codex review round 2 (PR #196): skipped cards must count as failure.

    Cards without a resolved topic were counted and printed, but the count
    never affected the returned status — an account-wide run with resolved
    topics all succeeding still returned False (success) even though some
    negotiations were never attempted. That contradicts the fail-closed
    contract for a destructive, irreversible command (issue #111).
    """
    from hhru_bot.responses import ResponseItem

    class Response:
        ok = True
        status = 204

    class Request:
        def delete(self, url):
            return Response()

    class Page:
        request = Request()

    class Throttle:
        def wait(self, reason):
            pass

    class Context:
        def new_page(self):
            return Page()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    cards = [
        ResponseItem(vacancy_id="1", status="read", topic="tp1"),
        ResponseItem(vacancy_id="2", status="read", topic=None),
    ]
    history = History(tmp_path / "history.db")
    monkeypatch.setattr(command, "confirm_write", lambda *args, **kwargs: True)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *args, **kwargs: Context())
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda *args, **kwargs: type(
            "Cfg", (), {"storage_state_file": "unused", "user_agent": None, "throttle": None}
        )(),
    )
    monkeypatch.setattr("hhru_bot.history.History", lambda *args, **kwargs: history)
    monkeypatch.setattr("hhru_bot.throttle.Throttle", lambda *args, **kwargs: Throttle())
    monkeypatch.setattr(command, "fetch_responses", lambda page, max_pages: cards)
    monkeypatch.setattr(command, "_has_next_page", lambda page, page_num: False)

    failed = command.run(_args(account_wide=True, force=True))
    assert failed is True  # a skipped card is an incomplete account-wide run


def test_account_wide_skips_cards_without_topic(tmp_path, monkeypatch):
    """Codex review (PR #196): cards with topic=None must not look processed.

    ``if card.topic`` used to drop chatless/ambiguous cards silently, so the
    command could report full success while some negotiations were untouched.
    Now the run must at least surface how many cards were skipped.
    """
    from hhru_bot.responses import ResponseItem

    class Response:
        ok = True
        status = 204

    class Request:
        def delete(self, url):
            return Response()

    class Page:
        request = Request()

    class Throttle:
        def wait(self, reason):
            pass

    class Context:
        def new_page(self):
            return Page()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    cards = [
        ResponseItem(vacancy_id="1", status="read", topic="tp1"),
        ResponseItem(vacancy_id="2", status="read", topic=None),
        ResponseItem(vacancy_id="3", status="read", topic=None, topic_ambiguous=True),
    ]
    history = History(tmp_path / "history.db")
    monkeypatch.setattr(command, "confirm_write", lambda *args, **kwargs: True)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *args, **kwargs: Context())
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda *args, **kwargs: type(
            "Cfg", (), {"storage_state_file": "unused", "user_agent": None, "throttle": None}
        )(),
    )
    monkeypatch.setattr("hhru_bot.history.History", lambda *args, **kwargs: history)
    monkeypatch.setattr("hhru_bot.throttle.Throttle", lambda *args, **kwargs: Throttle())
    monkeypatch.setattr(command, "fetch_responses", lambda page, max_pages: cards)
    monkeypatch.setattr(command, "_has_next_page", lambda page, page_num: False)

    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        command.run(_args(account_wide=True, force=True))
    out = buf.getvalue()
    assert "2" in out  # skipped-count surfaced, not silently dropped
    with history._connect() as conn:
        rows = conn.execute("SELECT vacancy_id FROM actions").fetchall()
    assert [r[0] for r in rows] == ["tp1"]
