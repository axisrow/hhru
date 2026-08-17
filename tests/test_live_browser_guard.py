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

Ревью PR #224 нашло ещё три пути, каждый из которых поднимал настоящий движок:

D. фабрику можно импортировать под ДРУГИМ именем в шапке модуля теста
   (`from playwright.sync_api import sync_playwright as factory`) — патч по
   имени такую ссылку не догоняет, и она возвращала живой
   `PlaywrightContextManager`;
E. код на уровне модуля исполняется при СБОРЕ, до применения `-m` и до любой
   фикстуры. Файл с маркером `live_write_danger`, который pytest исключил из
   сбора (`1 deselected`), всё равно стартовал Chromium на импорте;
F. `async_playwright` — отдельный контекст-менеджер со своим `Connection`,
   патч sync-класса его не касался: обычный тест поднимал движок через
   `asyncio.run`.

Итоговый инвариант держит `compute_driver_executable` — узкое место запуска
процесса-драйвера, общее для sync, async и прямого `PipeTransport`. Защита
ставится в `pytest_configure`, то есть ДО сбора, и снимается только на время
теста с live-маркером.

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
    сохранённой сессией hh.ru.

    Вход в контекст-менеджер здесь безопасен именно потому, что блокировка
    срабатывает до запуска процесса-драйвера; если тест когда-нибудь начнёт
    падать не RuntimeError'ом, а зависать или поднимать браузер — защита
    сломана.
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


def test_async_api_is_blocked() -> None:
    """Дыра F: async-API — отдельный контекст-менеджер со своим Connection.

    Патч sync-класса его не касался, и обычный тест поднимал настоящий движок
    через `asyncio.run`. Общая для обоих API граница — процесс-драйвер.
    """
    import asyncio

    from playwright.async_api import async_playwright

    async def _try_start() -> None:
        async with async_playwright():
            pass

    with pytest.raises(RuntimeError, match="Playwright заблокирован"):
        asyncio.run(_try_start())


def test_direct_driver_entrypoint_is_blocked() -> None:
    """Инвариант держится на границе процесса, а не на именах и классах.

    `compute_driver_executable` — единственный источник пути к бинарю драйвера;
    и sync, и async, и прямое построение `PipeTransport` идут через неё. Если
    кто-то обойдёт все обёртки, он всё равно упрётся сюда.
    """
    import playwright._impl._driver as driver
    import playwright._impl._transport as transport

    for module in (driver, transport):
        with pytest.raises(RuntimeError, match="Playwright заблокирован"):
            module.compute_driver_executable()


def test_engine_is_blocked_during_collection(tmp_path: Path) -> None:
    """Дыра E: код на уровне модуля исполняется при СБОРЕ, до всякой фикстуры.

    Самый опасный из найденных путей: файл с маркером `live_write_danger`,
    который pytest ИСКЛЮЧАЕТ из сбора, всё равно поднимал Chromium на импорте —
    маркеры применяются уже после того, как модуль импортирован.

    Проверяется отдельным процессом pytest: внутри текущего процесса стадию
    collection не воспроизвести. Тест-образец пишет исход в файл, поэтому
    «не запустился вовсе» и «запустился, но заблокирован» различимы.

    Образец обязан лежать ВНУТРИ `tests/`: conftest.py применяется по цепочке
    каталогов, и файл во временной папке защиту бы не получил — тест тогда
    проверял бы не тот процесс, а собственную ошибку.
    """
    import subprocess
    import sys
    import textwrap

    marker = tmp_path / "outcome.txt"
    sample = Path(__file__).parent / "test_zz_collection_sample_tmp.py"
    sample.write_text(
        textwrap.dedent(
            f"""
            import pytest
            from playwright.sync_api import sync_playwright

            pytestmark = pytest.mark.live_write_danger

            try:
                with sync_playwright():
                    _outcome = "ENGINE_STARTED"
            except RuntimeError as exc:
                _outcome = f"BLOCKED: {{exc}}"
            except Exception as exc:  # noqa: BLE001 — фиксируем любой иной исход
                _outcome = f"OTHER: {{type(exc).__name__}}"

            open({str(marker)!r}, "w").write(_outcome)


            def test_placeholder():
                pass
            """
        ).lstrip()
    )

    try:
        subprocess.run(
            [sys.executable, "-m", "pytest", str(sample), "-p", "no:randomly"],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            timeout=180,
            check=False,
        )
    finally:
        # Образец лежит в tests/ — не оставлять его даже при падении: иначе он
        # попадёт в обычный сбор и в валидатор маркеров (test_markers.py).
        sample.unlink(missing_ok=True)

    assert marker.exists(), "тест-образец не исполнился — проверка ничего не доказала"
    assert marker.read_text().startswith("BLOCKED"), marker.read_text()
