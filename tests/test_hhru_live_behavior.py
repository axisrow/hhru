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


def _run_node_scenario(runner: Path, args: list[str] | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не найден в PATH — поведенческий тест content.js пропущен")
    result = subprocess.run(
        [node, str(runner), *(args or [])],
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


# ---------------------------------------------------------------------------
# Policy-слой (issue #588, первый этап): классификация + allowlist-команды +
# закрытие ТОЛЬКО безопасных overlay. Сценарии исполняют content.js по-настоящему
# через tests/js_harness/run_command_scenario.js <name>.
# ---------------------------------------------------------------------------

COMMAND_RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_command_scenario.js"


def _run_command_scenario(name: str) -> dict:
    return _run_node_scenario(COMMAND_RUNNER, [name])


def test_hhru_live_policy_toast_safe_dismiss_closes_via_close_control():
    """Тост с явным close-контролом: safe, закрывается кликом ровно по нему."""
    scenario = _run_command_scenario("toast_safe")
    assert scenario["listedDisposition"] == "safe"
    assert scenario["dismissedOk"] and scenario["overlayGone"]
    assert scenario["clickCount"] == 1 and scenario["clickedClose"]


def test_hhru_live_policy_cookie_banner_dismiss():
    """Cookie-баннер закрывается своим close-контролом (#586)."""
    scenario = _run_command_scenario("cookie_banner")
    assert scenario["listedType"] == "cookie_banner"
    assert scenario["listedDisposition"] == "safe"
    assert scenario["dismissedOk"] and scenario["overlayGone"]
    assert scenario["clickCount"] == 1 and scenario["clickedClose"]


def test_hhru_live_policy_resume_delivered_never_clicks_save():
    """Ключевой инвариант #588/#586: тост «Резюме доставлено» закрывается
    крестиком; кнопка «Сохранить» (выбор статуса поиска — профильные данные)
    не кликается НИКОГДА, ни при каком раскладе."""
    scenario = _run_command_scenario("resume_delivered_never_saves")
    assert scenario["listedDisposition"] == "safe"
    assert scenario["dismissedOk"] and scenario["overlayGone"]
    assert scenario["clickCount"] == 1 and scenario["clickedClose"]
    assert scenario["clickedSave"] is False, f"клик по «Сохранить» недопустим: {scenario}"


def test_hhru_live_policy_apply_step_modal_is_blocked():
    """Модалка формы отклика (RESPONSE_MODAL_FORM_ID / data-qa
    vacancy-response) — apply_step: не закрывается автоматически, кликов 0."""
    scenario = _run_command_scenario("apply_step_blocked")
    assert scenario["listedDisposition"] == "apply_step"
    assert scenario["dismissedOk"] is False
    assert scenario["error"] == "overlay_not_safe"
    assert scenario["errorDisposition"] == "apply_step"
    assert scenario["clickCount"] == 0


def test_hhru_live_policy_dangerous_confirm_blocked():
    """Confirm-модалка необратимого действия — dangerous, блокируется даже
    при наличии close-контрола."""
    scenario = _run_command_scenario("danger_confirm_blocked")
    assert scenario["listedDisposition"] == "dangerous"
    assert scenario["dismissedOk"] is False and scenario["error"] == "overlay_not_safe"
    assert scenario["clickCount"] == 0


def test_hhru_live_policy_dangerous_captcha_blocked():
    """CAPTCHA-текст опасен сам по себе, без остальных якорей."""
    scenario = _run_command_scenario("danger_captcha_blocked")
    assert scenario["listedDisposition"] == "dangerous"
    assert scenario["dismissedOk"] is False and scenario["clickCount"] == 0


def test_hhru_live_policy_ambiguous_modal_blocked():
    """Незнакомая модалка без close-контрола — ambiguous: блокируется,
    решение возвращается агенту (fail-closed, никакого угадывания)."""
    scenario = _run_command_scenario("ambiguous_blocked")
    assert scenario["listedDisposition"] == "ambiguous"
    assert scenario["dismissedOk"] is False and scenario["error"] == "overlay_not_safe"
    assert scenario["clickCount"] == 0


def test_hhru_live_policy_unknown_action_rejected():
    """Действие вне allowlist отклоняется до какого-либо доступа к DOM."""
    scenario = _run_command_scenario("unknown_action_rejected")
    assert scenario["ok"] is False and scenario["error"] == "action_not_allowed"
    assert scenario["clickCount"] == 0


def test_hhru_live_policy_check_element_confirms_next_step():
    """Подтверждение «следующий элемент доступен»: found/visible по селектору;
    obstruction-проба в стабе недоступна и честно репортится как непроверенная."""
    scenario = _run_command_scenario("check_element")
    assert scenario["found"] and scenario["visible"]
    assert scenario["obstructionChecked"] is False
    assert scenario["absentFound"] is False and scenario["noSelectorFound"] is False


def test_hhru_live_policy_dismiss_hidden_overlay_does_not_click():
    """Overlay, скрытый между листингом и dismiss (или отвязанный), не
    кликается: действие по невидимому контролу не имеет оснований."""
    scenario = _run_command_scenario("dismiss_hidden_overlay")
    assert scenario["listedCount"] == 1
    assert scenario["dismissedOk"] is False
    assert scenario["error"] == "overlay_not_found"
    assert scenario["clickCount"] == 0


def test_hhru_live_policy_hidden_close_control_is_no_close_control():
    """Первый close-маркер в порядке документа может быть скрытым
    (display:none шаблон/дубль): клик по невидимому контролу противоречит
    философии файла, поэтому видимые контролы фильтруются, и при их
    отсутствии ответ no_close_control, а не молчаливый клик мимо (PR #935
    review)."""
    scenario = _run_command_scenario("dismiss_hidden_close_control")
    assert scenario["listedCount"] == 1
    assert scenario["dismissedOk"] is False
    assert scenario["error"] == "no_close_control"
    assert scenario["clickCount"] == 0


def test_hhru_live_policy_body_state_class_never_registered():
    """hh.ru помечает cookie-баннер state-классом на <body>
    (cookie-policy-banner-enabled, подтверждено живым DOM 2026-09-02):
    [class*="cookie"] матчит body, и без стража «оверлеем» становится вся
    страница с текстом всего документа."""
    scenario = _run_command_scenario("body_state_class_never_registered")
    assert scenario["listedCount"] == 0


def test_hhru_live_policy_latin_x_needs_real_button():
    """Латинская «x» засчитывается только на настоящей кнопке
    (button/a/[role=button]): декоративный спан с data-qa и глифом «x»
    не должен вставать controls[0] перед настоящим крестиком (PR #935
    review). ×/✕ работают как раньше."""
    scenario = _run_command_scenario("glyph_x_needs_real_button")
    assert scenario["closeControls"] == 1
    assert scenario["clickedFake"] is False
    assert scenario["clickedReal"] is True
    assert scenario["dismissedOk"] is True


def test_hhru_live_policy_reshow_reuses_registry_entry():
    """hide->show того же DOM-узла репортится повторно (seen — по видимости),
    но запись в registry переиспользуется: новый id дал бы дубль в листинге,
    который pruneRegistry никогда не уберёт (PR #935 review)."""
    scenario = _run_command_scenario("dedupe_on_reshow")
    assert scenario["entries"] == 1


def test_hhru_live_policy_obstruction_probe_off_viewport_is_not_checked():
    """elementFromPoint -> null (центр вне вьюпорта) означает «проба ничего
    не смогла проверить»: obstructionChecked обязан остаться false, а не
    covered=false, который агент прочтёт как «кликать можно» (PR #935
    review). Сама проба: свой элемент -> covered=false, чужой ->
    covered=true."""
    scenario = _run_command_scenario("obstruction_probe")
    assert scenario["clearChecked"] is True and scenario["clearCovered"] is False
    assert scenario["coveredChecked"] is True and scenario["coveredValue"] is True
    assert scenario["offscreenChecked"] is False and scenario["offscreenCovered"] is None


# ---------------------------------------------------------------------------
# Policy-core напрямую (issue #929): classifyDisposition / findCloseControls
# исполняются без content.js — через tests/js_harness/run_policy_scenario.js
# <name>. Шесть сценариев покрывают fail-closed приоритет и живые якоря #932
# (aria-label в danger-скане, data-qa *-close крестик, «Понятно» — не close).
# ---------------------------------------------------------------------------

POLICY_RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_policy_scenario.js"


def _run_policy_scenario(name: str) -> dict:
    return _run_node_scenario(POLICY_RUNNER, [name])


def test_hhru_live_policy_core_aria_label_delete_is_dangerous():
    """#932: у реальной hh.ru-нотификации единственный «close»-контрол —
    button[aria-label="Удалить"], текстом нигде не видимый. Без aria-label
    в collectText она классифицировалась бы safe с кликабельным удалением."""
    scenario = _run_policy_scenario("aria_label_delete_is_dangerous")
    assert scenario["disposition"] == "dangerous"
    assert scenario["dangerHit"] is False, "danger обязан прийти из aria-label, а не из textContent"


def test_hhru_live_policy_core_cookie_informer_ponyatno_never_close():
    """#932 (живой DOM): cookie-информер несёт «cookie» только в data-qa;
    кнопка «Понятно» — согласие, а не close-контрол: кликаться не должна
    никогда, окно остаётся safe/no_close_control."""
    scenario = _run_policy_scenario("cookie_informer_ponyatno_never_close")
    assert scenario["type"] == "cookie_banner"
    assert scenario["disposition"] == "safe"
    assert scenario["closeCount"] == 0
    assert scenario["clickedAcceptSafe"] is True


def test_hhru_live_policy_core_real_hhru_cross_via_data_qa():
    """#932: реальные крестики hh.ru — data-qa *-close без aria-label и
    глифов (svg-иконка); плечо /close/ по data-qa обязано их находить, а
    «Сохранить» — не close-контрол."""
    scenario = _run_policy_scenario("real_hhru_cross_via_data_qa")
    assert scenario["closeCount"] == 1
    assert scenario["onlyCross"] is True
    assert scenario["disposition"] == "safe"


def test_hhru_live_policy_core_danger_outranks_apply():
    """Fail-closed приоритет: якоря опасности бьют apply-сигналы той же
    модалки — «подтвердите/необратимо» поверх формы отклика => dangerous."""
    scenario = _run_policy_scenario("danger_outranks_apply")
    assert scenario["disposition"] == "dangerous"


def test_hhru_live_policy_core_apply_step_structural():
    """Структурные apply-якоря (form#RESPONSE_MODAL_FORM_ID + data-qa
    vacancy-response) => apply_step, без danger-текста."""
    scenario = _run_policy_scenario("apply_step_structural")
    assert scenario["disposition"] == "apply_step"


def test_hhru_live_policy_core_remote_work_not_dangerous():
    """«удалённая работа» — не dangerous (голый стем-удал не якорь, PR #935
    review); модалка без close-контроля уходит в ambiguous, не в угаданный
    safe."""
    scenario = _run_policy_scenario("remote_work_not_dangerous")
    assert scenario["disposition"] == "ambiguous"


# ---------------------------------------------------------------------------
# Транспорт agent-канала (issue #931): popup -> background relay -> активная
# hh.ru-вкладка. Сценарии исполняют background.js по-настоящему через
# tests/js_harness/run_background_scenario.js <name> (стаб chrome.tabs/
# storage.session; до этого relay был покрыт только grep-гвардами).
# ---------------------------------------------------------------------------

BACKGROUND_RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_background_scenario.js"


def _run_background_scenario(name: str) -> dict:
    return _run_node_scenario(BACKGROUND_RUNNER, [name])


def test_hhru_live_relay_forwards_allowlisted_command_to_hhru_tab():
    scenario = _run_background_scenario("relay_forwards_to_hhru_tab")
    assert scenario["response"]["ok"] is True
    assert scenario["sentToTabCount"] == 1
    assert scenario["sentAction"] == "list_overlays"
    assert scenario["tabReplyPreserved"]


def test_hhru_live_relay_refuses_without_hhru_tab():
    scenario = _run_background_scenario("relay_no_hhru_tab")
    assert scenario["error"] == "no_hhru_tab"
    assert scenario["sentToTabCount"] == 0


def test_hhru_live_relay_reports_content_script_unreachable():
    scenario = _run_background_scenario("relay_content_script_unreachable")
    assert scenario["error"] == "content_script_unreachable"


def test_hhru_live_relay_rejects_foreign_sender_and_unknown_action():
    foreign = _run_background_scenario("relay_rejects_foreign_sender")
    assert foreign["error"] == "sender_not_allowed"
    assert foreign["sentToTabCount"] == 0
    unknown = _run_background_scenario("relay_rejects_unknown_action")
    assert unknown["error"] == "action_not_allowed"
    assert unknown["sentToTabCount"] == 0


def test_hhru_live_relay_keeps_diagnostics_storage_path():
    stored = _run_background_scenario("diagnostics_stored")
    assert stored["responseOk"] is True
    assert stored["storedReports"] == 1
    assert stored["storedKind"] == "overlay_detected"
    foreign = _run_background_scenario("diagnostics_foreign_origin_rejected")
    assert foreign["error"] == "sender_not_allowed"
    assert foreign["storedReports"] == 0
