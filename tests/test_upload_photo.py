"""Unit-тесты команды upload-photo: валидация файла и поток загрузки.

Браузерный слой — фейки; селекторы из группы resume_photo подтверждены
живым DOM и боевым прогоном 2026-09-02 (см. модуль группы), здесь
проверяется только логика решений: dry-run без кликов, fail-closed отказы
до точки невозврата, порядок before_click и uncertain-исходы после
передачи файла (гидратация, редактор, назначение), readback персистентного
состояния после перезагрузки вместо оптимистичного img-маркера (#955).
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


class FakeKeyboard:
    def __init__(self, page):
        self._page = page

    def press(self, key):  # noqa: ARG002
        # Боевой прогон 7 (2026-09-04): Enter по сфокусированной assign-кнопке
        # отправляется БЕЗ ошибки, но кнопку не активирует (маркер не
        # появляется) — это фейк моделирует по умолчанию. Сценарий
        # «клавиатура сработала» включается явно: page.keyboard_activates.
        if self._page.keyboard_activates:
            self._page.on_click(resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT)


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

    def scroll_into_view_if_needed(self, *, timeout=None):  # noqa: ARG002
        self._page.scrolls.append(self._selector)

    def focus(self):  # noqa: ARG002
        if getattr(self._page, "keyboard_disabled", False):
            raise PlaywrightError("fake: focus failed")
        self._page.scrolls.append("focus:" + self._selector)

    def click(self, *, timeout=None):  # noqa: ARG002
        self._page.clicks.append(self._selector)
        self._page.on_click(self._selector)

    def dispatch_event(self, event):  # noqa: ARG002
        if getattr(self._page, "dispatch_disabled", False):
            raise PlaywrightError("fake: dispatch failed")
        self._page.dispatches.append(self._selector)
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
        readback_image_count=1,
        readback_page_ok=True,
        readback_placeholder_after=0,
    ):
        self.set_files: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.scrolls: list[str] = []
        self.dispatches: list[str] = []
        self.evaluates: list[str] = []
        self.keyboard = FakeKeyboard(self)
        self.keyboard_disabled = False
        # True = сценарий «Enter реально активировал кнопку» (по умолчанию
        # выключен: живой прогон 7 показал, что Enter кнопку не активирует)
        self.keyboard_activates = False
        # True = dispatch_event бросает PlaywrightError (имитация отказа)
        self.dispatch_disabled = False
        # бои 8-9: активация assign работает только после переоткрытия
        # вьювера карандашом (blob без photo id против персистентного фото)
        self.assign_works_only_after_reopen = False
        self.pencil_reopened = False
        self.marker_count = 0
        self._nav_calls = 0
        # гидратация assign-кнопки после неудачного клика (ленивый чанк
        # модалки): False имитирует «чанк так и не гидратировался»
        self.assign_hydrated = True
        self.wait_fn_calls = 0
        self.reloaded = False
        self._avatar_count = avatar_count
        self._file_input_count = file_input_count
        self._hydrated = hydrated
        self._editor_visible = editor_visible
        self._assign_visible = assign_visible
        self._marker_after_assign = marker_after_assign
        self._readback_image_count = readback_image_count
        self._readback_page_ok = readback_page_ok
        # после readback-перезагрузки img появляется только после стольких
        # опросов состояния (задержка рендера SPA, замечание ревью #962);
        # 0 = img доступен сразу. Ноль после исчерпания = плейсхолдер.
        self._readback_placeholder_after = readback_placeholder_after
        self._readback_polls = 0
        # имитация «на странице нет ни img, ни плейсхолдера» (дрейф обоих
        # селекторов) — readback обязан вернуть неопределённое состояние
        self.hide_placeholder = False
        # имитация модалки «8 фото — это максимум» (галерея аккаунта полна)
        self.limit_modal = False
        self.url = "https://hh.ru/resume/rid"

    def on_reload(self):
        """Вызывается заглушкой goto_hh; вторая навигация = readback (#955)."""
        self._nav_calls += 1
        if self._nav_calls < 2:
            return  # первичный переход на страницу резюме
        if not self._readback_page_ok:
            raise PlaywrightError("fake: readback navigation failed")
        self.reloaded = True

    def on_click(self, selector):
        # assign-клик назначает фото: маркер появляется по факту клика.
        # Флаг assign_works_only_after_reopen моделирует бои 8-9: активация
        # в модалке после crop-upload молча не работает, работает только
        # после переоткрытия вьювера через карандаш.
        if selector == resume_photo.RESUME_AVATAR_EDIT_BUTTON:
            self.pencil_reopened = True
        if selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            if self.assign_works_only_after_reopen and not self.pencil_reopened:
                return
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
        if selector == resume_photo.RESUME_PHOTO_VIEWER_LIMIT:
            return FakeLocator(self, selector, count=1 if self.limit_modal else 0)
        if selector == resume_photo.RESUME_AVATAR_IMAGE:
            # после readback-перезагрузки DOM свежий: оптимистичный маркер
            # заменяется персистентным состоянием readback_image_count
            if not self.reloaded:
                return FakeLocator(self, selector, count=self.marker_count)
            if self._readback_polls > self._readback_placeholder_after:
                return FakeLocator(self, selector, count=self._readback_image_count)
            return FakeLocator(self, selector, count=0)
        if selector == resume_photo.RESUME_AVATAR_PLACEHOLDER:
            # плейсхолдер «фото нет» подтверждается, когда лимит задержки
            # img исчерпан и персистентного img нет
            if self.reloaded and not self.hide_placeholder:
                has_img = self._readback_polls > self._readback_placeholder_after and (
                    self._readback_image_count > 0
                )
                if not has_img:
                    return FakeLocator(self, selector, count=1)
            return FakeLocator(self, selector, count=0)
        return FakeLocator(self, selector, count=0)

    def evaluate(self, script, arg=None):  # noqa: ARG002
        self.evaluates.append(script)
        return None  # scrollIntoView контейнера / scrollTo(0,0) перед assign

    def wait_for_function(self, script, *, arg=None, timeout=None):  # noqa: ARG002
        # browser.wait_for_react_hydration поллит через wait_for_function;
        # таймаут в реальном Playwright — PlaywrightError.
        self.wait_fn_calls += 1
        # второй вызов — гидратация assign-кнопки после неудачного клика
        if arg == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT and not self.assign_hydrated:
            raise PlaywrightError("fake: assign button hydration timeout")
        if not self._hydrated:
            raise PlaywrightError("fake: hydration timeout")

    def wait_for_timeout(self, ms):  # noqa: ARG002
        # опрос readback-цикла продвигает симулированное время рендера
        if self.reloaded:
            self._readback_polls += 1

    def content(self) -> str:
        return "<html></html>"  # для browser.dump_page_html в uncertain-исходах


class FakeResume:
    resume_url = "https://hh.ru/resume/rid"


PHOTO = PhotoFile(path=Path("/tmp/x.jpg"), size_bytes=100, kind="jpeg")


@pytest.fixture(autouse=True)
def _no_navigation(monkeypatch):
    """goto/auth/cookie — реальный браузерный слой; в unit-тестах заглушки."""
    monkeypatch.setattr(
        resume_photo, "goto_hh", lambda page, url: getattr(page, "on_reload", lambda: None)()
    )
    monkeypatch.setattr(resume_photo, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(resume_photo, "dismiss_cookie_banner", lambda page: None)
    # readback-бюджет реального времени в фейке не нужен длинным: цикл
    # опроса не спит по-настоящему, 100мс хватает на десятки итераций
    monkeypatch.setattr(resume_photo, "_READBACK_CONFIRM_TIMEOUT_MS", 100)


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
    assert page.reloaded  # успех подтверждён readback-перезагрузкой
    # первая мутация — set_input_files, потом редактор, потом назначение
    assert order == ["before_click", "set_files"]
    assert page.clicks == [
        resume_photo.RESUME_PHOTO_EDITOR_APPLY,
        resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
    ]
    # assign-кнопка явно скроллится перед кликом (боевой кейс 2026-09-04)
    assert page.scrolls == [resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT]
    # перед кликом документ скроллится наверх (шапка MediaViewer, гипотеза
    # по дампу photo_assign_click_uncertain_20260904_*)
    assert any("window.scrollTo" in script for script in page.evaluates)
    # при успехе позиционного клика dispatch_event не вызывается
    assert page.dispatches == []
    assert page.set_files == [(resume_photo.RESUME_PHOTO_FILE_INPUT, str(PHOTO.path))]


def test_marker_optimistic_readback_absent_is_uncertain():
    """Оптимистичный img есть, но после перезагрузки фото отсутствует (#955)."""
    page = FakePage(readback_image_count=0)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert result.photo_present is False
    assert page.reloaded
    assert "плейсхолдер" in result.reason


def test_readback_img_rendered_late_is_success(monkeypatch):
    """Регрессия ревью #962: img вставляется SPA с задержкой после видимости
    блока — мгновенный подсчёт давал ложный uncertain при успехе."""
    monkeypatch.setattr(resume_photo, "_READBACK_CONFIRM_TIMEOUT_MS", 2_000)
    monkeypatch.setattr(resume_photo, "_READBACK_POLL_MS", 1)
    page = FakePage(readback_placeholder_after=5, readback_image_count=1)
    result = _run(page, before_click=lambda: None)
    assert result.success
    assert result.photo_present is True
    assert page.reloaded


def test_readback_no_img_no_placeholder_is_uncertain():
    """Ни img, ни плейсхолдера за бюджет — состояние не определено."""
    page = FakePage(readback_image_count=0)
    # имитируем «плейсхолдера тоже нет»: image_count=0, но плейсхолдер
    # фейк рисует только пока img отсутствует; гасим его отдельным флагом
    page.hide_placeholder = True
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert result.photo_present is None


def test_readback_page_unreadable_is_uncertain():
    """Страница при readback не перечиталась — состояние не подтверждено."""
    page = FakePage(readback_page_ok=False)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert result.photo_present is None
    assert not page.reloaded
    assert "не перечиталась" in result.reason


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


def test_gallery_limit_is_clean_fail_not_uncertain(monkeypatch):
    """Боевой прогон 5 (2026-09-04): модалка photo-viewer-limit вместо
    crop-редактора — файл отклонён лимитом галереи аккаунта, мутации нет:
    чистый fail, не uncertain."""
    monkeypatch.setattr(resume_photo, "_EDITOR_WAIT_TIMEOUT_MS", 1)
    page = FakePage(editor_visible=False)
    page.limit_modal = True
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is False
    assert result.photo_present is False
    assert "переполнена" in result.reason


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


def test_assign_click_falls_back_to_keyboard_and_succeeds(monkeypatch):
    """Клик по assign вне вьюпорта падает — явный сценарий «клавиатурный
    Enter активировал кнопку»: маркер появляется, исход — success."""
    page = FakePage()
    page.keyboard_activates = True
    orig_click = FakeLocator.click

    def failing_click(self, *, timeout=None):  # noqa: ARG002
        if self._selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            raise PlaywrightError("fake assign click failed (outside viewport)")
        orig_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", failing_click)
    result = _run(page, before_click=lambda: None)
    assert result.success
    assert result.photo_present is True
    assert "focus:" + resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT in page.scrolls


def test_assign_click_keyboard_neutral_dispatch_succeeds(monkeypatch):
    """Боевой прогон 8 (2026-09-04): позиционный клик падает (NavBar модалки
    над оверлеем), Enter не активирует — после НЕУДАЧНОГО клика код ждёт
    гидратацию кнопки и отправляет dispatch_event('click'), который
    назначает фото; исход — success."""
    page = FakePage()
    orig_click = FakeLocator.click

    def failing_click(self, *, timeout=None):  # noqa: ARG002
        if self._selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            raise PlaywrightError("fake assign click failed (outside viewport)")
        orig_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", failing_click)
    result = _run(page, before_click=lambda: None)
    assert result.success
    assert result.photo_present is True
    assert page.reloaded  # success только через readback
    assert page.dispatches == [resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT]
    # гидратация assign-кнопки проверена ДО dispatch (MFE-инпут + кнопка)
    assert page.wait_fn_calls == 2


def test_assign_button_never_hydrated_still_attempts_dispatch(monkeypatch):
    """Чанк модалки не гидратировался за бюджет — активация всё равно
    отправлена, исход классифицирует маркер (fail-closed без раннего
    отказа): dispatch назначает фото — success."""
    page = FakePage()
    page.assign_hydrated = False
    orig_click = FakeLocator.click

    def failing_click(self, *, timeout=None):  # noqa: ARG002
        if self._selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            raise PlaywrightError("fake assign click failed (outside viewport)")
        orig_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", failing_click)
    result = _run(page, before_click=lambda: None)
    assert result.success
    assert result.photo_present is True
    assert page.wait_fn_calls == 2
    assert page.dispatches == [resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT]


def test_assign_click_and_keyboard_failing_is_uncertain(monkeypatch):
    page = FakePage()
    page.keyboard_disabled = True
    page.dispatch_disabled = True
    orig_click = FakeLocator.click

    def failing_click(self, *, timeout=None):  # noqa: ARG002
        if self._selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            raise PlaywrightError("fake assign click failed")
        orig_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", failing_click)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert "клавиатурный фолбэк" in result.reason
    assert "dispatch_event" in result.reason


def test_marker_missing_after_assign_is_uncertain(monkeypatch):
    monkeypatch.setattr(resume_photo, "_CONFIRM_TIMEOUT_MS", 1)
    page = FakePage(marker_after_assign=0)
    result = _run(page, before_click=lambda: None)
    assert not result.success
    assert result.uncertain is True
    assert result.photo_present is None
    assert len(page.set_files) == 1


def test_marker_missing_reopen_fallback_assigns_and_succeeds(monkeypatch):
    """Бои 8-9 (2026-09-04): активация assign-current в модалке после
    crop-upload молча не работает (current — blob без photo id); фолбэк —
    закрыть модалку, открыть вьювер карандашом (новейшее фото галереи),
    dispatch_event по assign-current — фото назначено, success через
    readback."""
    page = FakePage()
    page.assign_works_only_after_reopen = True
    orig_click = FakeLocator.click

    def failing_click(self, *, timeout=None):  # noqa: ARG002
        if self._selector == resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT:
            raise PlaywrightError("fake assign click failed (outside viewport)")
        orig_click(self, timeout=timeout)

    monkeypatch.setattr(FakeLocator, "click", failing_click)
    result = _run(page, before_click=lambda: None)
    assert result.success
    assert result.photo_present is True
    assert page.reloaded
    # круг 1: dispatch по assign (мёртвый для blob); фолбэк: close + assign
    assert page.dispatches == [
        resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
        resume_photo.RESUME_PHOTO_VIEWER_CLOSE,
        resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
    ]
    # вьювер переоткрыт позиционным кликом по карандашу
    assert resume_photo.RESUME_AVATAR_EDIT_BUTTON in page.clicks
    assert page.pencil_reopened
    # гидратация assign-кнопки проверялась в обоих кругах (+ MFE-инпут)
    assert page.wait_fn_calls == 3


def test_upload_result_defaults():
    result = UploadPhotoResult()
    assert result.success is False
    assert result.uncertain is False
    assert result.photo_present is None
