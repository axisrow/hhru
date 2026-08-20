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


def test_headless_sandbox_failure_is_not_classified_as_browser_launch_error():
    """B1: headless-сбой песочницы уходит сырым PlaywrightError → traceback в CLI.

    cli.py ловит BrowserLaunchError и печатает [ENVIRONMENT]; всё остальное
    долетает до `raise` в cli.py:190 и печатается Python-ом как traceback.
    """
    playwright = _FakePlaywright(_LIVE_HEADLESS_SANDBOX_ERROR)

    with pytest.raises(PlaywrightError) as excinfo:
        launch_browser(playwright, headless=True)

    # Дефект: это НЕ BrowserLaunchError, поэтому cli.py напечатает traceback.
    assert not isinstance(excinfo.value, BrowserLaunchError)
    assert "Permission denied (1100)" in str(excinfo.value)


def test_permission_denied_is_absent_from_sandbox_markers():
    """B1, вторая причина: маркера живого сбоя нет в списке классификации."""
    source = inspect.getsource(launch_browser)

    assert "Operation not permitted" in source, "список маркеров изменился — пересмотреть тест"
    assert "Permission denied" not in source


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


def test_apply_daily_limit_is_per_resume_so_account_total_multiplies():
    """B3: `apply` без --resume идёт по всем резюме, и каждое имеет свой лимит.

    check_reply_limit в том же классе (throttle.py:57-62) намеренно считает
    аккаунт целиком ("account-wide replies"), а check_apply_limit — нет.
    При 3 резюме в конфиге и daily_apply_limit=40 аккаунт отправляет до 120
    откликов в день, что расходится с антифрод-замыслом дневного лимита
    (CLAUDE.md: «ограничена дневными лимитами ... чтобы не выглядеть как
    подозрительная автоматизация»).
    """
    from hhru_bot.throttle import LimitReached, Throttle

    class _History:
        """Каждое резюме уже исчерпало свой лимит; аккаунт суммарно — втрое больше."""

        def __init__(self, per_resume: int):
            self.per_resume = per_resume
            self.asked: list[tuple[str, str]] = []

        def count_today(self, resume_id: str, action: str) -> int:
            self.asked.append((resume_id, action))
            # Аккаунт-wide счёт (resume_id == "") не ведётся для apply вовсе.
            return 0 if resume_id == "" else self.per_resume

    class _Config:
        daily_apply_limit = 40
        daily_bump_limit = 10

    limit = _Config.daily_apply_limit
    history = _History(per_resume=limit - 1)
    throttle = Throttle(_Config(), history)

    resumes = ["6b85a5a1", "b3236ebb", "a6c9aec0"]
    for resume_id in resumes:
        # Каждое резюме на 39/40 — ни одно не упирается в лимит.
        throttle.check_apply_limit(resume_id, dry_run=False)

    # Дефект: лимит спрашивают по resume_id, аккаунт целиком не спрашивают ни разу.
    assert history.asked == [(r, "apply") for r in resumes]
    assert ("", "apply") not in history.asked

    # Суммарно по аккаунту уже 117 откликов при заявленном дневном лимите 40.
    assert len(resumes) * (limit - 1) > limit

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


def test_withdraw_failure_after_destructive_click_is_uncertain_and_not_retried(
    monkeypatch, tmp_path
):
    """B4: post-click failure is uncertain and history blocks a second click.

    Тест проходит РЕАЛЬНЫЙ путь `_withdraw_topic`: фейковая страница отдаёт
    валидный SSR с одним топиком, уникальную карточку и уникальную кнопку
    отзыва, клик действительно выполняется (`clicked` фиксируется), и только
    затем падает ожидание позитивного маркера — ровно «серая зона» #207.

    `_withdraw_topic` returns ``acted=True`` after the destructive click, so
    `_run_topics` must record ``uncertain``.  A separate history guard must
    then refuse a retry before inspecting/clicking the DOM: changing the
    status alone does not provide deduplication.
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

    assert statuses == ["uncertain"]

    # The uncertain row is also a deduplication barrier.  A second run must
    # not click based only on the still-present SSR/DOM state.
    second_page = _Page()
    assert module._run_topics(args, [topic], page=second_page, history=history, throttle=None)
    assert withdraw_clicked == ["withdraw"]
    with history._connect() as conn:
        rows = conn.execute(
            "SELECT status FROM actions WHERE action = 'withdraw' AND vacancy_id = ?",
            (topic,),
        ).fetchall()
    assert [row["status"] for row in rows] == ["uncertain"]


def test_bump_and_withdraw_implement_the_uncertain_invariant():
    """Контроль: bump and withdraw share the uncertain status vocabulary."""
    from pathlib import Path as _Path

    from hhru_bot import bump as bump_module

    bump_source = _Path(bump_module.__file__).read_text(encoding="utf-8")
    withdraw_source = (
        _Path(browser_module.__file__).parent / "commands" / "clear_negotiations.py"
    ).read_text(encoding="utf-8")

    assert "uncertain=True" in bump_source
    assert "uncertain" in withdraw_source


# --- B5: save_about теряет факт состоявшегося клика сохранения ---------------


def test_save_about_reports_plain_failure_after_the_save_click_already_landed():
    """B5: таймаут ПОСЛЕ save.click() выдаётся как «сохранение не подтверждено».

    about.py:153-156 кликает сохранение, затем ждёт закрытия формы. Таймаут в
    этой точке — ровно «серая зона» #176/#207: клик уже ушёл на hh.ru. Модуль
    сообщает об этом как об обычной ошибке (в about.py слова 'uncertain' нет),
    а команда печатает [FAIL] и завершает процесс с кодом 1 — пользователь
    делает вывод, что текст не сохранён.

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
    # ...но исход подан как «не подтверждено», без признака состоявшегося действия.
    assert "не подтверждено" in str(excinfo.value)
    assert "uncertain" not in str(excinfo.value).lower()


def test_about_module_has_no_uncertain_concept_at_all():
    """B5, вторая половина: в about.py признака состоявшегося клика нет вовсе."""
    from pathlib import Path as _Path

    from hhru_bot import about as about_module

    source = _Path(about_module.__file__).read_text(encoding="utf-8")
    assert "save.click()" in source
    assert "uncertain" not in source


# --- B6: тот же разрыв инварианта в редакторах резюме -----------------------


_WRITE_MODULES_WITHOUT_UNCERTAIN = (
    "about.py",
    "experience.py",
    "resume_position.py",
    "resume_sections.py",
)


@pytest.mark.parametrize("filename", _WRITE_MODULES_WITHOUT_UNCERTAIN)
def test_write_modules_click_save_but_never_mark_the_outcome_uncertain(filename):
    """B6: WRITE-модули кликают сохранение, но статуса «клик мог уйти» не имеют.

    Систематический разрыв инварианта #176/#207, а не единичный промах:
    pipeline.py (19 упоминаний 'uncertain'), publish_resume.py (10),
    resume_education.py (7), copy_resume.py (6), bump.py и reply_employers.py (5)
    его соблюдают; перечисленные здесь модули — нет ни одного упоминания.

    Наиболее наглядно в commands/resume_position.py:205-213: после
    `page.locator(SAVE).click()` любое исключение печатается как голый [FAIL],
    хотя сохранение уже могло примениться на hh.ru.
    """
    from pathlib import Path as _Path

    root = _Path(browser_module.__file__).parent
    candidates = [root / filename, root / "commands" / filename]
    sources = [path.read_text(encoding="utf-8") for path in candidates if path.exists()]
    assert sources, filename

    combined = "\n".join(sources)
    assert ".click()" in combined, f"{filename}: клика нет — находка требует пересмотра"
    assert "uncertain" not in combined
