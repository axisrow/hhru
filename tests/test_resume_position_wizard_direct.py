"""Direct wizard save of an exact catalog leaf — unit level (#913).

Контракт #911 battle2 (5487694535): fill точного имени → NEXT → модалка
(state-machine wait: подтверждённая модалка ЛИБО уход URL) → выбор листа →
submit → финальный NEXT после скрытия. Дубликаты отсекает префлайт #912.
"""

from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError

import hhru_bot.resume_position as resume_position
from hhru_bot.config import bare_resume
from hhru_bot.resume_position import PositionValues

pytestmark = pytest.mark.unit

WIZARD_URL = "https://hh.ru/profile/resume/professional_role?resume=resume-id"
LEFT_URL = "https://hh.ru/profile/resume/common?resume=resume-id"


def _wizard_page():
    page = MagicMock()
    page.url = WIZARD_URL
    position = MagicMock()
    position.count.return_value = 1
    position.input_value.return_value = ""
    clear = MagicMock()
    clear.count.return_value = 0
    next_button = MagicMock()
    next_button.count.return_value = 1
    next_button.first = next_button
    page.locator.side_effect = lambda selector: {
        resume_position.WIZARD_POSITION: position,
        resume_position.WIZARD_POSITION_CLEAR: clear,
        resume_position.WIZARD_NEXT: next_button,
    }[selector]
    return page, position, next_button


def _plan() -> PositionValues:
    # Цель — точный лист: title совпадает с согласованной специализацией (#913).
    return PositionValues(title="Тестировщик", specializations=["Тестировщик"])


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(resume_position, "WIZARD_VERIFY_POLL_MS", 1)
    monkeypatch.setattr(resume_position, "WIZARD_WAIT_MS", 300)
    # словарные page-двойники не моделируют баннер cookie-политики
    monkeypatch.setattr(resume_position, "dismiss_cookie_banner", lambda _page: None)


def test_direct_save_waits_out_modal_flicker_then_selects_leaf(monkeypatch):
    # После NEXT модалка монтируется асинхронно (#913): мигание НЕ ошибка,
    # NEXT не ретраится. Живой прогон 2026-09-01: click() роняется
    # enter-анимацией модалки ПРИ СОСТОЯВШЕМСЯ переходе — исключение
    # глотается, переход подтверждает state-machine wait.
    page, position, next_button = _wizard_page()
    next_button.click.side_effect = PlaywrightError("intercepted by modal-overlay")
    confirmations = iter([False, False, True, True, False, False])
    monkeypatch.setattr(
        resume_position, "is_profession_modal_confirmed", lambda _page: next(confirmations)
    )
    selected: list[tuple[str, dict]] = []

    def fake_select(_page, area, *, expected_role_id=None, **_kwargs):
        selected.append((area, {"expected_role_id": expected_role_id}))
        # submit закрывает модалку: подтверждения после выбора — False
        return ""

    monkeypatch.setattr(resume_position, "select_catalog_leaf", fake_select)
    resume = bare_resume("resume-id")

    resume_position.save_position_wizard(
        page, resume, _plan(), role_id="124", before_first_click=lambda: None
    )

    position.fill.assert_called_once_with("Тестировщик")
    assert next_button.click.call_count == 2  # вход в модалку + финальный, не ретраи
    assert selected == [("Тестировщик", {"expected_role_id": "124"})]
    page.wait_for_url.assert_called_once()


def test_direct_save_returns_when_screen_left_without_modal(monkeypatch):
    # Экран ушёл сразу после NEXT (прямой save базовой категории, #900):
    # модалки не будет — функция возвращается, запись разбирает readback.
    page, _position, next_button = _wizard_page()
    monkeypatch.setattr(resume_position, "is_profession_modal_confirmed", lambda _page: False)
    select = MagicMock()
    monkeypatch.setattr(resume_position, "select_catalog_leaf", select)

    def _leave_wizard():
        page.url = LEFT_URL

    next_button.click.side_effect = _leave_wizard
    resume = bare_resume("resume-id")

    resume_position.save_position_wizard(page, resume, _plan(), role_id="124")

    next_button.click.assert_called_once_with()  # только первый NEXT
    select.assert_not_called()
    page.wait_for_url.assert_not_called()


def test_direct_save_fails_closed_when_modal_never_confirms(monkeypatch):
    # Модалка не подтвердилась за таймаут (в т.ч. скрытый overlay — узел в
    # DOM не означает открытие): dump, различимое исключение для фолбэка,
    # НИ ОДНОГО повторного клика NEXT и никакого submit.
    page, _position, next_button = _wizard_page()
    monkeypatch.setattr(resume_position, "is_profession_modal_confirmed", lambda _page: False)
    select = MagicMock()
    monkeypatch.setattr(resume_position, "select_catalog_leaf", select)
    dump = MagicMock(return_value="dump.html")
    monkeypatch.setattr(resume_position, "_dump_wizard_failure", dump)
    resume = bare_resume("resume-id")

    with pytest.raises(resume_position.ChipPopularUnavailable, match="Уточните специальность"):
        resume_position.save_position_wizard(page, resume, _plan(), role_id="124")

    next_button.click.assert_called_once_with()
    select.assert_not_called()
    dump.assert_called_once()


def test_direct_save_surfaces_catalog_reason_as_runtime_error(monkeypatch):
    # «Другое»/несовпадение role_id приходит причиной-строкой из
    # select_catalog_leaf — отказ ДО submit, финальный NEXT не кликается.
    page, _position, next_button = _wizard_page()
    monkeypatch.setattr(resume_position, "is_profession_modal_confirmed", lambda _page: True)
    monkeypatch.setattr(
        resume_position,
        "select_catalog_leaf",
        lambda *_args, **_kwargs: "профессия «Тестировщик» не найдена в каталоге",
    )
    resume = bare_resume("resume-id")

    with pytest.raises(RuntimeError, match="не найдена в каталоге"):
        resume_position.save_position_wizard(page, resume, _plan(), role_id="124")

    assert next_button.click.call_count == 1


def test_direct_save_waits_for_modal_close_before_final_next(monkeypatch):
    # Видимый overlay перекрывает экран визарда и БЛОКИРУЕТ клик по wizard
    # NEXT (#913): финальный NEXT кликается только после подтверждённого
    # скрытия модалки; не закрылась — отказ без второго NEXT.
    page, _position, next_button = _wizard_page()
    monkeypatch.setattr(resume_position, "is_profession_modal_confirmed", lambda _page: True)
    monkeypatch.setattr(resume_position, "select_catalog_leaf", lambda *_a, **_k: "")
    resume = bare_resume("resume-id")

    with pytest.raises(RuntimeError, match="не закрылась"):
        resume_position.save_position_wizard(page, resume, _plan(), role_id="124")

    assert next_button.click.call_count == 1
    page.wait_for_url.assert_not_called()
