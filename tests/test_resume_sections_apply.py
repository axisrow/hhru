"""Browser-level тесты _apply_rows/apply_plan (#352 cycle-review round 1, codex).

Стаб Page/Locator моделирует ровно то, что использует _apply_rows: click(),
locator(...).wait_for(state="visible"), .count(). ``ready_ok=False`` имитирует
таймаут гидратации (PlaywrightTimeoutError из wait_for) ПОСЛЕ того, как
предыдущая строка уже была сохранена — это регрессионный сценарий для находки
codex: до фикса такое исключение вылетало наружу из apply_plan необработанным,
теряя факт частичного сохранения; после фикса — заносится в errors как
конкретная строка, и обработка блока останавливается (fail-closed), не давая
исключению выйти за пределы apply_plan.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import hhru_bot.resume_sections as resume_sections
from hhru_bot.resume_sections import (
    OUTCOME_APPENDED,
    OUTCOME_FAILED,
    OUTCOME_PLANNED,
    OUTCOME_UNCERTAIN,
    OUTCOME_UPDATED,
    Attestation,
    Recommendation,
    RowOutcome,
    _apply_contacts,
    _apply_rows,
    apply_plan,
)

pytestmark = pytest.mark.unit


class FakeSaveButton:
    def __init__(self, page):
        self._page = page

    def count(self):
        return 1

    def click(self):
        self._page.saved_rows.append(self._page.current_index)

    def wait_for(self, *, state="hidden", timeout=None):  # noqa: ARG002
        # The inline editor closing (this button disappearing) confirms the
        # save (#331) — the fakes above always model a successful close.
        pass


class FakeReadyLocator:
    def __init__(self, page, ready: bool):
        self._page = page
        self._ready = ready

    def wait_for(self, *, state="visible", timeout=None):  # noqa: ARG002
        if not self._ready:
            raise PlaywrightTimeoutError("гидратация не завершилась вовремя")


class FakeTrigger:
    def __init__(self, page, count):
        self._page = page
        self._count = count
        self._calls = 0

    def count(self):
        # nth-й вызов count() (0-indexed) соответствует итерации цикла с тем же
        # индексом, т.к. _apply_rows зовёт trigger.count() ровно раз за строку.
        call_index = self._calls
        self._calls += 1
        if call_index in self._page._count_fails:
            raise PlaywrightTimeoutError("count() недоступен")
        return self._count

    def nth(self, index):
        return FakeTriggerRow(self._page, index)


class FakeTriggerRow:
    def __init__(self, page, index):
        self._page = page
        self._index = index

    def click(self):
        if self._index in self._page._click_fails:
            raise PlaywrightTimeoutError("триггер не кликается")
        self._page.current_index = self._index


class FakePage:
    """Строка ``ready_by_index[i] = False`` имитирует таймаут гидратации на
    строке i (после того как строки < i уже были кликнуты save). ``click_fails``
    имитирует таймаут САМОГО клика по триггеру строки i (codex, cycle 2:
    trigger.nth(index).click() изначально был вне try/except)."""

    def __init__(
        self,
        *,
        trigger_count: int,
        ready_by_index: dict[int, bool] | None = None,
        click_fails: set[int] | None = None,
        count_fails: set[int] | None = None,
    ):
        self._trigger_count = trigger_count
        self._ready_by_index = ready_by_index or {}
        self._click_fails = click_fails or set()
        self._count_fails = count_fails or set()
        self.saved_rows: list[int] = []
        self.current_index = -1

    def locator(self, selector: str):
        if selector.startswith("[data-qa^="):
            return FakeTrigger(self, self._trigger_count)
        if selector == "[data-qa='profile-layout-save-button']":
            return FakeSaveButton(self)
        # ready_selector для attestations/recommendations
        ready = self._ready_by_index.get(self.current_index, True)
        return FakeReadyLocator(self, ready)


def _fill_row(page, item):  # noqa: ARG001
    return page.locator("[data-qa='profile-layout-save-button']")


def test_hydration_timeout_after_prior_save_is_reported_not_raised():
    """Codex-находка: таймаут wait_for на строке 1 ПОСЛЕ успешного save строки 0
    должен вернуться как элемент errors, а не всплыть исключением из _apply_rows."""
    page = FakePage(trigger_count=2, ready_by_index={1: False})
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    # Строка 0 успела сохраниться до таймаута на строке 1.
    assert page.saved_rows == [0]
    # Ошибка сообщает именно про строку 1, а не тонет молча и не падает исключением.
    assert len(errors) == 1
    assert "строка 1" in errors[0]
    assert "attestations" in errors[0]


def test_trigger_click_failure_after_prior_save_is_reported_not_raised():
    """Codex-находка cycle 2: trigger.nth(index).click() строки 1 падает ПОСЛЕ
    успешного save строки 0 — должно вернуться как элемент errors, а не всплыть
    исключением из _apply_rows (клик был вне try/except до этого фикса)."""
    page = FakePage(trigger_count=2, click_fails={1})
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    assert page.saved_rows == [0]
    assert len(errors) == 1
    assert "строка 1" in errors[0]


def test_trigger_count_failure_after_prior_save_is_reported_not_raised():
    """cycle 3 (advisor review): trigger.count() строки 1 падает ПОСЛЕ успешного
    save строки 0 — тот же класс необработанного исключения; count() тоже должен
    быть внутри try/except, не только click()/wait_for()."""
    page = FakePage(trigger_count=2, count_fails={1})
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    assert page.saved_rows == [0]
    assert len(errors) == 1
    assert "строка 1" in errors[0]


def test_all_rows_hydrate_and_save_without_errors():
    page = FakePage(trigger_count=2)
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False)

    assert errors == []
    assert page.saved_rows == [0, 1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# --- блок контактов (#1119): один заход в редактор, save + readback ----------

CONTACTS_URL = "https://hh.ru/resume/edit/test-resume-id/contacts"


class FakeContactField:
    """Locator одного поля формы: count/input_value/fill/first."""

    def __init__(self, page, qa: str):
        self._page = page
        self._qa = qa

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def input_value(self):
        if self._qa == self._page.readback_raises:
            raise PlaywrightTimeoutError("input_value недоступен")
        if self._page.saved:
            # readback читает значение ПОСЛЕ save — подменяем через
            # readback_values; до save поле хранит то, что ввели.
            return self._page.readback_values.get(self._qa, self._page.filled.get(self._qa, ""))
        return self._page.filled.get(self._qa, "")

    def get_attribute(self, name):  # noqa: ARG002
        return (
            "magritte-radio-input-unchecked"
            if not self._page.preferred
            else ("magritte-radio-input-checked")
        )

    def fill(self, value):
        if self._qa == "resume-phone-cell_phone" and self._page.mask_rewrite_once:
            # Боевой факт 2026-09-14: маска до гидратации «нормализует» значение
            # в CN-пример; первый fill даёт мусор, ретрай после паузы — успех.
            self._page.mask_rewrite_once = False
            self._page.filled[self._qa] = "+86 138 00-13-80-00"
            return
        self._page.filled[self._qa] = value

    def wait_for(self, *, state="visible", timeout=None):  # noqa: ARG002
        if not self._page.ready:
            raise PlaywrightTimeoutError("гидратация не завершилась вовремя")

    def click(self):
        self._page.preferred = True


class FakeContactsSave:
    def __init__(self, page):
        self._page = page

    def count(self):
        return 1

    def click(self):
        self._page.saved = True

    def wait_for(self, *, state="hidden", timeout=None):
        if self._page.save_wait_times_out:
            raise PlaywrightTimeoutError("редактор не закрылся")
        self._page.closed = True


class FakeContactsCancel:
    def __init__(self, page):
        self._page = page

    def count(self):
        return 1

    def click(self):
        self._page.cancelled = True


class FakeContactsPage:
    """Стаб формы контактов (#1119): телефон/email/комментарий, radio, save/cancel."""

    def __init__(self, *, ready: bool = True, save_wait_times_out: bool = False):
        self.url = CONTACTS_URL
        self.ready = ready
        self.save_wait_times_out = save_wait_times_out
        self.filled: dict[str, str] = {}
        self.readback_values: dict[str, str] = {}
        self.readback_raises: str = ""
        self.mask_rewrite_once: bool = False
        self.preferred = False
        self.saved = False
        self.closed = False
        self.cancelled = False

    def wait_for_timeout(self, timeout):  # noqa: ARG002
        pass

    def locator(self, selector: str):
        if selector == "[data-qa='resume-partial-edit-save']":
            return FakeContactsSave(self)
        if selector == "[data-qa='resume-partial-edit-cancel']":
            return FakeContactsCancel(self)
        qa = selector.removeprefix("[data-qa='").removesuffix("']")
        return FakeContactField(self, qa)


@pytest.fixture
def contacts_page(monkeypatch):
    """_apply_contacts ходит через goto_hh — подменяем на смену page.url."""
    page = FakeContactsPage()

    def fake_goto(page_arg, url):  # noqa: ARG001
        page_arg.url = url

    monkeypatch.setattr(resume_sections, "goto_hh", fake_goto)
    return page


def test_contacts_dry_run_fills_and_cancels_without_save(contacts_page):
    items = [
        resume_sections.Contact(type="phone", value="+7 900", comment="Вацап"),
        resume_sections.Contact(type="email", value="a@b.c"),
    ]
    outcomes = [RowOutcome("contacts", i, OUTCOME_PLANNED) for i in range(2)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=True, outcomes=outcomes
    )

    assert errors == []
    assert contacts_page.filled == {
        "resume-phone-cell_phone": "+7 900",
        "resume-editor-phone-comment-input": "Вацап",
        "resume-editor-email-input": "a@b.c",
    }
    assert contacts_page.cancelled and not contacts_page.saved
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED, OUTCOME_PLANNED]


def test_contacts_save_confirmed_by_readback(contacts_page):
    items = [
        resume_sections.Contact(type="phone", value="+7 900", comment="Вацап", preferred=True),
        resume_sections.Contact(type="email", value="a@b.c"),
    ]
    outcomes = [RowOutcome("contacts", i, OUTCOME_PLANNED) for i in range(2)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert errors == []
    assert contacts_page.saved and contacts_page.closed and contacts_page.preferred
    assert [o.status for o in outcomes] == [OUTCOME_UPDATED, OUTCOME_UPDATED]
    assert all("readback совпал" in o.reason for o in outcomes)


def test_contacts_phone_readback_tolerates_country_prefix(contacts_page):
    # Боевой факт 2026-09-14: маска хранит номер с другим кодом страны —
    # readback сравнивает национальные цифры (последние 10), не строки.
    contacts_page.readback_values = {"resume-phone-cell_phone": "8 999 000-11-22"}
    items = [resume_sections.Contact(type="phone", value="+7 999 000-11-22")]
    outcomes = [RowOutcome("contacts", 0, OUTCOME_PLANNED)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert errors == []
    assert [o.status for o in outcomes] == [OUTCOME_UPDATED]


def test_contacts_mask_rewrite_is_retried_before_save(contacts_page):
    # Боевой факт 2026-09-14: fill до гидратации маски даёт CN-мусор
    # (+86 138 00-13-80-00); фича обязана перезаполнить и сверить цифры
    # ДО клика save, иначе на hh.ru уезжает мусор.
    contacts_page.mask_rewrite_once = True
    items = [resume_sections.Contact(type="phone", value="+7 999 000-11-22")]
    outcomes = [RowOutcome("contacts", 0, OUTCOME_PLANNED)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert errors == []
    assert contacts_page.filled["resume-phone-cell_phone"] == "+7 999 000-11-22"
    assert [o.status for o in outcomes] == [OUTCOME_UPDATED]


def test_contacts_save_wait_timeout_marks_all_uncertain(contacts_page):
    # Клик save мог уйти (#176): readback не выполняется, все строки uncertain.
    contacts_page.save_wait_times_out = True
    items = [resume_sections.Contact(type="phone", value="+7 900")]
    outcomes = [RowOutcome("contacts", 0, OUTCOME_PLANNED)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert len(errors) == 1 and "uncertain" in errors[0]
    assert [o.status for o in outcomes] == [OUTCOME_UNCERTAIN]


def test_contacts_readback_mismatch_marks_rows_uncertain(contacts_page):
    contacts_page.readback_values = {"resume-phone-cell_phone": "+7 000"}
    items = [resume_sections.Contact(type="phone", value="+7 900")]
    outcomes = [RowOutcome("contacts", 0, OUTCOME_PLANNED)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert len(errors) == 1 and "uncertain" in errors[0]
    assert outcomes[0].status == OUTCOME_UNCERTAIN
    assert "ожидалось '+7 900'" in outcomes[0].reason
    assert "получено '+7 000'" in outcomes[0].reason


def test_contacts_readback_goto_timeout_marks_uncertain(contacts_page, monkeypatch):
    # cycle-review PR #1127: таймаут goto_hh при readback-переходе ПОСЛЕ
    # успешного save.click() не содержит «uncertain» в тексте, но клик мог
    # уйти — строки обязаны получить OUTCOME_UNCERTAIN по флагу past_click.
    calls = {"count": 0}

    def flaky_goto(page_arg, url):  # noqa: ARG001
        calls["count"] += 1
        if calls["count"] == 2:
            raise PlaywrightTimeoutError("goto: TLS handshake timeout")
        page_arg.url = url

    monkeypatch.setattr(resume_sections, "goto_hh", flaky_goto)
    items = [resume_sections.Contact(type="phone", value="+7 900")]
    outcomes = [RowOutcome("contacts", 0, OUTCOME_PLANNED)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert len(errors) == 1 and "uncertain" in errors[0]
    assert [o.status for o in outcomes] == [OUTCOME_UNCERTAIN]


def test_contacts_readback_error_keeps_already_updated_rows(contacts_page):
    # cycle-review PR #1127: исключение в цикле readback ПОСЛЕ зафиксированного
    # OUTCOME_UPDATED не перезаписывает готовые исходы — фейлится только
    # незавершённая (planned) строка.
    contacts_page.readback_raises = "resume-editor-email-input"
    items = [
        resume_sections.Contact(type="phone", value="+7 900"),
        resume_sections.Contact(type="email", value="a@b.c"),
    ]
    outcomes = [RowOutcome("contacts", i, OUTCOME_PLANNED) for i in range(2)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert len(errors) == 1
    # Клик save уже был → исключение в readback даёт uncertain, а не failed.
    assert [o.status for o in outcomes] == [OUTCOME_UPDATED, OUTCOME_UNCERTAIN]


def test_contacts_wrong_resume_route_fails_closed(monkeypatch):
    page = FakeContactsPage()
    page.url = "https://hh.ru/resume/edit/another-resume/contacts"
    monkeypatch.setattr(
        resume_sections, "goto_hh", lambda page_arg, _url: setattr(page_arg, "url", page_arg.url)
    )
    items = [resume_sections.Contact(type="phone", value="+7 900")]
    outcomes = [RowOutcome("contacts", 0, OUTCOME_PLANNED)]

    errors = _apply_contacts(page, "test-resume-id", items, dry_run=False, outcomes=outcomes)

    assert len(errors) == 1 and "не для того резюме" in errors[0]
    assert not page.saved
    assert [o.status for o in outcomes] == [OUTCOME_FAILED]


def test_contacts_hydration_timeout_fails_before_save(contacts_page):
    contacts_page.ready = False
    items = [resume_sections.Contact(type="phone", value="+7 900")]
    outcomes = [RowOutcome("contacts", 0, OUTCOME_PLANNED)]

    errors = _apply_contacts(
        contacts_page, "test-resume-id", items, dry_run=False, outcomes=outcomes
    )

    assert not contacts_page.saved
    assert len(errors) == 1
    assert [o.status for o in outcomes] == [OUTCOME_FAILED]


def test_apply_plan_early_exit_fails_contacts_rows(monkeypatch):
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: False)
    outcomes = {"contacts": [RowOutcome("contacts", 0, OUTCOME_PLANNED)]}

    errors = apply_plan(
        MagicMock(),
        "resume-id",
        resume_sections.ResumeSectionsPlan(
            contacts=[resume_sections.Contact(type="phone", value="+7 900")],
        ),
        dry_run=False,
        outcomes=outcomes,
    )

    assert errors == ["отсутствует auth cookie"]
    assert outcomes["contacts"][0].status == OUTCOME_FAILED


# --- per-row контракт исходов (#1118) ----------------------------------------


class FakeCancel:
    def count(self):
        return 1

    def click(self):
        pass


class DryRunPage(FakePage):
    """FakePage + подтверждённая кнопка отмены (dry-run выходит из редактора)."""

    def locator(self, selector: str):
        if selector == "[data-qa='resume-partial-edit-cancel']":
            return FakeCancel()
        return super().locator(selector)


def test_outcomes_marked_appended_then_failed_on_hydration_timeout():
    page = FakePage(trigger_count=2, ready_by_index={1: False})
    items = [Attestation("A", "Org", "Spec", "2020"), Attestation("B", "Org", "Spec", "2021")]
    outcomes = [RowOutcome("attestations", i, OUTCOME_PLANNED) for i in range(2)]

    errors = _apply_rows(page, "attestations", items, _fill_row, dry_run=False, outcomes=outcomes)

    assert page.saved_rows == [0]
    assert len(errors) == 1
    assert [o.status for o in outcomes] == [OUTCOME_APPENDED, OUTCOME_FAILED]
    assert outcomes[1].reason == "гидратация не завершилась вовремя"


def test_outcomes_stay_planned_in_dry_run():
    page = DryRunPage(trigger_count=1)
    outcomes = [RowOutcome("attestations", 0, OUTCOME_PLANNED)]

    errors = _apply_rows(
        page,
        "attestations",
        [Attestation("A", "Org", "Spec", "2020")],
        _fill_row,
        dry_run=True,
        outcomes=outcomes,
    )

    assert errors == []
    assert [o.status for o in outcomes] == [OUTCOME_PLANNED]


def test_apply_plan_marks_all_outcomes_failed_on_login_form(monkeypatch):
    """Ранний выход apply_plan честно помечает ВСЕ строки failed (#1118)."""
    monkeypatch.setattr(resume_sections, "has_auth_cookie", lambda _page: False)
    outcomes = {
        "attestations": [RowOutcome("attestations", 0, OUTCOME_PLANNED)],
        "recommendations": [RowOutcome("recommendations", 0, OUTCOME_PLANNED)],
    }

    errors = apply_plan(
        MagicMock(),
        "resume-id",
        resume_sections.ResumeSectionsPlan(
            attestations=[Attestation("A", "Org", "Spec", "2020")],
            recommendations=[Recommendation(text="", company="Acme")],
        ),
        dry_run=False,
        outcomes=outcomes,
    )

    assert errors == ["отсутствует auth cookie"]
    assert all(o.status == OUTCOME_FAILED for block in outcomes.values() for o in block)


def test_apply_plan_without_outcomes_keeps_old_contract():
    page = FakePage(trigger_count=1)

    errors = _apply_rows(
        page,
        "attestations",
        [Attestation("A", "Org", "Spec", "2020")],
        _fill_row,
        dry_run=False,
    )

    assert errors == []
    assert page.saved_rows == [0]


def test_certificate_rows_save_through_shared_apply_path():
    """#1120: сертификаты идут по тому же per-row контракту _apply_rows,
    что и attestations/recommendations — append + подтверждение close."""
    from hhru_bot.resume_sections import Certificate

    page = FakePage(trigger_count=2)
    outcomes = [
        RowOutcome("certificates", 0, OUTCOME_PLANNED),
        RowOutcome("certificates", 1, OUTCOME_PLANNED),
    ]

    errors = _apply_rows(
        page,
        "certificates",
        [Certificate("A", "2020", ""), Certificate("B", "2021", "https://x")],
        _fill_row,
        dry_run=False,
        outcomes=outcomes,
    )

    assert errors == []
    assert page.saved_rows == [0, 1]
    assert [o.status for o in outcomes] == [OUTCOME_APPENDED, OUTCOME_APPENDED]
