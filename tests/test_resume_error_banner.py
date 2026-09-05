"""#972: детекция баннера ошибки на /resume/{id} — внятный отказ вместо таймаутов.

HTML-фикстура — скелет живого сбойного экрана, снятого read-only замером
2026-09-05 (tests/test_resume_error_banner_live.py, дамп
data/logs/972_resume_error_banner_000000.html): несуществующее/удалённое
резюме рендерит <div class="attention attention_bad">Произошла ошибка...</div>
без data-qa. Все тесты без браузера: детерминированный фейк Page поверх
html.parser (паттерн ``_fakes``), селектор — ``div.attention.attention_bad``
+ точный текст (двойной признак, отсекающий другие attention_bad-баннеры).

Семантика (#972 п.3): детекция — pre-mutation отказ (обычный failed/retry),
не uncertain и не запись в actions; существующие ранние выходы (#163) не
меняются.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from _fakes import _DOMNode, _parse_root
from hhru_bot import browser as browser_module
from hhru_bot.about import AboutGenerationError, open_about_editor
from hhru_bot.browser import (
    RESUME_ERROR_BANNER,
    RESUME_UNAVAILABLE_REASON,
    ResumeUnavailable,
    has_resume_error_banner,
    open_confirmed_resume,
    require_available_resume,
)
from hhru_bot.bump import bump_resume
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.resume_education import EducationPlan, edit_education_on_hh
from hhru_bot.resume_position import open_position_form
from hhru_bot.resume_sections import apply_plan as apply_sections_plan
from hhru_bot.skills import edit_skills_on_hh

pytestmark = pytest.mark.integration

# Скелет живого DOM сбойного экрана (без шапки/футера — только main-контейнер
# баннера, снятый замером #972; вложенность и классы дословные).
BANNER_HTML = """
<main class="main-content main-content_broad-spacing">
  <div class="bloko-columns-wrapper">
    <div class="row-content">
      <div class="bloko-column bloko-column_xs-4 bloko-column_s-8 bloko-column_m-12 bloko-column_l-16">
        <div class="attention attention_bad">Произошла ошибка. Возникли неполадки, но мы уже работаем над их устранением.</div>
      </div>
    </div>
  </div>
</main>
"""
BANNER_TEXT_FULL = "Произошла ошибка. Возникли неполадки, но мы уже работаем над их устранением."
# Очевидно поддельный id (38 hex, правило #828).
RESUME_ID = "0" * 38
RESUME_URL = f"https://hh.ru/resume/{RESUME_ID}"


def _attention_bad_nodes(html: str) -> list[_DOMNode]:
    """Все div.attention.attention_bad фикстуры (селектор детектора #972)."""
    found: list[_DOMNode] = []

    def walk(node: _DOMNode) -> None:
        for child in node.children:
            classes = child.attrs.get("class", "").split()
            if child.tag == "div" and "attention" in classes and "attention_bad" in classes:
                found.append(child)
            walk(child)

    walk(_parse_root(html))
    return found


class _ClassTextLocator:
    """Локатор ровно под паттерн детектора: ``.filter(has_text=, visible=)`` + count.

    Playwright-семантика has_text — подстрока в тексте элемента; здесь то же
    (inner_text узла включает его прямых потомков, как у реального браузера).
    ``visible`` — no-op, как у FakeLocator в ``_fakes``: html.parser не знает
    CSS display, все узлы фикстуры считаются видимыми (скрытые копии баннера
    отсекает реальный Playwright-фильтр, а не этот фейк).
    """

    def __init__(self, nodes: list[_DOMNode]):
        self._nodes = nodes

    def filter(
        self,
        *,
        has_text: str | None = None,
        visible: bool | None = None,  # noqa: ARG002
    ) -> _ClassTextLocator:
        if has_text is None:
            return self
        return _ClassTextLocator([n for n in self._nodes if has_text in n.inner_text()])

    def count(self) -> int:
        return len(self._nodes)


class _FakeContext:
    def cookies(self) -> list[dict[str, str]]:
        return [{"name": "hhtoken", "value": "fake"}]


class FakeBannerPage:
    """Page сбойного экрана: goto, пустая форма входа, баннер по селектору #972."""

    def __init__(self, html: str = BANNER_HTML, *, locator_error: bool = False):
        self._html = html
        self.url = RESUME_URL
        self.goto_calls: list[str] = []
        self.context = _FakeContext()
        self._locator_error = locator_error

    def goto(self, url: str, *, wait_until: str = "load") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def content(self) -> str:
        return self._html

    def locator(self, selector: str) -> _ClassTextLocator:
        if self._locator_error:
            raise PlaywrightError("page is closed")
        if selector == RESUME_ERROR_BANNER:
            return _ClassTextLocator(_attention_bad_nodes(self._html))
        # LOGIN_FORM и прочие селекторы точек применения после детекции не
        # читаются; форма входа обязана быть пустой (сессия жива, #972).
        if selector == browser_module.LOGIN_FORM:
            return _ClassTextLocator([])
        return _ClassTextLocator([])


def _resume() -> ResumeConfig:
    return ResumeConfig(
        id="r1",
        resume_url=RESUME_URL,
        search=SearchFilters(text="python", area=1),
    )


# --- Детектор ---------------------------------------------------------------


def test_banner_detected_on_live_dom_skeleton():
    """Позитив: снятый живой скелет распознаётся детектором."""
    assert has_resume_error_banner(FakeBannerPage()) is True


def test_other_attention_bad_text_is_not_the_banner():
    """Двойной признак: attention_bad с ДРУГИМ текстом — не наш баннер.

    ``attention attention_bad`` — общий класс предупреждений hh.ru; класс без
    текста ложно срабатывал бы на чужих баннерах той же страницы.
    """
    html = '<div class="attention attention_bad">Проверьте правильность данных</div>'

    assert has_resume_error_banner(FakeBannerPage(html)) is False


def test_error_text_without_attention_class_is_not_the_banner():
    """Текст баннера в элементе без класса attention_bad — не детект."""
    html = f"<div>{BANNER_TEXT_FULL}</div>"

    assert has_resume_error_banner(FakeBannerPage(html)) is False


def test_regular_resume_page_is_not_the_banner():
    """Страница живого резюме (без баннера) не детектируется."""
    html = """
    <main><div data-qa="resume-block-title-position">Python-разработчик</div></main>
    """

    assert has_resume_error_banner(FakeBannerPage(html)) is False


def test_locator_error_keeps_detector_silent():
    """Ошибка чтения селектора — не доказательство баннера: детектор молчит,
    вызывающий путь сохраняет свой обычный fail-closed отказ."""
    assert has_resume_error_banner(FakeBannerPage(locator_error=True)) is False


# --- Инвариант семантики отказа ----------------------------------------------


def test_require_available_resume_message_states_fact_and_probable_cause():
    """Формулировка #972 п.2: факт (баннер) констатируется, причина —
    вероятная, «удалено» НЕ утверждается (баннер — общий сбойный экран)."""
    with pytest.raises(ResumeUnavailable) as exc_info:
        require_available_resume(FakeBannerPage())

    message = str(exc_info.value)
    assert "резюме недоступно" in message
    assert "баннер ошибки" in message
    assert "удалено владельцем" in message
    assert "не доказано" in message


def test_require_available_resume_passes_without_banner():
    require_available_resume(
        FakeBannerPage("<main><div data-qa='resume-block-title-position'>x</div></main>")
    )


def test_resume_unavailable_is_not_indeterminate_and_not_auth():
    """#972 п.3: отказ — pre-mutation failed/retry, НЕ PageStateIndeterminate
    (состояние подтверждено) и не авторизационный класс (сессия жива)."""
    assert issubclass(ResumeUnavailable, RuntimeError)
    assert not issubclass(ResumeUnavailable, browser_module.PageStateIndeterminate)
    assert not issubclass(ResumeUnavailable, browser_module.NotAuthenticated)


# --- Точки применения: ранний отказ вместо таймаута --------------------------


def test_open_confirmed_resume_fails_fast_on_banner():
    """open_confirmed_resume (пути experience edit/read): ResumeUnavailable
    до identity-чека — сбойный экран держит URL /resume/{id} и прошёл бы его."""
    with pytest.raises(ResumeUnavailable, match="резюме недоступно"):
        open_confirmed_resume(FakeBannerPage(), RESUME_ID)


def test_bump_reports_unavailable_resume_before_button_search():
    """bump: внятный отказ вместо «кнопка поднятия не найдена»; acted=False и
    uncertain=False — клика не было, actions/пауза не нужны (#163/#176)."""
    page = FakeBannerPage()

    result = bump_resume(page, _resume(), dry_run=False)

    assert result.success is False
    assert result.reason == RESUME_UNAVAILABLE_REASON
    assert result.acted is False
    assert result.uncertain is False


def test_open_position_form_raises_resume_unavailable():
    """resume-position (editor/wizard вход): терминальный отказ до выбора flow."""
    with pytest.raises(ResumeUnavailable, match="резюме недоступно"):
        open_position_form(FakeBannerPage(), _resume())


def test_edit_skills_reports_unavailable_resume():
    from hhru_bot.skills import Skill

    result = edit_skills_on_hh(
        FakeBannerPage(), _resume(), (Skill("Python", "basic"),), dry_run=True, mode="append"
    )

    assert result.success is False
    assert result.reason == RESUME_UNAVAILABLE_REASON


def test_edit_education_raises_resume_unavailable():
    with pytest.raises(ResumeUnavailable, match="резюме недоступно"):
        edit_education_on_hh(
            FakeBannerPage(), RESUME_URL, EducationPlan(), section="primary", dry_run=True
        )


def test_sections_apply_plan_reports_unavailable_resume():
    """attestations/recommendations: отказ в errors-списке, триггеры не ищутся."""
    from hhru_bot.resume_sections import ResumeSectionsPlan

    errors = apply_sections_plan(FakeBannerPage(), RESUME_ID, ResumeSectionsPlan(), dry_run=True)

    assert errors == [RESUME_UNAVAILABLE_REASON]


def test_about_editor_raises_domain_error_on_banner():
    """edit-about: доменная AboutGenerationError (команда ловит только её)."""
    with pytest.raises(AboutGenerationError, match="резюме недоступно"):
        open_about_editor(FakeBannerPage(), _resume())


def test_upload_photo_reports_unavailable_resume():
    from pathlib import Path

    from hhru_bot.resume_photo import PhotoFile, upload_photo_on_hh

    photo = PhotoFile(path=Path("/tmp/x.jpg"), size_bytes=10, kind="jpeg")
    result = upload_photo_on_hh(FakeBannerPage(), _resume(), photo, True)

    assert result.success is False
    assert result.reason == RESUME_UNAVAILABLE_REASON
    assert result.photo_present is None


def test_select_photo_reports_unavailable_resume():
    from hhru_bot.resume_photo import select_photo_on_hh

    result = select_photo_on_hh(FakeBannerPage(), _resume(), "123", True)

    assert result.success is False
    assert result.reason == RESUME_UNAVAILABLE_REASON
