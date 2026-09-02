"""Unit-тесты команды upload-photo: валидация файла и поток загрузки.

Браузерный слой — фейки; селекторы из группы resume_photo подтверждены
живым DOM и боевым прогоном 2026-09-02 (см. модуль группы), здесь
проверяется только логика решений: dry-run без кликов, fail-closed отказы
до точки невозврата, порядок before_click и uncertain-исходы после
передачи файла (гидратация, редактор, назначение).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot import resume_photo
from hhru_bot.resume_photo import (
    PhotoFile,
    UploadPhotoResult,
    photo_upload_plan,
    upload_photo_on_hh,
    validate_photo,
)

pytestmark = pytest.mark.unit

JPEG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 12
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


class FakeLocator:
    def __init__(self, page, selector, count=1, visible=True):
        self._page = page
        self._selector = selector
        self._count = count
        self._visible = visible
        self.first = self

    def count(self):
        return self._count

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        if self._count == 0 or not self._visible:
            raise PlaywrightError("fake: not visible")

    def click(self, *, timeout=None):  # noqa: ARG002
        self._page.clicks.append(self._selector)
        self._page.on_click(self._selector)

    def set_input_files(self, files):
        self._page.set_files.append((self._selector, files))


class FakePage:
    """Стейт-машина боевого потока: set_files -> editor -> assign -> marker."""

    def __init__(
        self,
        *,
        avatar_count=1,
        file_input_count=1,
        hydrated=True,
        editor_visible=True,
        assign_visible=True,
        marker_after_assign=1,
    ):
        self.set_files: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.marker_count = 0
        self._avatar_count = avatar_count
        self._file_input_count = file_input_count
        self._hydrated = hydrated
        self._editor_visible = editor_visible
        self._assign_visible = assign_visible
        self._marker_after_assign = marker_after_assign
        self.url = "https://hh.ru/resume/rid"

    def on_click(self, selector):
        # assign-клик назначает фото: маркер появляется по факту клика
        if selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            self.marker_count = self._marker_after_assign

    def locator(self, selector: str):
        if selector == resume_photo.RESUME_AVATAR_BLOCK:
            return FakeLocator(self, selector, count=self._avatar_count)
        if selector == resume_photo.RESUME_PHOTO_FILE_INPUT:
            return FakeLocator(self, selector, count=self._file_input_count)
        if selector == resume_photo.RESUME_PHOTO_EDITOR_APPLY:
            return FakeLocator(self, selector, count=1, visible=self._editor_visible)
        if selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            return FakeLocator(self, selector, count=1, visible=self._assign_visible)
        if selector == resume_photo.RESUME_AVATAR_IMAGE:
            return FakeLocator(self, selector, count=self.marker_count)
        return FakeLocator(self, selector, count=0)

    def evaluate(self, script, arg=None):  # noqa: ARG002
        return None  # scrollIntoView контейнера

    def wait_for_function(self, script, *, arg=None, timeout=None):  # noqa: ARG002
        # browser.wait_for_react_hydration поллит через wait_for_function;
        # таймаут в реальном Playwright — PlaywrightError.
        if not self._hydrated:
            raise PlaywrightError("fake: hydration timeout")

    def wait_for_timeout(self, ms):  # noqa: ARG002
        pass

    def content(self) -> str:
        return "<html></html>"  # для browser.dump_page_html в uncertain-исходах


class FakeResume:
    resume_url = "https://hh.ru/resume/rid"


PHOTO = PhotoFile(path=Path("/tmp/x.jpg"), size_bytes=100, kind="jpeg")


@pytest.fixture(autouse=True)
def _no_navigation(monkeypatch):
    """goto/auth/cookie — реальный браузерный слой; в unit-тестах заглушки."""
    monkeypatch.setattr(resume_photo, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(resume_photo, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(resume_photo, "dismiss_cookie_banner", lambda page: None)


def _jpeg(tmp_path, name="photo.jpg", data=JPEG_HEAD):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _run(page, *, dry_run=False, before_click=None):
    return upload_photo_on_hh(page, FakeResume(), PHOTO, dry_run, before_click=before_click)


def test_validate_photo_accepts_jpeg_png(tmp_path):
    for name, head in (("a.jpg", JPEG_HEAD), ("b.jpeg", JPEG_HEAD), ("c.png", PNG_HEAD)):
        photo = validate_photo(_jpeg(tmp_path, name, head))
        assert photo.kind == ("png" if name.endswith("png") else "jpeg")
        assert photo.size_bytes == len(head)


def test_validate_photo_rejects_missing_and_dir(tmp_path):
    with pytest.raises(ValueError, match="не найден"):
        validate_photo(tmp_path / "nope.jpg")
    with pytest.raises(ValueError, match="директор"):
        validate_photo(tmp_path)


def test_validate_photo_rejects_bad_ext_and_empty(tmp_path):
    with pytest.raises(ValueError, match="не поддерживается"):
        validate_photo(_jpeg(tmp_path, "a.gif", b"GIF89a" + b"\x00" * 11))
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="пустой"):
        validate_photo(empty)


def test_validate_photo_rejects_oversize_and_html_as_jpg(tmp_path, monkeypatch):
    monkeypatch.setattr(resume_photo, "MAX_PHOTO_BYTES", 8)
    with pytest.raises(ValueError, match="больше"):
        validate_photo(_jpeg(tmp_path))
    monkeypatch.setattr(resume_photo, "MAX_PHOTO_BYTES", 5 * 1024 * 1024)
    with pytest.raises(ValueError, match="магическ"):
        validate_photo(_jpeg(tmp_path, "page.jpg", b"<html>" + b"\x00" * 10))


def test_plan_mentions_flow_steps(tmp_path):
    photo = validate_photo(_jpeg(tmp_path))
    plan = photo_upload_plan(photo, "rid")
    assert str(photo.path) in plan
    assert "rid" in plan
    assert str(photo.size_bytes) in plan
    assert "photo-editor-apply" in plan
    assert "photo-viewer-action-assign-current" in plan


def test_dry_run_inspects_without_clicking():
    page = FakePage()
    result = _run(page, dry_run=True, before_click=lambda: pytest.fail("click in dry-run"))
    assert result.success
    assert result.photo_present is False
    assert result.uncertain is False
    assert page.set_files == []
    assert page.clicks == []


def test_avatar_not_rendered_is_fail_without_click():
    page = FakePage(avatar_count=0)
    result = _run(page, before_click=lambda: pytest.fail("click before avatar"))
    assert not result.success
    assert not result.uncertain
    assert result.photo_present is None
    assert page.set_files == []


def test_existing_photo_is_fail_without_click():
    page = FakePage()
    page.marker_count = 1  # фото уже есть ещё до передачи файла
    result = _run(page, before_click=lambda: pytest.fail("click with existing photo"))
    assert not result.success
    assert result.photo_present is True
    assert result.uncertain is False
    assert page.set_files == []


def test_not_hydrated_is_fail_closed_without_transfer():
    page = FakePage(hydrated=False)
    result = _run(page, before_click=lambda: pytest.fail("click without hydration"))
    assert not result.success
    assert not result.uncertain
    assert result.photo_present is False
    assert page.set_files == []
    assert page.clicks == []


def test_file_input_unconfirmed_is_fail_closed():
    page = FakePage(file_input_count=0)
    result = _run(page, before_click=lambda: pytest.fail("click without input"))
    assert not result.success
    assert not result.uncertain
    assert page.set_files == []


def test_happy_path_order_and_success(monkeypatch):
    page = FakePage()
    order: list[str] = []

    def before_click():
        order.append("before_click")

    orig_set_files = FakeLocator.set_input_files

    def spy(self, files):
        order.append("set_files")
        orig_set_files(self, files)

    monkeypatch.setattr(FakeLocator, "set_input_files", spy)
    result = _run(page, before_click=before_click)
    assert result.success
    assert result.photo_present is True
    # первая мутация — set_input_files, потом редактор, потом назначение
    assert order == ["before_click", "set_files"]
    assert page.clicks == [
        resume_photo.RESUME_PHOTO_EDITOR_APPLY,
        resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
    ]
    assert page.set_files == [(resume_photo.RESUME_PHOTO_FILE_INPUT, str(PHOTO.path))]


def test_playwright_error_on_transfer_is_uncertain(monkeypatch):
    def boom(self, files):  # noqa: ARG002
        raise PlaywrightError("fake transfer error")

    monkeypatch.setattr(FakeLocator, "set_input_files", boom)
    result = _run(FakePage(), before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True


def test_editor_missing_after_transfer_is_uncertain():
    page = FakePage(editor_visible=False)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert len(page.set_files) == 1  # файл уже передан


def test_editor_click_failure_is_uncertain(monkeypatch):
    def boom(self, *, timeout=None):  # noqa: ARG002
        raise PlaywrightError("fake editor click failed")

    monkeypatch.setattr(FakeLocator, "click", boom)
    result = _run(FakePage(), before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True


def test_assign_missing_after_apply_is_uncertain():
    page = FakePage(assign_visible=False)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert page.clicks == [resume_photo.RESUME_PHOTO_EDITOR_APPLY]


def test_assign_click_failure_is_uncertain(monkeypatch):
    page = FakePage()
    orig_click = FakeLocator.click

    def failing_click(self, *, timeout=None):  # noqa: ARG002
        if self._selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            raise PlaywrightError("fake assign click failed")
        orig_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", failing_click)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True


def test_marker_missing_after_assign_is_uncertain(monkeypatch):
    monkeypatch.setattr(resume_photo, "_CONFIRM_TIMEOUT_MS", 1)
    page = FakePage(marker_after_assign=0)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert result.photo_present is None
    assert len(page.set_files) == 1


def test_upload_result_defaults():
    result = UploadPhotoResult()
    assert result.success is False
    assert result.uncertain is False
    assert result.photo_present is None
