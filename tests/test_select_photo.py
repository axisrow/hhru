"""Unit-тесты select-photo: выбор фото из библиотеки (#953).

Браузерный слой — фейки; селекторы подтверждены живым read-only дампом
2026-09-04 и боевым дампом 2026-09-02 (см. selector_groups/resume_photo).
Проверяется логика решений: dry-run без мутаций, fail-closed до точки
невозврата, порядок before_click (только перед assign-кликом), тройное
доказательство идентичности фото (лента -> слайд -> аватар после
перезагрузки).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot import resume_photo
from hhru_bot.resume_photo import (
    LibraryPhoto,
    SelectPhotoResult,
    ViewerState,
    parse_library_photos,
    parse_photo_id,
    select_photo_on_hh,
)

pytestmark = pytest.mark.unit


class FakeThumbLocator:
    def __init__(self, page):
        self._page = page

    def nth(self, index: int):
        self._page.thumb_clicks.append(index)
        self._page.on_thumb_click(index)
        return self

    def click(self, *, timeout=None):  # noqa: ARG002
        pass


class FakeLocator:
    def __init__(self, page, selector, visible=True):
        self._page = page
        self._selector = selector
        self._visible = visible
        self.first = self

    def nth(self, index: int):  # noqa: ARG002
        return self

    def locator(self, selector: str):  # скоупленный локатор (root -> thumbs)
        return self._page.locator(selector)

    def count(self):
        return 1 if self._visible else 0

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        if not self._visible:
            raise PlaywrightError("fake: not visible")

    def click(self, *, timeout=None):  # noqa: ARG002
        self._page.clicks.append(self._selector)
        self._page.on_click(self._selector)

    def get_attribute(self, name: str):  # noqa: ARG002
        if len(self._page.navigations) >= 2:
            return self._page.reloaded_avatar_src
        return self._page.avatar_src


class SelectFakePage:
    """Стейт-машина карандашного потока select-photo.

    ``viewer_states`` — последовательные ответы page.evaluate (до и после
    клика по миниатюре); ``avatar_src`` — src аватара после перезагрузки.
    """

    def __init__(
        self,
        *,
        avatar_visible=True,
        hydrated=True,
        viewer_visible=True,
        viewer_states=None,
        assign_visible=True,
        avatar_src=None,
        reloaded_avatar_src=None,
    ):
        self.clicks: list[str] = []
        self.thumb_clicks: list[int] = []
        self.navigations: list[str] = []
        self.avatar_src = avatar_src  # оптимистичный маркер после assign
        self.reloaded_avatar_src = reloaded_avatar_src  # серверное состояние
        self._states = list(viewer_states or [])
        self._avatar_visible = avatar_visible
        self._hydrated = hydrated
        self._viewer_visible = viewer_visible
        self._assign_visible = assign_visible

    def on_thumb_click(self, index: int):
        pass

    def on_click(self, selector: str):
        pass

    def locator(self, selector: str):
        if selector == resume_photo.RESUME_AVATAR_BLOCK:
            return FakeLocator(self, selector, visible=self._avatar_visible)
        if selector == resume_photo.RESUME_AVATAR_EDIT_BUTTON:
            return FakeLocator(self, selector, visible=self._avatar_visible)
        if selector == resume_photo.RESUME_PHOTO_VIEWER_ROOT:
            return FakeLocator(self, selector, visible=self._viewer_visible)
        if selector == resume_photo.RESUME_PHOTO_VIEWER_THUMBNAILS:
            return FakeThumbLocator(self)
        if selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            return FakeLocator(self, selector, visible=self._assign_visible)
        if selector == resume_photo.RESUME_AVATAR_IMAGE:
            return FakeLocator(self, selector, visible=self.avatar_src is not None)
        return FakeLocator(self, selector, visible=False)

    def evaluate(self, script, arg=None):  # noqa: ARG002
        if "scrollIntoView" in script:
            return None  # скролл контейнера MFE
        if not self._states:
            return None
        return self._states.pop(0)

    def wait_for_function(self, script, *, arg=None, timeout=None):  # noqa: ARG002
        if not self._hydrated:
            raise PlaywrightError("fake: hydration timeout")

    def wait_for_timeout(self, ms):  # noqa: ARG002
        pass


class FakeResume:
    id = "00001"
    resume_url = "https://hh.ru/resume/00001"


def _state(photo_ids, index=1, total=None, assigned=False):
    return {
        "photos": [
            {"photoId": pid, "src": f"https://img.hhcdn.ru/photo/{pid}.jpeg?x=1"}
            for pid in photo_ids
        ],
        "index": index,
        "total": total or len(photo_ids),
        "assigned": assigned,
    }


PHOTOS = ["100", "200", "300"]


@pytest.fixture(autouse=True)
def _no_navigation_and_dumps(monkeypatch):
    monkeypatch.setattr(resume_photo, "goto_hh", lambda page, url: page.navigations.append(url))
    monkeypatch.setattr(resume_photo, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(resume_photo, "dismiss_cookie_banner", lambda page: None)
    monkeypatch.setattr(resume_photo, "dump_page_html", lambda page, stem: None)


def test_parse_photo_id_extracts_numeric_id():
    assert parse_photo_id("https://img.hhcdn.ru/photo/637550758.jpeg?t=1&h=x") == "637550758"
    assert parse_photo_id("/photo/912940449.jpeg?t=1") == "912940449"
    assert parse_photo_id("https://i.hh.ru/banner.svg") is None


def test_parse_library_photos_dedupes_by_id_keeping_order():
    items = [
        {"photoId": "200", "src": "a"},
        {"photoId": "100", "src": "b"},
        {"photoId": "200", "src": "c"},  # дубликат кропа
        {"photoId": "", "src": "d"},  # мусор без id
    ]
    photos = parse_library_photos(items)
    assert photos == (
        LibraryPhoto(photo_id="200", src="a"),
        LibraryPhoto(photo_id="100", src="b"),
    )


def test_dry_run_lists_photos_without_assign_click():
    page = SelectFakePage(viewer_states=[_state(PHOTOS)])
    result = select_photo_on_hh(page, FakeResume(), None, True)
    assert result.success
    assert [p.photo_id for p in result.photos] == PHOTOS
    assert page.clicks == [resume_photo.RESUME_AVATAR_EDIT_BUTTON]
    assert page.thumb_clicks == []
    assert result.assigned_photo_id is None


def test_viewer_not_opened_is_fail_without_mutation():
    page = SelectFakePage(viewer_visible=False)
    result = select_photo_on_hh(page, FakeResume(), "100", False, before_click=lambda: None)
    assert not result.success
    assert not result.uncertain
    assert page.thumb_clicks == []
    assert page.clicks == [resume_photo.RESUME_AVATAR_EDIT_BUTTON]


def test_not_hydrated_is_fail_without_pencil_click():
    page = SelectFakePage(hydrated=False)
    result = select_photo_on_hh(page, FakeResume(), "100", False)
    assert not result.success
    assert page.clicks == []


def test_unknown_photo_id_is_fail_closed_before_click():
    page = SelectFakePage(viewer_states=[_state(PHOTOS)])
    result = select_photo_on_hh(page, FakeResume(), "999", False, before_click=lambda: None)
    assert not result.success
    assert not result.uncertain
    assert page.thumb_clicks == []


def test_empty_library_is_fail_closed():
    page = SelectFakePage(viewer_states=[_state([])])
    result = select_photo_on_hh(page, FakeResume(), "100", False, before_click=lambda: None)
    assert not result.success
    assert page.thumb_clicks == []


def test_slide_not_confirmed_after_thumb_click_is_fail_closed():
    # После клика по миниатюре слайдер остался на другом фото (index=2 -> 200)
    page = SelectFakePage(
        viewer_states=[_state(PHOTOS, index=1), _state(PHOTOS, index=2)],
    )
    result = select_photo_on_hh(page, FakeResume(), "300", False, before_click=lambda: None)
    assert not result.success
    assert not result.uncertain
    assert page.thumb_clicks == [2]  # «300» стоит третьим в ленте
    assert resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT not in page.clicks


def test_already_assigned_is_success_without_mutation():
    page = SelectFakePage(
        viewer_states=[_state(PHOTOS, index=3), _state(PHOTOS, index=3, assigned=True)],
    )
    result = select_photo_on_hh(page, FakeResume(), "300", False, before_click=lambda: None)
    assert result.success
    assert result.assigned_photo_id == "300"
    assert resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT not in page.clicks


def test_happy_path_click_order_and_server_confirm():
    page = SelectFakePage(
        viewer_states=[_state(PHOTOS, index=1), _state(PHOTOS, index=2, assigned=False)],
        avatar_src="https://img.hhcdn.ru/photo/200.jpeg?t=9&h=y",
        reloaded_avatar_src="https://img.hhcdn.ru/photo/200.jpeg?t=9&h=y",
    )
    order: list[str] = []
    result = select_photo_on_hh(
        page,
        FakeResume(),
        "200",
        False,
        before_click=lambda: order.append("before_click"),
    )
    assert result.success
    assert result.assigned_photo_id == "200"
    # единственная мутация — клик assign-current; before_click строго перед ним
    assert page.thumb_clicks == [1]
    assert page.clicks == [
        resume_photo.RESUME_AVATAR_EDIT_BUTTON,
        resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
    ]
    assert order == ["before_click"]
    # серверная сверка — перезагрузкой страницы резюме
    assert page.navigations == [FakeResume.resume_url, FakeResume.resume_url]


def test_assign_click_failure_is_uncertain(monkeypatch):
    page = SelectFakePage(
        viewer_states=[_state(PHOTOS), _state(PHOTOS, index=2)],
        avatar_src="https://img.hhcdn.ru/photo/200.jpeg?x=1",
    )

    def boom(self, *, timeout=None):  # noqa: ARG002
        if self._selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            raise PlaywrightError("fake assign intercepted")
        orig_click(self, timeout=timeout)

    orig_click = FakeLocator.click
    monkeypatch.setattr(FakeLocator, "click", boom)
    result = select_photo_on_hh(page, FakeResume(), "200", False, before_click=lambda: None)
    assert not result.success
    assert result.uncertain


def test_marker_missing_after_assign_is_uncertain(monkeypatch):
    monkeypatch.setattr(resume_photo, "_CONFIRM_TIMEOUT_MS", 1)
    page = SelectFakePage(
        viewer_states=[_state(PHOTOS), _state(PHOTOS, index=2)],
        avatar_src=None,  # маркер не появился
    )
    result = select_photo_on_hh(page, FakeResume(), "200", False, before_click=lambda: None)
    assert not result.success
    assert result.uncertain


def test_reload_confirms_different_photo_is_uncertain():
    page = SelectFakePage(
        viewer_states=[_state(PHOTOS), _state(PHOTOS, index=2)],
        avatar_src="https://img.hhcdn.ru/photo/200.jpeg?x=1",  # оптимистичный маркер
        reloaded_avatar_src="https://img.hhcdn.ru/photo/300.jpeg?x=1",  # назначилось другое!
    )
    result = select_photo_on_hh(page, FakeResume(), "200", False, before_click=lambda: None)
    assert not result.success
    assert result.uncertain


def test_reload_without_avatar_is_uncertain():
    page = SelectFakePage(
        viewer_states=[_state(PHOTOS), _state(PHOTOS, index=2)],
        avatar_src="https://img.hhcdn.ru/photo/200.jpeg?x=1",
        reloaded_avatar_src=None,  # после перезагрузки фото не подтвердилось
    )
    result = select_photo_on_hh(page, FakeResume(), "200", False, before_click=lambda: None)
    assert not result.success
    assert result.uncertain


def test_select_photo_result_defaults():
    result = SelectPhotoResult()
    assert result.success is False
    assert result.uncertain is False
    assert result.photos == ()
    assert result.assigned_photo_id is None


def test_viewer_state_helpers():
    state = ViewerState(
        photos=(LibraryPhoto("1", "s"),), thumb_ids=("1",), index=None, total=None, assigned=False
    )
    assert resume_photo._current_photo_id(state) is None
    state = ViewerState(
        photos=(LibraryPhoto("1", "s"), LibraryPhoto("2", "t")),
        thumb_ids=("1", "2"),
        index=2,
        total=2,
        assigned=True,
    )
    assert resume_photo._current_photo_id(state) == "2"


def test_duplicate_id_in_strip_clicks_raw_index():
    # Тот же id дважды в ленте (разные query-кропы): дедуп — для вывода,
    # nth() обязан адресовать СЫРОЙ порядок (ревью #967).
    raw = ["100", "200", "100", "300"]
    page = SelectFakePage(
        viewer_states=[_state(raw, index=1), _state(raw, index=1, assigned=False)],
        avatar_src="https://img.hhcdn.ru/photo/100.jpeg?x=1",
        reloaded_avatar_src="https://img.hhcdn.ru/photo/100.jpeg?x=1",
    )
    result = select_photo_on_hh(page, FakeResume(), "100", False, before_click=lambda: None)
    assert result.success
    assert result.assigned_photo_id == "100"
    assert page.thumb_clicks == [0]  # первое вхождение сырого порядка, не дедупа


def test_assign_button_missing_is_fail_closed_without_uncertain(monkeypatch):
    # До before_click мутации нет: отказ обязан быть чистым, не uncertain.
    # Поздний авто-assign — success no-op по перечитанному маркеру.
    page = SelectFakePage(
        viewer_states=[
            _state(PHOTOS, index=2),
            _state(PHOTOS, index=2),
            _state(PHOTOS, index=2, assigned=True),  # перечитанный маркер
        ],
        assign_visible=False,
    )
    result = select_photo_on_hh(page, FakeResume(), "200", False, before_click=lambda: None)
    assert result.success
    assert result.assigned_photo_id == "200"


def test_assign_button_missing_without_late_assign_is_plain_fail():
    page = SelectFakePage(
        viewer_states=[
            _state(PHOTOS, index=2),
            _state(PHOTOS, index=2),
            _state(PHOTOS, index=2, assigned=False),  # перечитанный маркер
        ],
        assign_visible=False,
    )
    result = select_photo_on_hh(page, FakeResume(), "200", False, before_click=lambda: None)
    assert not result.success
    assert not result.uncertain
    assert resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT not in page.clicks


def test_variant_sibling_classification():
    from hhru_bot.resume_photo import _variant_sibling

    state = ViewerState(
        photos=(LibraryPhoto("100", "a"), LibraryPhoto("200", "b")),
        thumb_ids=("100", "200"),
        index=1,
        total=2,
        assigned=False,
    )
    assert _variant_sibling("100", "100", state) == "same"
    # боевой кейс 2026-09-04: назначен канонический СОСЕДНИЙ СТАРШИЙ id
    assert _variant_sibling("101", "100", state) == "sibling"
    # направление −1 не принято (ложный sibling при подряд выделенных парах)
    assert _variant_sibling("99", "100", state) == "other"
    # соседний id, но сам присутствует в ленте как отдельное фото — не вариант
    state_with_101 = ViewerState(
        photos=(LibraryPhoto("100", "a"), LibraryPhoto("101", "b")),
        thumb_ids=("100", "101"),
        index=1,
        total=2,
        assigned=False,
    )
    assert _variant_sibling("101", "100", state_with_101) == "other"
    # далёкий id — чужое фото
    assert _variant_sibling("500", "100", state) == "other"
    assert _variant_sibling(None, "100", state) == "other"
