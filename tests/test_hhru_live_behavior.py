"""Поведенческий тест extensions/hhru-live/content.js (issue #743).

Существующие тесты в tests/test_page_state.py проверяют исходник content.js
текстовым grep'ом — они умеют убедиться, что нужная строка присутствует, но
не умеют исполнить код и проверить реальное поведение. Именно поэтому round 2
(PR #644) дал регрессию находки 1 (permanent WeakSet блокирует повторный
report после hide -> show): grep видел "WeakSet" в файле и был доволен, хотя
семантика WeakSet стала неверной.

Здесь content.js исполняется по-настоящему в Node (`node --check`-совместимый
ES5/ES2020 синтаксис, без сборки) через vm.createContext с минимальным
DOM/`chrome.*`-стабом (tests/js_harness/dom_stub.js). Зависимость выбрана
намеренно: проект не имеет npm/package.json вообще, а jsdom/linkedom как
devDependency потребовали бы заводить такой footprint ради нескольких
DOM-примитивов, которые content.js реально использует. Системный `node`
уже присутствует на GitHub Actions ubuntu-latest раннере (используется самим
Actions рантаймом), поэтому здесь не требуется новый CI setup-шаг — тест
пропускается (skip, не fail), если `node` недоступен локально.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).parents[1]
RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_content_scenario.js"


def _run_scenario() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не найден в PATH — поведенческий тест content.js пропущен")
    result = subprocess.run(
        [node, str(RUNNER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node harness завершился с ошибкой:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return json.loads(result.stdout)


def test_hhru_live_content_script_re_detects_overlay_after_hide_then_show():
    """Issue #743 finding 1: permanent WeakSet блокировал повторный report
    того же DOM-узла, если overlay скрылся и снова показался через тот же
    attribute-toggle (класс/aria-hidden). Сценарий в
    tests/js_harness/run_content_scenario.js: cookie-баннер уже в DOM
    (скрыт) -> показан (toggle класса) -> скрыт -> показан снова тем же
    узлом. Ожидается ровно ДВА overlay_detected report'а — по одному на
    каждый show, ни один hide не репортится, а второй show не должен быть
    молча проглочен permanent-блокировкой.
    """
    scenario = _run_scenario()
    overlay_reports = [m for m in scenario["messages"] if m["kind"] == "overlay_detected"]
    assert len(overlay_reports) == 2, (
        "ожидались 2 overlay_detected (после первого и второго show того же узла), "
        f"получено {len(overlay_reports)}: {overlay_reports}"
    )
    assert all(r["overlay"]["visible"] for r in overlay_reports), (
        "report() должен вызываться только когда overlay реально видим "
        f"(offsetWidth/offsetHeight/getClientRects); got {overlay_reports}"
    )
    assert all(r["overlay"]["type"] == "cookie_banner" for r in overlay_reports)


def test_hhru_live_content_script_reports_connected_on_load():
    """Сценарий должен по-прежнему отправлять `connected` при загрузке
    content.js — регресс-проверка, что harness не сломал остальную
    инициализацию, пока проверяет узкий hide/show сценарий выше.
    """
    scenario = _run_scenario()
    assert "connected" in scenario["kinds"]
