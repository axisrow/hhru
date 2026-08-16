"""Guard из #217: тест без live-маркера не должен доходить до движка Playwright.

Аудит реализации #217 нашёл три пути в обход прежней заглушки, которая
патчила `launch_context`:

A. подмена `browser.sync_playwright` ОТКРЫВАЛА проход к настоящему
   `launch_context` (заглушка сама себя отключала по условию
   `browser.sync_playwright is not real_sync_playwright`);
B. ссылка на `launch_context`, импортированная в шапке модуля теста, живёт
   мимо патча — autouse-фикстура выполняется позже импорта;
C. `auth.login` вызывает `sync_playwright()` напрямую, минуя `launch_context`,
   то есть заявленный в #217 инвариант «единственная дверь наружу» не держался.

Ревью PR #224 нашло, что переноса перехвата на имя `sync_playwright` мало:

D. фабрику можно импортировать под ДРУГИМ именем в шапке модуля теста
   (`from playwright.sync_api import sync_playwright as factory`) — патч по
   имени такую ссылку не догоняет, и она возвращала живой
   `PlaywrightContextManager`.

Итоговый инвариант держит патч `PlaywrightContextManager.__enter__` — самой
точки старта движка. Класс один на все ссылки, поэтому алиас, замыкание и
default-аргумент закрываются одинаково.

Сам файл маркера live_* НЕ несёт — в этом суть проверки.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Путь D: фабрика движка захвачена под другим именем ДО старта фикстуры.
from playwright.sync_api import sync_playwright as _factory_captured_at_import

# Путь B: ссылка захватывается на импорте модуля, ДО запуска autouse-фикстуры.
from hhru_bot.browser import launch_context as _captured_at_import

pytestmark = pytest.mark.integration


def _unused_session(tmp_path: Path) -> Path:
    return tmp_path / "session.json"


def test_module_attribute_path_is_blocked(tmp_path: Path) -> None:
    """Прямой вызов через атрибут модуля."""
    import hhru_bot.browser as browser

    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        with browser.launch_context(_unused_session(tmp_path)):
            pass


def test_function_local_import_is_blocked(tmp_path: Path) -> None:
    """Локальный импорт внутри функции — так пишут все commands/*.py."""
    from hhru_bot.browser import launch_context

    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        with launch_context(_unused_session(tmp_path)):
            pass


def test_command_module_alias_is_blocked(tmp_path: Path) -> None:
    """Алиас, разрешённый в модуле команды на импорте."""
    import hhru_bot.auth_code as auth_code

    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        with auth_code.launch_context(_unused_session(tmp_path)):
            pass


def test_reference_captured_at_import_is_blocked(tmp_path: Path) -> None:
    """Дыра B: ссылка, захваченная до фикстуры, тоже упирается в заглушку."""
    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        with _captured_at_import(_unused_session(tmp_path)):
            pass


def test_auth_login_is_blocked(tmp_path: Path) -> None:
    """Дыра C: вторая дверь наружу — auth.login мимо launch_context.

    storage_state_file уводим в tmp_path: login() создаёт родительский каталог
    до запуска браузера, и проверять надо блокировку движка, а не отказ mkdir.
    """
    import hhru_bot.auth as auth

    class _Config:
        storage_state_file = tmp_path / "storage_state" / "session.json"
        user_agent = None

    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        auth.login(_Config())  # type: ignore[arg-type]


def test_patching_sync_playwright_does_not_unlock_real_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Дыра A: подмена sync_playwright больше не открывает настоящий движок.

    Тест ведёт себя как невнимательный агент, который «замокал браузер»
    неполноценно: подменил `sync_playwright` объектом, не являющимся рабочим
    контекст-менеджером. Раньше такая подмена снимала защиту и пускала в
    настоящий `launch_context`; теперь исполняется только сам мок, а движок
    Playwright недостижим.
    """
    import hhru_bot.browser as browser

    calls: list[str] = []

    class _IncompleteMock:
        def __enter__(self):
            calls.append("entered")
            raise AssertionError("тест дошёл только до собственного мока")

        def __exit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr(browser, "sync_playwright", lambda: _IncompleteMock())

    with pytest.raises(AssertionError, match="только до собственного мока"):
        with browser.launch_context(_unused_session(tmp_path)):
            pass

    assert calls == ["entered"]


def test_aliased_factory_captured_at_import_is_blocked() -> None:
    """Дыра D: фабрика под другим именем, захваченная до фикстуры.

    Патч по имени `sync_playwright` такую ссылку не догонял — она возвращала
    живой PlaywrightContextManager, и вход в него поднял бы Chromium с
    сохранённой сессией hh.ru. Теперь блокирует патч `__enter__` на классе.

    Вход в контекст-менеджер здесь безопасен именно потому, что заглушка
    срабатывает до старта движка; если тест когда-нибудь начнёт падать не
    RuntimeError'ом, а зависать или поднимать браузер — защита сломана.
    """
    factory = _factory_captured_at_import()

    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        with factory:
            pass


def test_aliased_factory_start_is_blocked() -> None:
    """Тот же обход через .start() — второй вход в движок помимо `with`."""
    factory = _factory_captured_at_import()

    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        factory.start()


def test_guard_covers_every_sync_playwright_caller_in_src() -> None:
    """Инвариант #217 «дверь наружу одна» проверяется, а не декларируется.

    Если в src/ появится новый прямой вызов sync_playwright (как было с auth.py),
    он обязан импортировать его как атрибут модуля — только такие имена фикстура
    способна перехватить. `from playwright.sync_api import sync_playwright` даёт
    именно такой атрибут; вызов вида `playwright.sync_api.sync_playwright()`
    тоже покрыт, так как патчится и сам модуль playwright.sync_api.
    """
    import hhru_bot.auth as auth
    import hhru_bot.browser as browser

    for module in (browser, auth):
        blocked = module.sync_playwright
        with pytest.raises(RuntimeError, match="Playwright заблокирован"):
            blocked()
