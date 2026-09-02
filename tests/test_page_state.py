"""Общий контракт неопределённого состояния браузерных страниц (#143)."""

import pytest

from hhru_bot.apply.questions import QuestionDetection
from hhru_bot.browser import PAGE_STATE, PageStateIndeterminate
from hhru_bot.commands.probe import PageCheck
from hhru_bot.copy_resume import ResumeListIndeterminate
from hhru_bot.responses import NotAuthenticated

pytestmark = pytest.mark.unit


def test_browser_paths_share_indeterminate_exception_base():
    assert issubclass(NotAuthenticated, PageStateIndeterminate)
    assert issubclass(ResumeListIndeterminate, PageStateIndeterminate)


def test_flagged_page_states_use_common_vocabulary():
    assert QuestionDetection.no().page_state == PAGE_STATE["confirmed"]
    assert (
        QuestionDetection.indeterminate_scope("неизвестно").page_state
        == PAGE_STATE["indeterminate"]
    )
    assert PageCheck("x", "https://hh.ru", unreachable=True).page_state == PAGE_STATE["unreachable"]


def test_hhru_live_extension_manifest_and_detector_contract():
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["permissions"] == ["storage"]
    assert manifest["background"]["service_worker"] == "background.js"
    source = (root / "content.js").read_text()
    for selector in (
        '[role="dialog"]',
        '[role="alertdialog"]',
        '[class*="toast"]',
        '[class*="notification"]',
        '[class*="cookie"]',
    ):
        assert selector in source
    assert "MutationObserver" in source
    assert "element.remove" not in source
    # Issue #588 stage 1: единственный клик в content.js — по close-контролу
    # safe-overlay внутри dismissOverlay, и он стоит ПОСЛЕ fail-closed гейта
    # `overlay_not_safe` (любой не-safe disposition выходит раньше клика).
    assert source.count("control.click();") == 1, (
        "click() разрешён ровно один раз — по close-контролу внутри dismissOverlay"
    )
    assert source.index("overlay_not_safe") < source.index("control.click();")


def test_hhru_live_extension_transport_allowlist_is_exact():
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    background = (root / "background.js").read_text()
    allowlist = "new Set(['list_overlays', 'dismiss_overlay', 'check_element'])"
    # Ровно три действия MVP (детект живёт сам, без команд); всё прочее —
    # action_not_allowed. Новый дефолт вместо пустого allowlist эпохи #644.
    assert allowlist in content
    assert allowlist in background, (
        "RELAY_ACTIONS в background.js должен зеркалить ACTION_ALLOWLIST "
        "content.js — две копии обязаны совпадать дословно"
    )
    assert "name !== 'hhru-agent'" in background
    assert "action_not_allowed" in background


def test_hhru_live_extension_danger_anchors_outrank_apply_anchors():
    """Fail-closed приоритет классификации: опасное окно остаётся опасным,
    даже если в нём есть сигналы формы отклика. DANGEROUS_TEXT проверяется
    строго раньше hasApplySignal."""
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    # Обе метки — вызовы внутри classifyDisposition: DANGEROUS_TEXT.some там
    # один, 'apply_step' впервые возвращается сразу после hasApplySignal.
    danger_check = content.index("DANGEROUS_TEXT.some")
    apply_check = content.index("return 'apply_step'")
    assert danger_check < apply_check


def test_hhru_live_extension_danger_and_apply_text_anchors_are_narrow():
    """Суженные якоря (PR #935 review):
    - /удал/i запрещён: подстрока матчит «удалённая работа» и навсегда
      делает рутинные попапы dangerous; разрешены только /удалить|удалени/.
    - голое /отклик/i запрещено в APPLY_TEXT: тост «Отклик отправлен» —
      штатное подтверждение после submit и должно оставаться safe-dismiss;
      форму отклика покрывают структурные якоря."""
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    assert "/удал/i" not in content
    assert "/удалить|удалени/i" in content
    assert "/отклик/i" not in content


def test_hhru_live_extension_close_markers_exclude_action_buttons():
    """Close-контролы — только явные close-маркеры. Слова-действия
    («Сохранить», «Отмена», «Понятно», «Принять») не входят и не должны
    появиться в списке паттернов: клик по ним меняет данные пользователя
    (#586: «Сохранить» в тосте «Резюме доставлено» — выбор статуса поиска)."""
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    # Страж смотрит только в findCloseControls: в шапке файла те же слова
    # упомянуты легитимно — в описании того, куда кликать запрещено.
    start = content.index("function findCloseControls")
    end = content.index("function hasApplySignal")
    close_marker_body = content[start:end]
    for word in ("Понятно", "Отмена", "Сохранить", "Принять"):
        assert word not in close_marker_body, (
            f"«{word}» не может быть close-маркером — только крестик/close-aria/data-qa"
        )


def test_hhru_live_extension_declares_storage_permission():
    """Codex review of PR #644, finding 1: without "storage" permission
    chrome.storage is undefined, so background.js's write and popup.js's
    read both silently no-op and the diagnostics popup never shows anything.
    """
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    manifest = json.loads((root / "manifest.json").read_text())
    assert "storage" in manifest["permissions"]


def test_hhru_live_extension_scans_existing_dom_before_observing():
    """Codex review of PR #644, finding 2: MutationObserver only reports
    future childList mutations. Without an initial scan, an overlay already
    present at document_idle (e.g. a server-rendered cookie banner) is never
    detected.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    observe_index = content.index("observer.observe(")
    scan_index = content.index("document.querySelectorAll(OVERLAY_SELECTORS")
    assert scan_index < observe_index, (
        "initial DOM scan must run before observer.observe() attaches, "
        "otherwise overlays already in the DOM at document_idle are missed"
    )


def test_hhru_live_extension_observes_attribute_visibility_toggles():
    """Codex review of PR #644, finding 3: a childList-only observer misses
    overlays that are already mounted and revealed via a class/style/
    aria-hidden toggle instead of a DOM insertion (common for cookie
    banners and toasts).
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    assert "attributes: true" in content
    assert "attributeFilter" in content


def test_hhru_live_extension_dedupes_reports_by_element():
    """Codex review of PR #644, finding 4: an added subtree runs
    querySelectorAll over every added node, so an element added as a
    descendant of an already-matched ancestor can be reported twice.
    A per-batch WeakSet must gate report() so each element is reported at
    most once per mutation callback invocation.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    assert "WeakSet" in content


def test_hhru_live_extension_dedup_set_persists_across_mutation_batches():
    """Round-2 review of PR #644 (Codex + /review), finding: the dedup
    WeakSet was created fresh inside the MutationObserver callback, so an
    overlay whose attribute toggles across separate mutation batches
    (each a distinct callback invocation) was re-reported every batch.
    The seen-set must be declared outside the callback so it persists for
    the observer's lifetime.
    """
    import re
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    observer_decl = content.index("new MutationObserver(")
    weakset_decl = content.index("new WeakSet()")
    assert weakset_decl < observer_decl, (
        "the seen WeakSet must be declared before/outside the MutationObserver "
        "callback so it persists across callback invocations, not recreated per batch"
    )
    # No second, per-callback WeakSet re-creation.
    assert len(re.findall(r"new WeakSet\(\)", content)) == 1


def test_hhru_live_extension_classifies_cookie_banner_before_role_dialog():
    """Round-2 review of PR #644 (/review), finding: classify()'s ternary
    tested role === 'dialog'/'alertdialog' before the cookie-text check, so
    a cookie-consent banner marked role="dialog" (a common accessible
    pattern) was always misclassified as 'modal' instead of 'cookie_banner'.
    The cookie check must be evaluated first.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    cookie_check = content.index("/cookie/i.test")
    dialog_role_check = content.index("role === 'dialog'")
    assert cookie_check < dialog_role_check, (
        "cookie-text classification must be checked before the dialog/modal "
        'role check, so a role="dialog" cookie banner is still classified '
        "as cookie_banner"
    )


def test_hhru_live_extension_awaits_storage_write():
    """Round-2 review of PR #644 (/review), finding: chrome.storage.session
    .set() was fire-and-forget; a rejected write (e.g. quota exceeded) was
    silently swallowed while sendResponse still reported ok: true.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    background = (root / "background.js").read_text()
    assert "await chrome.storage.session" in background
    # A storage write can only be awaited from an async context; the
    # listener must keep the message channel open (`return true`) while it does.
    assert "return true;" in background


def test_hhru_live_extension_rehydrates_reports_on_worker_startup():
    """Round-2 review of PR #644 (Codex), finding: recentReports is an
    in-memory array never rehydrated from chrome.storage.session on
    service-worker startup; an MV3 SW restart silently truncates
    diagnostics history to just the next incoming report.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    background = (root / "background.js").read_text()
    assert "storage.session.get" in background or "storage.session?.get" in background


def test_hhru_live_extension_validates_sender_origin():
    """Round-2 review of PR #644 (Codex), finding: onMessage/onConnect
    accepted any sender without validating it came from an hh.ru tab,
    which is the wrong foundation for the planned action bridge.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    background = (root / "background.js").read_text()
    assert "hh.ru" in background or "hh\\.ru" in background


def test_hhru_live_extension_readme_notes_agent_channel_is_external_only():
    """Round-2 review of PR #644 (/review), finding: the README described
    the hhru-agent runtime.connect channel without noting that no code in
    the extension itself (content.js/popup.js) ever opens that port —
    it exists only for a future external caller.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    readme = (root / "README.md").read_text()
    assert "external" in readme.lower()


def test_hhru_live_extension_collapses_whitespace_with_single_backslash_regex():
    """Issue #743 finding 2: classify()'s text-collapse regex was
    written as double-backslash /\\\\s+/g in the source, which matches a
    literal backslash + 's' in DOM text — not whitespace. It must be the
    single-backslash /\\s+/g whitespace-class regex.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    assert ".replace(/\\s+/g, ' ')" in content
    assert ".replace(/\\\\s+/g, ' ')" not in content


def test_hhru_live_extension_reads_class_attribute_not_classname_property():
    """Issue #743 finding 4: element.className is an SVGAnimatedString
    (not a string) for SVG elements, so the old
    `typeof element.className === 'string'` guard was always false for
    SVG, silently degrading classification's className to ''. Read the
    class attribute directly instead, which works uniformly for HTML and
    SVG elements.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    content = (root / "content.js").read_text()
    assert "element.getAttribute('class')" in content
    assert "typeof element.className" not in content


def test_hhru_live_extension_readme_does_not_overclaim_external_reachability():
    """Issue #743 finding 3 (Option B — documentation only, no code fix
    yet, tracked as backlog): README previously claimed the hhru-agent
    channel is "reachable only from an external caller", but onConnect
    (not onConnectExternal) is registered and manifest.json has no
    externally_connectable key — the channel is not actually reachable
    by anyone yet. README must not overclaim reachability.
    """
    from pathlib import Path

    root = Path(__file__).parents[1] / "extensions" / "hhru-live"
    readme = (root / "README.md").read_text()
    assert "reachable only from an **external** caller" not in readme
    assert "#743" in readme
