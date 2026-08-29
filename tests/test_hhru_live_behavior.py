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
VISIBILITY_RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_visibility_hidden_scenario.js"


def _run_node_scenario(runner: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не найден в PATH — поведенческий тест content.js пропущен")
    result = subprocess.run(
        [node, str(runner)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node harness завершился с ошибкой:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return json.loads(result.stdout)


def _run_scenario() -> dict:
    return _run_node_scenario(RUNNER)


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


def test_hhru_live_content_script_does_not_misreport_css_visibility_hidden_as_shown():
    """PR #767 Codex round-3 review, finding 1 (confidence 0.9): offsetWidth/
    offsetHeight/getClientRects() alone react only to display:none, not to
    CSS `visibility:hidden` -- an element with visibility:hidden still has
    non-zero layout metrics. Without an explicit visibility check, an overlay
    that starts visibility:hidden would be misreported as already-visible on
    the initial DOM scan (and added to `seen`), permanently suppressing the
    real reveal that follows -- silently defeating the issue #743 finding 1
    hide->show re-detect fix for this specific CSS pattern.

    Scenario in tests/js_harness/run_visibility_hidden_scenario.js: a modal
    already in the DOM, laid out but visibility:hidden -> revealed by
    clearing that property. Expects zero reports before the reveal and
    exactly one report after.
    """
    scenario = _run_node_scenario(VISIBILITY_RUNNER)
    assert scenario["reportsBeforeReveal"] == 0, (
        "overlay must not be reported while still visibility:hidden "
        f"(offsetWidth/offsetHeight/getClientRects alone would wrongly see it as visible): {scenario}"
    )
    assert scenario["overlayReportCount"] == 1, (
        f"expected exactly one overlay_detected after the reveal, got: {scenario}"
    )
