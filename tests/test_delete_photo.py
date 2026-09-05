"""Unit-тесты delete-photo: скрытие из резюме и удаление из библиотеки (#966).

Браузерный слой — фейки; селекторы подтверждены живым read-only DOM
2026-09-05 (дампы photo_more_menu_* / photo_delete_confirm_*, см.
selector_groups/resume_photo). Проверяется логика решений: dry-run без
единого клика по пунктам меню, fail-closed до точки невозврата, позиция
before_click (перед hide-кликом / перед confirm-кликом — у них РАЗНЫЕ
точки невозврата), readback-классификация исходов.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot import delete_photo, resume_photo
from hhru_bot.delete_photo import (
    DELETE_ACTION,
    HIDE_ACTION,
    MenuAction,
    delete_photo_on_hh,
    delete_photo_plan,
)
from hhru_bot.resume_photo import ViewerState

pytestmark = pytest.mark.unit


class FakeThumbLocator:
    def __init__(self, page):
        self._page = page

    def nth(self, index: int):
        self._page.thumb_clicks.append(index)
        self._page.slide = index + 1  # сырой индекс ленты -> 1-based слайдер
        return self

    def click(self, *, timeout=None):  # noqa: ARG002
        pass


class FakeLocator:
    def __init__(self, page, selector, visible=True, count=None):
        self._page = page
        self._selector = selector
        self._visible = visible
        self._count = count
        self.first = self

    def nth(self, index: int):  # noqa: ARG002
        return self

    def locator(self, selector: str):
        return self._page.locator(selector)

    def count(self):
        if self._count is not None:
            return self._count
        return 1 if self._visible else 0

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        visible = self._visible
        if state == "detached":
            visible = not visible
        if not visible:
            raise PlaywrightError("fake: state not reached")

    def click(self, *, timeout=None):  # noqa: ARG002
        self._page.clicks.append(self._selector)
        self._page.on_click(self._selector)

    def dispatch_event(self, event: str):  # noqa: ARG002
        self._page.clicks.append(f"{self._selector}:dispatch")

    def get_attribute(self, name: str):  # noqa: ARG002
        return None


PHOTOS = ["100", "200", "300"]


class DeleteFakePage:
    """Реактивная модель карандашного потока delete-photo.

    ``library`` — id фото ленты; ``slide`` — текущий слайд (1-based);
    ``assigned_photo_id`` — id фото, назначенного этому резюме (маркер
    «Установлено в резюме» + наличие пункта hide); ``avatar_after_reload`` —
    состояние аватара на перечитанной странице (readback hide);
    ``confirm_dialog_opens`` — открылся ли confirm-диалог после клика по
    пункту «Удалить»; ``keep_photo_in_library`` — confirm не убрал photo_id
    из ленты readback (uncertain-исход удаления).
    """

    def __init__(
        self,
        *,
        library=None,
        assigned_photo_id=None,
        hydrated=True,
        hydrated_after_reload=True,
        menu_opens=True,
        hide_in_menu=True,
        confirm_dialog_opens=True,
        confirm_count=1,
        keep_photo_in_library=False,
        avatar_after_reload="placeholder",
        photo_id=None,
    ):
        self.library = list(library if library is not None else PHOTOS)
        self.assigned_photo_id = assigned_photo_id
        self.hydrated = hydrated
        self.hydrated_after_reload = hydrated_after_reload
        self.menu_opens = menu_opens
        self.hide_in_menu = hide_in_menu
        self.confirm_dialog_opens = confirm_dialog_opens
        self.confirm_count = confirm_count
        self.keep_photo_in_library = keep_photo_in_library
        self.avatar_after_reload = avatar_after_reload
        self.photo_id = photo_id
        self.clicks: list[str] = []
        self.thumb_clicks: list[int] = []
        self.navigations: list[str] = []
        self.slide = 1
        self.viewer_open = False
        self.menu_open = False
        self.dialog_open = False
        self.hide_clicked = False
        self.confirm_clicked = False

    @property
    def _current_id(self):
        if 0 < self.slide <= len(self.library):
            return self.library[self.slide - 1]
        return None

    @property
    def _assigned_current(self):
        return self._current_id is not None and self._current_id == self.assigned_photo_id

    def on_click(self, selector: str):
        if selector == resume_photo.RESUME_AVATAR_EDIT_BUTTON:
            self.viewer_open = True
        elif selector == delete_photo.RESUME_PHOTO_VIEWER_MORE:
            self.menu_open = self.menu_opens
        elif selector == delete_photo.RESUME_PHOTO_VIEWER_ACTION_DELETE:
            self.dialog_open = self.confirm_dialog_opens
        elif selector == delete_photo.RESUME_PHOTO_VIEWER_ACTION_HIDE:
            self.hide_clicked = True
        elif selector == delete_photo.RESUME_PHOTO_VIEWER_DELETE_CONFIRM:
            self.confirm_clicked = True
            self.dialog_open = False
            if not self.keep_photo_in_library and self.photo_id in self.library:
                self.library.remove(self.photo_id)

    def locator(self, selector: str):
        if selector == resume_photo.RESUME_PHOTO_VIEWER_THUMBNAILS:
            return FakeThumbLocator(self)
        if selector == delete_photo.RESUME_PHOTO_VIEWER_MORE:
            return FakeLocator(self, selector, visible=self.viewer_open)
        if selector == delete_photo.RESUME_PHOTO_VIEWER_ACTION_DELETE:
            return FakeLocator(self, selector, visible=self.menu_open)
        if selector == delete_photo.RESUME_PHOTO_VIEWER_ACTION_HIDE:
            return FakeLocator(
                self,
                selector,
                visible=self.menu_open and self._assigned_current and self.hide_in_menu,
            )
        if selector == delete_photo.RESUME_PHOTO_VIEWER_DELETE_DIALOG:
            return FakeLocator(self, selector, visible=self.dialog_open)
        if selector == delete_photo.RESUME_PHOTO_VIEWER_DELETE_CONFIRM:
            return FakeLocator(
                self,
                selector,
                visible=self.dialog_open,
                count=self.confirm_count if self.dialog_open else 0,
            )
        if selector == resume_photo.RESUME_AVATAR_IMAGE:
            if len(self.navigations) >= 2:
                return FakeLocator(self, selector, visible=self.avatar_after_reload == "photo")
            return FakeLocator(self, selector, visible=self._assigned_current)
        if selector == resume_photo.RESUME_AVATAR_PLACEHOLDER:
            if len(self.navigations) >= 2:
                return FakeLocator(
                    self, selector, visible=self.avatar_after_reload == "placeholder"
                )
            return FakeLocator(self, selector, visible=False)
        return FakeLocator(self, selector, visible=True)

    def evaluate(self, script, arg=None):  # noqa: ARG002
        if "scrollIntoView" in script:
            return None
        if "magritte-media-viewer" in script:
            photos = [
                {"photoId": pid, "src": f"https://img.hhcdn.ru/photo/{pid}.jpeg?x=1"}
                for pid in self.library
            ]
            return {
                "photos": photos,
                "index": self.slide,
                "total": len(self.library),
                "assigned": self._assigned_current,
            }
        if "photo-viewer-action-" in script:
            if not self.menu_open:
                return []
            items = [
                {"qa": "photo-viewer-action-add", "text": "Добавить новое фото"},
                {"qa": "photo-viewer-action-download", "text": "Скачать"},
                {"qa": "photo-viewer-action-delete", "text": "Удалить"},
            ]
            if self._assigned_current:
                items.append({"qa": "photo-viewer-action-assigned", "text": "Установлено в резюме"})
                if self.hide_in_menu:
                    items.append(
                        {"qa": "photo-viewer-action-hide", "text": "Скрыть фото из резюме"}
                    )
            else:
                items.append(
                    {
                        "qa": "photo-viewer-action-assign-current",
                        "text": "Установить для этого резюме",
                    }
                )
            return items
        return None

    def wait_for_function(self, script, *, arg=None, timeout=None):  # noqa: ARG002
        if len(self.navigations) >= 2 and not self.hydrated_after_reload:
            raise PlaywrightError("fake: hydration timeout")
        if not self.hydrated:
            raise PlaywrightError("fake: hydration timeout")

    def wait_for_timeout(self, ms):  # noqa: ARG002
        # switch_viewer_photo поллит состояние после клика миниатюры: фейк
        # применяет смену слайда сразу после клика (см. FakeThumbLocator).
        return


class FakeResume:
    id = "00001"
    resume_url = "https://hh.ru/resume/00001"


@pytest.fixture(autouse=True)
def _no_navigation_and_dumps(monkeypatch):
    monkeypatch.setattr(resume_photo, "goto_hh", lambda page, url: page.navigations.append(url))
    monkeypatch.setattr(resume_photo, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(resume_photo, "dismiss_cookie_banner", lambda page: None)
    monkeypatch.setattr(resume_photo, "dump_page_html", lambda page, stem: None)
    monkeypatch.setattr(delete_photo, "goto_hh", lambda page, url: page.navigations.append(url))
    monkeypatch.setattr(delete_photo, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(delete_photo, "dismiss_cookie_banner", lambda page: None)
    monkeypatch.setattr(delete_photo, "dump_page_html", lambda page, stem: None)


def test_delete_photo_plan_mentions_scope_of_both_modes():
    hide_plan = delete_photo_plan("00001", "100", False)
    delete_plan = delete_photo_plan("00001", "100", True)
    assert "Скрыть фото из резюме" in hide_plan
    assert "остаётся в библиотеке" in hide_plan
    assert "Удалить фото" in delete_plan
    assert "ВСЕХ резюме" in delete_plan


def test_dry_run_lists_library_and_menu_without_item_clicks():
    page = DeleteFakePage(assigned_photo_id="100")
    result = delete_photo_on_hh(page, FakeResume(), None, False, True)
    assert result.success
    assert result.action == HIDE_ACTION
    assert [p.photo_id for p in result.photos] == PHOTOS
    assert page.clicks == [
        resume_photo.RESUME_AVATAR_EDIT_BUTTON,
        delete_photo.RESUME_PHOTO_VIEWER_MORE,
    ]
    assert page.thumb_clicks == []
    qas = {item.qa for item in result.menu_actions}
    assert "photo-viewer-action-delete" in qas
    assert "photo-viewer-action-hide" in qas
    assert not page.hide_clicked and not page.confirm_clicked


def test_dry_run_with_photo_id_switches_slide():
    page = DeleteFakePage(assigned_photo_id="200")
    result = delete_photo_on_hh(page, FakeResume(), "200", True, True)
    assert result.success
    assert result.action == DELETE_ACTION
    assert page.thumb_clicks == [1]
    assert not page.hide_clicked and not page.confirm_clicked


def test_battle_without_photo_id_is_fail():
    page = DeleteFakePage()
    result = delete_photo_on_hh(page, FakeResume(), None, False, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "боевой режим требует --photo-id" in result.reason
    assert delete_photo.RESUME_PHOTO_VIEWER_MORE not in page.clicks


def test_unknown_photo_id_is_fail_closed_before_menu():
    page = DeleteFakePage()
    result = delete_photo_on_hh(page, FakeResume(), "999", False, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "не подтверждён в ленте" in result.reason
    assert page.thumb_clicks == []
    assert delete_photo.RESUME_PHOTO_VIEWER_MORE not in page.clicks


def test_slide_not_confirmed_is_fail_closed(monkeypatch):
    page = DeleteFakePage()

    def stalling_nth(self, index):
        self._page.slide = 1  # слайдер не подтвердил переключение
        return self

    monkeypatch.setattr(FakeThumbLocator, "nth", stalling_nth)
    result = delete_photo_on_hh(page, FakeResume(), "300", False, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "слайдер не подтвердил" in result.reason
    assert delete_photo.RESUME_PHOTO_VIEWER_MORE not in page.clicks


def test_not_hydrated_is_fail_without_any_click():
    page = DeleteFakePage(hydrated=False)
    result = delete_photo_on_hh(page, FakeResume(), "100", False, False)
    assert not result.success
    assert page.clicks == []


def test_hide_when_not_assigned_is_fail_closed_before_menu():
    page = DeleteFakePage(assigned_photo_id=None)  # целевое фото не назначено
    result = delete_photo_on_hh(page, FakeResume(), "100", False, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "не назначен резюме" in result.reason
    assert delete_photo.RESUME_PHOTO_VIEWER_MORE not in page.clicks
    assert not page.hide_clicked


def test_hide_menu_item_missing_is_fail_closed():
    page = DeleteFakePage(assigned_photo_id="100", hide_in_menu=False)
    result = delete_photo_on_hh(page, FakeResume(), "100", False, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "пункт «Скрыть фото из резюме» не подтверждён" in result.reason
    assert not page.hide_clicked


def test_hide_happy_path_click_order_and_readback():
    page = DeleteFakePage(
        assigned_photo_id="100", avatar_after_reload="placeholder", photo_id="100"
    )
    order: list[str] = []
    result = delete_photo_on_hh(
        page, FakeResume(), "100", False, False, before_click=lambda: order.append("before_click")
    )
    assert result.success
    assert result.action == HIDE_ACTION
    assert page.clicks == [
        resume_photo.RESUME_AVATAR_EDIT_BUTTON,
        delete_photo.RESUME_PHOTO_VIEWER_MORE,
        delete_photo.RESUME_PHOTO_VIEWER_ACTION_HIDE,
    ]
    assert order == ["before_click"]  # строго до единственного мутирующего клика
    assert page.hide_clicked
    # readback: страница резюме перечитана
    assert page.navigations == [FakeResume.resume_url, FakeResume.resume_url]


def test_hide_click_failure_is_uncertain(monkeypatch):
    page = DeleteFakePage(assigned_photo_id="100", photo_id="100")
    original_click = FakeLocator.click

    def failing_click(self, *, timeout=None):
        if self._selector == delete_photo.RESUME_PHOTO_VIEWER_ACTION_HIDE:
            raise PlaywrightError("boom")
        return original_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", failing_click)
    result = delete_photo_on_hh(page, FakeResume(), "100", False, False, before_click=lambda: None)
    assert not result.success and result.uncertain
    assert "мог уйти" in result.reason


def test_hide_readback_photo_still_present_is_uncertain():
    page = DeleteFakePage(assigned_photo_id="100", avatar_after_reload="photo", photo_id="100")
    result = delete_photo_on_hh(page, FakeResume(), "100", False, False, before_click=lambda: None)
    assert not result.success and result.uncertain
    assert "readback" in result.reason


def test_hide_readback_indeterminate_is_uncertain(monkeypatch):
    monkeypatch.setattr(resume_photo, "_READBACK_CONFIRM_TIMEOUT_MS", 300)
    page = DeleteFakePage(assigned_photo_id="100", avatar_after_reload="none", photo_id="100")
    result = delete_photo_on_hh(page, FakeResume(), "100", False, False, before_click=lambda: None)
    assert not result.success and result.uncertain
    assert "readback не выполнен" in result.reason


def test_delete_happy_path_click_order_and_readback():
    page = DeleteFakePage(assigned_photo_id="100", photo_id="100")
    order: list[str] = []
    result = delete_photo_on_hh(
        page, FakeResume(), "100", True, False, before_click=lambda: order.append("before_click")
    )
    assert result.success
    assert result.action == DELETE_ACTION
    assert page.clicks == [
        resume_photo.RESUME_AVATAR_EDIT_BUTTON,
        delete_photo.RESUME_PHOTO_VIEWER_MORE,
        delete_photo.RESUME_PHOTO_VIEWER_ACTION_DELETE,
        delete_photo.RESUME_PHOTO_VIEWER_DELETE_CONFIRM,
        # readback переоткрывает вьюер карандашом для чтения ленты
        resume_photo.RESUME_AVATAR_EDIT_BUTTON,
    ]
    assert order == ["before_click"]  # строго до confirm-клика; пункт меню — до него
    assert page.confirm_clicked
    assert "100" not in page.library  # readback-лента без удалённого фото
    assert "удалено из библиотеки" in result.reason


def test_more_click_timeout_but_menu_opened_skips_dispatch(monkeypatch):
    # Находка cycle-review PR #973: клик more, упавший по таймауту, мог
    # дойти с опозданием — повторная активация закрыла бы открывшуюся
    # панель (toggle). Фикс: панель уже открыта → dispatch не отправляется.
    page = DeleteFakePage(assigned_photo_id="100")
    original_click = FakeLocator.click

    def late_click(self, *, timeout=None):
        if self._selector == delete_photo.RESUME_PHOTO_VIEWER_MORE:
            original_click(self, timeout=timeout)  # клик дошёл: панель открылась
            raise PlaywrightError("fake: click timeout")  # ...но Playwright упал
        return original_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", late_click)
    result = delete_photo_on_hh(page, FakeResume(), None, False, True)
    assert result.success
    # ровно один клик по more: без закрывающего dispatch-fолбэка
    assert page.clicks.count(delete_photo.RESUME_PHOTO_VIEWER_MORE) == 1
    assert not any(c.endswith(":dispatch") for c in page.clicks)


def test_delete_dialog_not_opened_is_plain_fail():
    # Клик по пункту меню немутирующий (живой факт 2026-09-05) — повтор
    # разрешён без reconciliation: чистый fail, не uncertain.
    page = DeleteFakePage(confirm_dialog_opens=False)
    result = delete_photo_on_hh(page, FakeResume(), "100", True, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "confirm-диалог удаления не открылся" in result.reason
    assert not page.confirm_clicked


def test_delete_confirm_ambiguous_is_fail_closed():
    page = DeleteFakePage(confirm_count=2)
    result = delete_photo_on_hh(page, FakeResume(), "100", True, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "не подтверждена однозначно" in result.reason
    assert not page.confirm_clicked


def test_delete_readback_photo_still_in_library_is_uncertain():
    page = DeleteFakePage(keep_photo_in_library=True, photo_id="100")
    result = delete_photo_on_hh(page, FakeResume(), "100", True, False, before_click=lambda: None)
    assert not result.success and result.uncertain
    assert "всё ещё в библиотеке" in result.reason


def test_delete_readback_viewer_reopen_fails_is_uncertain():
    page = DeleteFakePage(hydrated_after_reload=False, photo_id="100")
    result = delete_photo_on_hh(page, FakeResume(), "100", True, False, before_click=lambda: None)
    assert not result.success and result.uncertain
    assert "не переоткрыт" in result.reason


def test_more_menu_not_opened_is_fail():
    page = DeleteFakePage(assigned_photo_id="100", menu_opens=False)
    result = delete_photo_on_hh(page, FakeResume(), "100", False, False, before_click=lambda: None)
    assert not result.success and not result.uncertain
    assert "more-меню не открылось" in result.reason
    assert not page.hide_clicked


def test_menu_action_dataclass_holds_identity():
    item = MenuAction(qa="photo-viewer-action-delete", text="Удалить")
    assert (item.qa, item.text) == ("photo-viewer-action-delete", "Удалить")


def test_viewer_state_from_fake_is_parseable():
    # Smoke: JS-инвентарь фейка совместим с ViewerState (контракт resume_photo).
    page = DeleteFakePage(assigned_photo_id="100")
    raw = page.evaluate(resume_photo._VIEWER_STATE_JS)
    state = ViewerState(
        photos=resume_photo.parse_library_photos(raw.get("photos", [])),
        thumb_ids=tuple(str(i.get("photoId", "")) for i in raw.get("photos", [])),
        index=raw.get("index"),
        total=raw.get("total"),
        assigned=bool(raw.get("assigned")),
    )
    assert state.thumb_ids == ("100", "200", "300")
    assert state.assigned
