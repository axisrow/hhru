"""Аудит #4: тесты-доказательства найденных дефектов (браузер не запускается).

Каждый тест воспроизводит находку из docs/research/reference-audit-full-sweep.md
на моках/чтении исходника. Ни один не открывает hh.ru и ничего не создаёт.
"""

from __future__ import annotations

import inspect

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot import browser as browser_module
from hhru_bot.browser import BrowserLaunchError, launch_browser

pytestmark = pytest.mark.unit


# --- B1: headless-сбой песочницы не классифицируется -------------------------

# Дословный маркер из живого краха 2026-08-19 08:59:21 (data/logs/hhru_bot.log:1243),
# команда list-resumes, headless (chrome-headless-shell в аргументах запуска).
_LIVE_HEADLESS_SANDBOX_ERROR = (
    "BrowserType.launch: Target page, context or browser has been closed\n"
    "Browser logs:\n"
    "[pid=13682][err] [0819/085917.940964:FATAL:base/apple/mach_port_rendezvous_mac.cc:159] "
    "Check failed: kr == KERN_SUCCESS. bootstrap_check_in "
    "org.chromium.Chromium.MachPortRendezvousServer.13682: Permission denied (1100)"
)


class _FailingChromium:
    def __init__(self, message: str):
        self.message = message

    def launch(self, **kwargs):  # noqa: ARG002 - подпись как у playwright
        del kwargs
        raise PlaywrightError(self.message)


class _FakePlaywright:
    def __init__(self, message: str):
        self.chromium = _FailingChromium(message)


def test_headless_sandbox_failure_is_classified_as_browser_launch_error():
    """B1: headless-сбой песочницы превращается в контролируемую ошибку CLI."""
    playwright = _FakePlaywright(_LIVE_HEADLESS_SANDBOX_ERROR)

    with pytest.raises(BrowserLaunchError) as excinfo:
        launch_browser(playwright, headless=True)

    assert "CODEX_SANDBOX_BROWSER_FAILURE" in str(excinfo.value)


def test_permission_denied_mach_port_failure_is_a_sandbox_marker():
    """B1: the observed macOS permission failure is classified explicitly."""
    source = inspect.getsource(launch_browser)

    assert "Operation not permitted" in source, "список маркеров изменился — пересмотреть тест"
    assert '"Permission denied" in details' in source
    assert "mach_port_rendezvous_mac" in source


def test_headed_sandbox_failure_is_classified():
    """Контроль: headed-путь с известным маркером работает — дефект именно в двух условиях."""
    playwright = _FakePlaywright("Operation not permitted while starting Crashpad")

    with pytest.raises(BrowserLaunchError):
        launch_browser(playwright, headless=False)


# --- B2 ОТКЛОНЕНА -------------------------------------------------------------
# Гипотеза «wait_for_url без timeout наследует 90 с — это дефект» опровергнута:
# browser.py:181-188 задаёт потолок context-wide через set_default_navigation_timeout
# осознанно и по DRY, а #352 (f7c1f53) правил ровно эти вызовы, добавляя wait_until
# и привязку к identity, но намеренно НЕ добавляя timeout. Отсутствие timeout —
# принятое решение проекта, а не упущение. Тест не пишется: доказывать нечего.


# --- B3: дневной лимит apply считается ПО РЕЗЮМЕ, а не по аккаунту -----------


def test_apply_daily_limit_is_account_wide_across_resumes():
    """B3: `apply` uses one account-wide daily counter for every resume.

    The apply command can iterate over all configured resumes, so checking
    each resume independently would multiply the configured allowance.  The
    same account-wide scope is already used by the reply limit.
    """
    from hhru_bot.throttle import LimitReached, Throttle

    class _History:
        """Record both the requested scope and the configured account count."""

        def __init__(self, account_count: int):
            self.account_count = account_count
            self.asked: list[tuple[str, str]] = []

        def count_today(self, resume_id: str, action: str) -> int:
            self.asked.append((resume_id, action))
            return self.account_count if resume_id == "" else 0

    class _Config:
        daily_apply_limit = 40
        daily_bump_limit = 10

    limit = _Config.daily_apply_limit
    history = _History(account_count=limit - 1)
    throttle = Throttle(_Config(), history)

    resumes = ["6b85a5a1", "b3236ebb", "a6c9aec0"]
    for resume_id in resumes:
        # Every resume consults the same account-wide counter.
        throttle.check_apply_limit(resume_id, dry_run=False)

    assert history.asked == [("", "apply") for _ in resumes]

    # Once the account reaches the limit, another resume cannot get a fresh
    # allowance of its own.
    at_limit = _History(account_count=limit)
    with pytest.raises(LimitReached, match="account"):
        Throttle(_Config(), at_limit).check_apply_limit(resumes[0], dry_run=False)
    assert at_limit.asked == [("", "apply")]

    # Контроль: reply-лимит в том же классе считается по аккаунту (resume_id == "").
    history.asked.clear()
    throttle.check_reply_limit(dry_run=False)
    assert history.asked == [("", "reply")]

    # И он действительно срабатывает, когда аккаунтный счётчик достигает лимита.
    class _AtLimit(_History):
        def count_today(self, resume_id: str, action: str) -> int:
            return limit

    with pytest.raises(LimitReached):
        Throttle(_Config(), _AtLimit(limit)).check_reply_limit(dry_run=False)


# --- B4: необратимый отзыв отклика не умеет статус 'uncertain' ---------------


def test_withdraw_failure_after_destructive_click_is_recorded_as_failed_not_uncertain(
    monkeypatch, tmp_path
):
    """B4: сбой ПОСЛЕ необратимого клика отзыва пишется как 'failed' (= не произошло).

    Тест проходит РЕАЛЬНЫЙ путь `_withdraw_topic`: фейковая страница отдаёт
    валидный SSR с одним топиком, уникальную карточку и уникальную кнопку
    отзыва, клик действительно выполняется (`clicked` фиксируется), и только
    затем падает ожидание позитивного маркера — ровно «серая зона» #207.

    `_withdraw_topic` возвращает (False, ...), а `_run_topics` (:304) знает
    только два статуса, поэтому необратимое действие записывается как `failed`.
    Для обратимого bump тот же проект реализует инвариант #176/#207 полностью
    (bump.py:102-119: acted=True + uncertain=True); в этом модуле слова
    'uncertain' нет вовсе.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from hhru_bot.commands import clear_negotiations as module
    from hhru_bot.history import History

    history = History(str(tmp_path / "history.db"))
    topic = "5503922709"
    withdraw_clicked: list[str] = []

    class _Element:
        def __init__(self, kind: str):
            self.kind = kind

        def click(self) -> None:
            withdraw_clicked.append(self.kind)

        def wait_for(self, **_kwargs):
            # Позитивный маркер успеха так и не появился: клик уже ушёл на hh.ru.
            raise PlaywrightTimeoutError("Timeout 10000ms exceeded")

        def locator(self, selector: str):
            # `card = cards.first`, поэтому карточка тоже скоупит вложенные локаторы.
            return _locator_for(selector)

    class _Locator:
        def __init__(self, kind: str, count: int):
            self.kind = kind
            self._count = count

        @property
        def first(self):
            return _Element(self.kind)

        def count(self) -> int:
            return self._count

        def locator(self, selector: str):
            return _locator_for(selector)

    def _locator_for(selector: str) -> _Locator:
        if selector == module.NEGOTIATION_WITHDRAW:
            return _Locator("withdraw", 1)
        if selector == module.NEGOTIATION_WITHDRAW_CONFIRM:
            return _Locator("confirm", 0)
        if selector == module.NEGOTIATION_WITHDRAW_SUCCESS:
            return _Locator("success", 1)
        if selector == module.NEGOTIATION_ITEM:
            return _Locator("card", 1)
        raise AssertionError(f"неожиданный селектор: {selector}")

    class _Page:
        def content(self) -> str:
            return "<html></html>"

        def locator(self, selector: str):
            return _locator_for(selector)

    ref = type("Ref", (), {"topic_id": topic, "chat_id": "1", "vacancy_id": "2"})()
    monkeypatch.setattr(module, "goto_hh", lambda *a, **k: None)
    monkeypatch.setattr(module, "topic_refs", lambda _html: [ref])

    args = type("Args", (), {"config": None, "history": str(tmp_path / "history.db")})()
    failed = module._run_topics(args, [topic], page=_Page(), history=history, throttle=None)

    # Необратимый клик ДЕЙСТВИТЕЛЬНО состоялся в этом прогоне.
    assert withdraw_clicked == ["withdraw"]
    assert failed is True

    with history._connect() as conn:
        rows = conn.execute(
            "SELECT status FROM actions WHERE action = 'withdraw' AND vacancy_id = ?",
            (topic,),
        ).fetchall()
    statuses = [row["status"] for row in rows]

    # Дефект: состоявшийся необратимый клик записан как 'failed' (= не произошёл).
    assert statuses == ["failed"]
    assert "uncertain" not in statuses


def test_bump_module_implements_the_uncertain_invariant_that_withdraw_lacks():
    """Контроль: инвариант в проекте есть — он просто не применён к отзыву."""
    from pathlib import Path as _Path

    from hhru_bot import bump as bump_module

    bump_source = _Path(bump_module.__file__).read_text(encoding="utf-8")
    withdraw_source = (
        _Path(browser_module.__file__).parent / "commands" / "clear_negotiations.py"
    ).read_text(encoding="utf-8")

    assert "uncertain=True" in bump_source
    assert "uncertain" not in withdraw_source


# --- B5: save_about теряет факт состоявшегося клика сохранения ---------------


def test_save_about_marks_post_click_timeout_as_uncertain():
    """B5: a timeout after save.click() is explicitly uncertain.

    about.py clicks save and then waits for the form to close. A timeout at
    this point is the #176/#207 grey zone: the click may already have reached
    hh.ru, so the error must retain the ``uncertain`` marker.

    Для сравнения: bump.py и reply_employers.py в тех же условиях возвращают
    acted=True + uncertain=True.
    """
    from hhru_bot.about import AboutGenerationError, save_about

    class _Locator:
        def __init__(self, *, count: int, on_wait=None):
            self._count = count
            self._on_wait = on_wait
            self.clicked = False
            self.filled: str | None = None

        def count(self) -> int:
            return self._count

        def fill(self, value: str) -> None:
            self.filled = value

        def click(self) -> None:
            self.clicked = True

        def wait_for(self, **_kwargs):
            if self._on_wait is not None:
                raise self._on_wait

    field = _Locator(count=1, on_wait=PlaywrightError("Timeout 30000ms exceeded"))
    save = _Locator(count=1)

    class _Page:
        def locator(self, selector: str):
            return save if "save" in selector else field

    with pytest.raises(AboutGenerationError) as excinfo:
        save_about(_Page(), "новый текст")

    # Клик сохранения СОСТОЯЛСЯ...
    assert save.clicked is True
    # ...и исход явно сохраняет признак состоявшегося действия.
    assert "не подтверждено" in str(excinfo.value)
    assert "uncertain" in str(excinfo.value).lower()


def test_about_module_marks_post_click_save_failures_as_uncertain():
    """B5: about.py keeps the save click visible in the uncertain outcome."""
    from pathlib import Path as _Path

    from hhru_bot import about as about_module

    source = _Path(about_module.__file__).read_text(encoding="utf-8")
    assert "save.click()" in source
    assert "uncertain" in source
    assert "SAVE_TIMEOUT_MS" in source


# --- B6: тот же разрыв инварианта в редакторах резюме -----------------------


_WRITE_MODULES_WITH_SAVE_OUTCOME = (
    "about.py",
    "experience.py",
    "resume_position.py",
    "resume_sections.py",
)


@pytest.mark.parametrize("filename", _WRITE_MODULES_WITH_SAVE_OUTCOME)
def test_write_modules_mark_post_click_save_failures_as_uncertain(filename):
    """B6: every WRITE module preserves an uncertain post-click outcome.

    This is the shared #176/#207 invariant: a save click followed by an
    exception must not be reported as an ordinary pre-click failure.

    The source-level assertion covers all four editors, including the command
    wrappers that turn the marker into the user-visible result.
    """
    from pathlib import Path as _Path

    root = _Path(browser_module.__file__).parent
    candidates = [root / filename, root / "commands" / filename]
    sources = [path.read_text(encoding="utf-8") for path in candidates if path.exists()]
    assert sources, filename

    combined = "\n".join(sources)
    assert ".click()" in combined, f"{filename}: клика нет — находка требует пересмотра"
    assert "uncertain" in combined
