// Stage 1 of issue #588 on top of the #644/#743 detection skeleton:
// overlay detection/registry + allowlisted commands + closing of SAFE
// overlays only.
//
// Classification policy (fail-closed: dangerous / apply_step / safe /
// ambiguous) and the close-control finder live in policy.js, loaded by
// manifest.json BEFORE this file — its top-level constants and functions
// (danger anchors, close markers, disposition) are policy.js bindings
// shared within the same isolated world and consumed below as globals.
//
// The ONLY click this content script ever performs is on an explicit
// close control (aria-label/title "close", data-qa/class "close", or a
// × glyph) of an overlay classified `safe` at the moment of dismissal —
// see dismissOverlay(). It never presses «Сохранить»/«Отмена»/submit
// buttons and never removes DOM nodes.

const ACTION_ALLOWLIST = new Set(['list_overlays', 'dismiss_overlay', 'check_element']);
// Detection surface (what counts as a potential overlay at all) is
// observation, not policy — it stays here. #932, live DOM 2026-09-05
// (анонимная главная): информер cookies — это
// div[data-qa="cookies-policy-informer"] с классом wrapper--* («cookie» в
// классе НЕТ), так что [class*="cookie"] его не находит. Внутри — кнопка
// «Понятно» (data-qa="cookies-policy-informer-accept", НЕ close-маркер:
// dismiss вернёт no_close_control и не кликнет согласие).
const OVERLAY_SELECTORS = [
  '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]',
  '[class*="modal"]', '[class*="popup"]', '[class*="toast"]',
  '[class*="notification"]', '[class*="cookie"]',
  '[data-qa*="cookie"]'
];
const OVERLAY_GONE_POLL_MS = 100;
const OVERLAY_GONE_TIMEOUT_MS = 2000;

function isAttached(element) {
  // Climb to the root of the tree: an attached node's root is the Document
  // itself (nodeType 9), NOT documentElement — documentElement.parentNode is
  // the document, so a === documentElement comparison is never true in a real
  // browser (found on live hh.ru: the registry pruned every entry, so
  // list_overlays was always empty and dismiss always overlay_not_found).
  let node = element;
  while (node.parentNode) node = node.parentNode;
  return node.nodeType === 9;
}

// Registry of overlays reported by the observer, keyed by stable id so the
// agent can address a specific overlay. Visibility/attachment are re-checked
// (and stale entries pruned) on every listing; disposition is always
// recomputed at decision time, never trusted from detection time.
const registry = new Map();
let registrySeq = 0;

function describeOverlay(id, element) {
  const info = classify(element);
  return {
    id,
    ...info,
    disposition: classifyDisposition(element, info),
    closeControls: findCloseControls(element).length
  };
}

function pruneRegistry() {
  for (const [id, entry] of registry) {
    if (!isAttached(entry.element) || !isVisible(entry.element)) registry.delete(id);
  }
}

function listOverlays() {
  pruneRegistry();
  return Array.from(registry.entries()).map(([id, entry]) => describeOverlay(id, entry.element));
}

function sendReport(id, info, element) {
  chrome.runtime.sendMessage({
    kind: 'overlay_detected',
    observedAt: new Date().toISOString(),
    overlay: { ...info, id, disposition: classifyDisposition(element, info) }
  });
}

function report(element) {
  // Re-use the existing entry when the same element re-surfaces via
  // hide->show: `seen` is intentionally per-visibility (see
  // reportIfNewlyVisible), so the node reports again — but a fresh id would
  // list one DOM node twice, the stale entry staying visible so pruneRegistry
  // never removes it (PR #935 review).
  for (const [existingId, entry] of registry) {
    if (entry.element === element) {
      entry.info = classify(element);
      sendReport(existingId, entry.info, element);
      return;
    }
  }
  const id = `overlay-${++registrySeq}`;
  const info = classify(element);
  registry.set(id, { element, info });
  sendReport(id, info, element);
}

// `seen` gates a report only while the element stays visible: once it goes
// hidden it is dropped from the set, so a later show (same DOM node, same
// attribute toggle) is treated as a fresh detection instead of being
// permanently blocked. This is what makes the `attributes: true` observer
// (added for hide/show toggles) actually useful across repeat show events.
function reportIfNewlyVisible(node, seen) {
  // The page shell is never an overlay: hh.ru marks the cookie banner with a
  // state class on <body> (cookie-policy-banner-enabled, confirmed live
  // 2026-09-02), which [class*="cookie"] matches — registering it reports the
  // whole page text and misclassifies everything. Confirmed live in #932
  // follow-up territory; the guard belongs here, the single choke point both
  // the initial scan and every mutation go through.
  if (node === document.documentElement || node === document.body) return;
  if (!isVisible(node)) {
    seen.delete(node);
    return;
  }
  if (seen.has(node)) return;
  seen.add(node);
  report(node);
}

function inspect(node, seen) {
  if (!(node instanceof Element)) return;
  if (OVERLAY_SELECTORS.some((selector) => node.matches(selector))) {
    reportIfNewlyVisible(node, seen);
  }
  node.querySelectorAll(OVERLAY_SELECTORS.join(',')).forEach((el) => {
    reportIfNewlyVisible(el, seen);
  });
}

const seen = new WeakSet();
document.querySelectorAll(OVERLAY_SELECTORS.join(',')).forEach((el) => {
  reportIfNewlyVisible(el, seen);
});
const observer = new MutationObserver((mutations) => {
  mutations.forEach(({ type, target, addedNodes }) => {
    if (type === 'attributes') { inspect(target, seen); return; }
    addedNodes.forEach((node) => inspect(node, seen));
  });
});
observer.observe(document.documentElement, {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ['class', 'style', 'hidden', 'aria-hidden']
});

function checkElement(selector) {
  if (!selector || typeof selector !== 'string') {
    return { found: false, visible: false, obstructionChecked: false };
  }
  const element = document.querySelectorAll(selector)[0] || null;
  if (!element) return { found: false, visible: false, obstructionChecked: false };
  const visible = isVisible(element);
  let covered = null;
  let obstructionChecked = false;
  // Real-browser obstruction probe: the element at the overlay's center point
  // must be the element itself or a descendant. Skipped (reported as such)
  // where the APIs are unavailable, e.g. in the Node test stub.
  if (visible && typeof document.elementFromPoint === 'function'
    && typeof element.getBoundingClientRect === 'function') {
    const rect = element.getBoundingClientRect();
    if (rect) {
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      // hit === null means the center point is off-viewport: the probe checked
      // nothing, so report "not checked" rather than a misleading
      // covered=false that an agent would read as "clear to click"
      // (PR #935 review).
      if (hit) {
        obstructionChecked = true;
        covered = hit !== element && !(typeof element.contains === 'function' && element.contains(hit));
      }
    }
  }
  return { found: true, visible, covered, obstructionChecked };
}

function waitForOverlayGone(element, onDone) {
  const startedAt = Date.now();
  const poll = () => {
    if (!isAttached(element) || !isVisible(element)) { onDone(true); return; }
    if (Date.now() - startedAt >= OVERLAY_GONE_TIMEOUT_MS) { onDone(false); return; }
    setTimeout(poll, OVERLAY_GONE_POLL_MS);
  };
  poll();
}

// The single mutating path in this content script. Everything before the
// final control.click() is fail-closed gating; control.click() is the only
// click() call in this file, and it always targets a close control of an
// overlay re-classified as `safe` at this exact moment.
function dismissOverlay(id, params, sendResponse) {
  const entry = registry.get(id);
  // Not attached OR already invisible at decision time: nothing to dismiss,
  // and clicking an invisible control is an action without evidence behind it.
  if (!entry || !isAttached(entry.element) || !isVisible(entry.element)) {
    sendResponse({ ok: false, error: 'overlay_not_found', id });
    return;
  }
  const info = classify(entry.element);
  const disposition = classifyDisposition(entry.element, info);
  if (disposition !== 'safe') {
    sendResponse({ ok: false, error: 'overlay_not_safe', disposition, overlay: info });
    return;
  }
  // The first close marker in document order may be a hidden template/duplicate
  // (display:none); clicking an invisible control is exactly the action
  // without evidence this file refuses to do, so only visible controls count
  // (PR #935 review).
  const controls = findCloseControls(entry.element).filter(isVisible);
  if (controls.length === 0) {
    sendResponse({ ok: false, error: 'no_close_control', disposition, overlay: info });
    return;
  }
  const control = controls[0];
  control.click();
  waitForOverlayGone(entry.element, (gone) => {
    const result = {
      overlayId: id,
      type: info.type,
      disposition,
      action: `clicked close control: ${(control.getAttribute('data-qa')
        || control.getAttribute('aria-label') || control.textContent || '').trim().slice(0, 100)}`,
      overlayGone: gone,
      elements: { closeControls: controls.length },
      finalState: params && params.selector
        ? { nextElement: checkElement(params.selector) }
        : { nextElement: null }
    };
    sendResponse({ ok: true, result });
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !ACTION_ALLOWLIST.has(message.action)) {
    sendResponse({ ok: false, error: 'action_not_allowed', action: message?.action ?? null });
    return false;
  }
  // Commands are accepted only from this extension itself (popup/background
  // relay), never from the page or another extension.
  if (sender.id !== chrome.runtime.id) {
    sendResponse({ ok: false, error: 'sender_not_allowed' });
    return false;
  }
  if (message.action === 'list_overlays') {
    sendResponse({ ok: true, overlays: listOverlays() });
    return false;
  }
  if (message.action === 'check_element') {
    sendResponse({ ok: true, element: checkElement(message.selector) });
    return false;
  }
  // dismiss_overlay answers asynchronously after the close click + re-check.
  dismissOverlay(message.id, message, sendResponse);
  return true;
});
chrome.runtime.sendMessage({ kind: 'connected', url: location.href, observedAt: new Date().toISOString() });
