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
import re
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
    obstruction-проба в стабе недоступна и честно репортится как непроверенная.
    matchCount (#1006): неоднозначный селектор не должен выглядеть как однозначный."""
    scenario = _run_command_scenario("check_element")
    assert scenario["found"] and scenario["visible"]
    assert scenario["obstructionChecked"] is False
    assert scenario["matchCount"] == 2
    assert scenario["absentFound"] is False and scenario["noSelectorFound"] is False
    assert scenario["absentMatchCount"] == 0 and scenario["noSelectorMatchCount"] == 0


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


def test_hhru_live_relay_reports_response_lost_when_port_closed():
    """Боевой флейк #1181: команда ДОСТАВЛЕНА во вкладку, но ответ потерян
    (клик мог случиться — страница ушла в навигацию посреди ожидания). MV3
    отдаёт другой текст lastError, и релей обязан ответить response_lost, а не
    content_script_unreachable (который читается как «клика не было» и
    приглашает повторный клик)."""
    scenario = _run_background_scenario("relay_response_port_closed")
    assert scenario["error"] == "response_lost"


def test_hhru_live_relay_alarm_reconnects_disconnected_bridge():
    """SW-idle (#1181): периодический alarm пересоздаёт соединение после сна SW
    (reconnect-таймеры умирают вместе с ним) и не плодит соединения, пока сокет
    ещё жив/подключается."""
    scenario = _run_background_scenario("alarm_created_and_reconnects")
    assert scenario["created"]["name"] == "hhru-live-reconnect"
    assert scenario["created"]["periodInMinutes"] == 0.5
    assert (scenario["connected"], scenario["afterAlarm"], scenario["still"]) == (1, 2, 2)


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


# ---------------------------------------------------------------------------
# Исполнитель команд в живой вкладке (issue #1160, этап 2): click_element /
# wait_element / get_page_state поверх policy-ядра #929. Сценарии исполняют
# весь extension (policy.js + executor.js + content.js) по-настоящему через
# tests/js_harness/run_executor_scenario.js <name>. Ключевой инвариант:
# dangerous/apply_step/ambiguous и клик без объявленного post-click условия
# отказывают БЕЗ какого-либо клика.
# ---------------------------------------------------------------------------

EXECUTOR_RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_executor_scenario.js"


def _run_executor_scenario(name: str) -> dict:
    return _run_node_scenario(EXECUTOR_RUNNER, [name])


def test_hhru_live_executor_click_safe_overlay_allowed():
    """Разрешённый клик: контроль внутри safe-overlay, клик ровно по цели,
    объявленное post-click условие дождалось, результат структурирован
    (действие, policy-вердикт, цель, wait, итоговое состояние DOM)."""
    scenario = _run_executor_scenario("click_safe_allowed")
    assert scenario["ok"] is True
    assert scenario["verdict"] == "allowed" and scenario["context"] == "safe_overlay"
    assert scenario["clicked"] is True and scenario["waitMet"] is True
    assert scenario["clickCount"] == 1 and scenario["clickedDetails"] is True
    assert scenario["targetText"] == "Подробнее"
    assert scenario["url"].startswith("https://hh.ru/")
    assert scenario["overlaysAfter"] >= 1


def test_hhru_live_executor_click_dangerous_refused_without_click():
    """Якоря опасности на тексте цели — отказ без клика: и внутри модалки,
    и «голой» кнопкой на странице (collectText включает поддерево)."""
    scenario = _run_executor_scenario("click_dangerous_refused")
    assert scenario["modalError"] == "policy_refused" and scenario["modalReason"] == "dangerous"
    assert scenario["pageError"] == "policy_refused" and scenario["pageReason"] == "dangerous"
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_click_apply_step_refused_without_click():
    """apply-сигналы (data-qa vacancy-response) — отказ без клика БЕЗ явной
    авторизации: ключ сценария отклика — allowApply команды агента (#1162)."""
    scenario = _run_executor_scenario("click_apply_step_refused")
    assert scenario["error"] == "policy_refused" and scenario["reason"] == "apply_step"
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_allow_apply_clicks_only_apply_step():
    """#1162: allowApply понижает ТОЛЬКО apply_step-отказ цели (context
    apply_flow, клик выполняется); якорь опасности и ambiguous-предок
    отказывают и с allowApply — гейты этапа 1 не ослаблены."""
    scenario = _run_executor_scenario("click_apply_allowed_only_with_permission")
    assert scenario["allowedOk"] and scenario["allowedContext"] == "apply_flow"
    assert scenario["allowedClicked"] and scenario["clickedApply"]
    assert scenario["dangerError"] == "policy_refused" and scenario["dangerReason"] == "dangerous"


def test_hhru_live_executor_fill_element_sets_value():
    """#1162: текст письма проходит через native setter и читается обратно;
    расхождение — ok:false, а не молчаливая полузаполненная форма."""
    scenario = _run_executor_scenario("fill_element_sets_value")
    assert scenario["ok"] and scenario["filled"]
    assert scenario["valueInField"] and scenario["length"] == len(scenario["valueInField"])


def test_hhru_live_executor_fill_element_dangerous_refused():
    """Пустой input meanings в атрибутах: data-qa с captcha — отказ без
    записи значения, гейт опасности у fill сильнее текстового скана."""
    scenario = _run_executor_scenario("fill_element_dangerous_refused")
    assert scenario["error"] == "policy_refused" and scenario["reason"] == "dangerous"
    assert scenario["untouched"]


def test_hhru_live_executor_click_ambiguous_overlay_refused():
    """Контроль внутри неклассифицируемой модалки — ambiguous, отказ без
    клика; вердикт возвращается агенту на решение (fail-closed)."""
    scenario = _run_executor_scenario("click_ambiguous_overlay_refused")
    assert scenario["error"] == "policy_refused" and scenario["reason"] == "ambiguous"
    assert scenario["overlayType"] == "modal"
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_click_without_wait_refused():
    """visible != гидратирован (CLAUDE.md): клик без объявленного
    post-click условия отказывается ДО клика — no wait declaration, no
    click. Иначе исход клика, запускающего React-рендер, не доказуем."""
    scenario = _run_executor_scenario("click_without_wait_refused")
    assert scenario["error"] == "wait_required"
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_click_ambiguous_target_refused():
    """Дубликаты одного data-qa (шапка + липкая панель hh.ru, прогон #1162) —
    один контрол в нескольких местах: кликается первый видимый — и по dataQa,
    и по чистому [data-qa='X']. Неточный селектор ([data-qa*='...']) остаётся
    ambiguous (fail-closed)."""
    scenario = _run_executor_scenario("click_ambiguous_target")
    assert scenario["sameQaClicked"] is True and scenario["sameQaText"] == "Один"
    assert scenario["cssClicked"] is True
    assert scenario["fuzzyError"] == "ambiguous_target" and scenario["fuzzyMatchCount"] == 2
    assert scenario["clickCount"] == 2


def test_hhru_live_executor_apply_modal_overlay_allowed_with_permission():
    """Боевой факт #1162: ответная модалка классифицируется apply_step, а
    пикер/письмо/submit сидят внутри неё — allowApply понижает и apply_step
    оверлея-предка (apply_flow_overlay). Без флага — отказ без клика."""
    scenario = _run_executor_scenario("click_apply_modal_overlay_allowed_with_permission")
    assert scenario["allowedContext"] == "apply_flow_overlay"
    assert scenario["allowedClicked"] is True
    assert scenario["noFlagError"] == "policy_refused" and scenario["noFlagReason"] == "apply_step"


def test_hhru_live_executor_nested_overlay_family_collapsed_to_topmost():
    """Боевой факт #1214: одна модалка — семейство вложенных overlay-узлов
    (outer apply_step, inner ambiguous), клик бьёт по inner. Вердикт снимается
    по ВЕРХНЕМУ overlay-предку: apply_step наружного понижается allowApply, а
    safe-inner больше не маскирует dangerous-наружную (прежде ближайший
    safe-предок позволил бы клик внутри опасной модалки)."""
    scenario = _run_executor_scenario("click_nested_family_collapsed_to_topmost")
    assert scenario["allowedContext"] == "apply_flow_overlay"
    assert scenario["clicked"] is True and scenario["clickedPicker"] is True
    assert scenario["refusedError"] == "policy_refused"
    assert scenario["refusedReason"] == "dangerous"
    assert scenario["refusedOverlayType"] == "modal"
    assert scenario["clickedInnerOk"] is False


def test_hhru_live_executor_click_invisible_target_refused():
    """Действие по невидимому контролю — действие без оснований: отказ до
    policy-гейта, кликов 0."""
    scenario = _run_executor_scenario("click_invisible_target")
    assert scenario["error"] == "element_not_visible"
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_wait_element_appears():
    """wait_element 'visible': элемент появляется в течение явного таймаута;
    появившийся overlay попадает в реестр детектора — MutationObserver
    продолжает работать во время исполнения команд (#1160 п.5)."""
    scenario = _run_executor_scenario("wait_element_appears")
    assert scenario["ok"] is True
    assert scenario["waitMet"] is True and scenario["state"] == "visible"
    assert scenario["matchCount"] == 1 and scenario["visible"] is True
    assert scenario["overlayListed"] is True
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_wait_element_timeout_is_result_not_error():
    """Истёкший явный таймаут — честный отрицательный результат (ok,
    met:false), не ошибка и не бесконечный опрос."""
    scenario = _run_executor_scenario("wait_element_timeout")
    assert scenario["ok"] is True and scenario["waitMet"] is False
    assert scenario["elapsedMs"] >= 100
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_wait_requires_state_and_explicit_timeout():
    """Таймауты явные (#1160 п.3): без state или без timeoutMs ожидание не
    начинается."""
    scenario = _run_executor_scenario("wait_element_validation")
    assert scenario["noStateError"] == "state_required"
    assert scenario["noTimeoutError"] == "timeout_required"
    assert scenario["noTargetError"] == "target_required"
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_get_page_state():
    """Чтение DOM-состояния без действий: текущий URL и ноль кликов."""
    scenario = _run_executor_scenario("get_page_state")
    assert scenario["ok"] is True
    assert scenario["url"] == "https://hh.ru/vacancy/1"
    assert scenario["clickCount"] == 0


def test_hhru_live_executor_check_element_reports_text():
    """check_element дополняется census-полем text (#1160 п.4: наличие/
    текст/visibility; сырой HTML по-прежнему наружу не уходит)."""
    scenario = _run_executor_scenario("check_element_reports_text")
    assert scenario["found"] is True and scenario["text"] == "Далее"
    assert scenario["absentText"] is None


# ---------------------------------------------------------------------------
# Транспорт расширения — loopback WebSocket-мост (issue #1160 поверх #1159):
# envelope {v, id, action, payload} -> {id, status, result}. Сценарии
# исполняют background.js по-настоящему через
# tests/js_harness/run_ws_bridge_scenario.js <name> (стаб WebSocket + tabs).
# ---------------------------------------------------------------------------

WS_BRIDGE_RUNNER = REPO_ROOT / "tests" / "js_harness" / "run_ws_bridge_scenario.js"


def _run_ws_bridge_scenario(name: str) -> dict:
    return _run_node_scenario(WS_BRIDGE_RUNNER, [name])


def test_hhru_live_bridge_relays_envelope_and_answers():
    scenario = _run_ws_bridge_scenario("envelope_relayed")
    assert scenario["sentToTabCount"] == 1
    assert scenario["sentAction"] == "check_element"
    assert scenario["sentSelector"] == '[data-qa="x"]'
    assert scenario["envelopes"] == [
        {
            "id": "c1",
            "status": "ok",
            "result": {"element": {"found": True, "visible": True}},
        }
    ]


def test_hhru_live_bridge_rejects_unknown_version_without_tab():
    scenario = _run_ws_bridge_scenario("unsupported_version_rejected")
    assert scenario["sentToTabCount"] == 0
    assert scenario["envelopes"] == [
        {
            "id": "c2",
            "status": "error",
            "result": {"code": "unsupported_version", "receivedV": 99},
        }
    ]


def test_hhru_live_bridge_rejects_unknown_action_without_tab():
    scenario = _run_ws_bridge_scenario("unknown_action_rejected")
    assert scenario["sentToTabCount"] == 0
    assert scenario["envelopes"][0]["status"] == "error"
    assert scenario["envelopes"][0]["result"]["code"] == "action_not_allowed"


def test_hhru_live_bridge_echoes_integer_command_id():
    """Серверный протокол допускает целочисленные id (protocol._is_valid_id):
    ответ обязан эхоить тот же id — раньше мост отвечал command_id_required
    с null-id, и вызывающий сгорал по полному таймауту ответа (#1178 review)."""
    scenario = _run_ws_bridge_scenario("integer_id_echoed")
    assert scenario["sentToTabCount"] == 1
    assert scenario["envelopes"] == [{"id": 7, "status": "ok", "result": {"overlays": []}}]


def test_hhru_live_bridge_whitelists_payload_fields():
    """Мост копирует в команду только перечисленные скалярные поля; прочее
    содержимое payload наружу не проходит."""
    scenario = _run_ws_bridge_scenario("payload_fields_whitelisted")
    command = scenario["command"]
    assert scenario["sentToTabCount"] == 1
    assert command["action"] == "click_element"
    assert command["dataQa"] == "x"
    assert command["waitFor"] == {"state": "hidden", "timeoutMs": 500}
    assert "evil" not in command
    assert "evil" not in command["waitFor"]


def test_hhru_live_bridge_reconnects_after_drop():
    """Разрыв соединения — не ошибка сценария (#1159 п.3): мост планирует
    reconnect и продолжает отвечать через новое соединение."""
    scenario = _run_ws_bridge_scenario("reconnect_after_drop")
    assert scenario["secondSocket"] is True
    assert scenario["firstReadyState"] == 3
    assert scenario["sentToTabCount"] == 1
    assert scenario["envelopes"] == [{"id": "c5", "status": "ok", "result": {"overlays": []}}]


def test_hhru_live_bridge_maps_relay_errors_into_envelope():
    scenario = _run_ws_bridge_scenario("no_hhru_tab_ws")
    assert scenario["sentToTabCount"] == 0
    assert scenario["envelopes"] == [
        {
            "id": "c6",
            "status": "error",
            "result": {"error": "no_hhru_tab"},
        }
    ]


def test_hhru_live_bridge_heartbeats_only_when_open():
    scenario = _run_ws_bridge_scenario("heartbeat_only_when_open")
    assert scenario["framesBeforeOpen"] == 0
    assert scenario["heartbeatCount"] >= 1


def test_hhru_live_bridge_announces_hello_diagnostics():
    """Handshake-диагностика (#1163): первым кадром после open идёт hello с
    версией протокола, allowlist и permissions манифеста — источник live-doctor
    для проверок версии/allowlist/permissions."""
    scenario = _run_ws_bridge_scenario("hello_announced_on_open")
    hello = scenario["hello"]
    assert scenario["helloIsFirstFrame"] is True
    assert hello is not None
    assert hello["v"] == 1
    # Sorted: сервер и doctor сравнивают множества, порядок — детерминизм кадра.
    assert hello["actions"] == sorted(
        [
            "list_overlays",
            "dismiss_overlay",
            "check_element",
            "click_element",
            "wait_element",
            "get_page_state",
            "fill_element",
        ]
    )
    assert hello["permissions"] == ["storage", "alarms"]
    assert hello["hostPermissions"] == ["https://hh.ru/*", "https://*.hh.ru/*"]


def test_hhru_live_bridge_reconnects_on_browser_startup():
    """onStartup будит SW и подключает канал (#1163): без слушателя SW стартует
    только по install/сообщению контент-скрипта, и live-doctor/bump-live,
    запущенные до открытия вкладки hh.ru, расширения не видят."""
    scenario = _run_ws_bridge_scenario("startup_reconnect")
    assert scenario["listenerRegistered"] is True
    assert scenario["newSocketCreated"] is True


def test_hhru_live_bridge_answers_keepalive_ping():
    """#1187/#1197: keep-alive пинг контент-скрипта обязан доходить до
    background и получать ответ. Каждое сообщение контент-скрипта будит
    заснувший MV3 SW (module eval перезапускает connectLiveServe) и сбрасывает
    его ~30-секундный idle-таймер, поэтому пинг раз в 20 с держит воркера —
    и его reconnect-цепочку на setTimeout, — живыми, пока открыта вкладка
    hh.ru: сервер, поднятый после запуска браузера, больше не пропускается.
    Чужой origin по-прежнему за гейтом."""
    scenario = _run_ws_bridge_scenario("keepalive_answered")
    assert scenario["response"] == {"ok": True}
    assert scenario["foreign"] == {"ok": False, "error": "sender_not_allowed"}


def test_hhru_live_bridge_keepalive_expedites_reconnect():
    """Ревью #1203 (Codex P1): при закрытом сокете пинг не просто отвечает —
    он немедленно даёт попытку соединения, не оставляя следующий ретрай на
    30-секундном потолке backoff (сервер live-doctor держит порт всего 10 с,
    и промах по окну возвращал бы ложный [FAIL] даже при живом воркере).
    Чужой origin состояние моста не трогает; подключённый мост лишних сокетов
    не плодит."""
    scenario = _run_ws_bridge_scenario("keepalive_expedites_reconnect")
    assert scenario["expedited"] is True
    assert scenario["connectedResponse"] == {"ok": True}
    assert scenario["noExtraSocketWhileConnected"] is True


def test_hhru_live_content_script_sends_keepalive_ping():
    """#1187/#1197: пинг живёт в content.js (живёт, пока открыта вкладка),
    а не в background.js (его таймеры умирают вместе с SW). Интервал — под
    30-секундным idle-окном MV3."""
    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    background = (root / "background.js").read_text()
    assert "setInterval" in content
    assert "kind: 'keepalive'" in content
    assert "keepalive" in background
    # Пинг чаще idle-окна: 20000 < 30000. Небольшой потолок сверху оставлен
    # явным, чтобы «оптимизация» интервала до 45 с поймалась ревью/тестом.
    match = re.search(r"KEEPALIVE_INTERVAL_MS = (\d+)", content)
    assert match is not None
    assert int(match.group(1)) < 30000
