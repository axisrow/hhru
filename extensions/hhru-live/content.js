// Stage 1 of issue #588 on top of the #644/#743 detection skeleton:
// classification + allowlisted commands + closing of SAFE overlays only.
//
// Policy (fail-closed):
//   dangerous  — captcha / irreversible-action confirmations / anything with
//                a danger text anchor. Never touched by this script.
//   apply_step — response-form modal, test-question bodies, questionnaire
//                wording. Part of the apply flow, never auto-dismissed.
//   safe       — toasts/notifications/cookie banners and, for modal/overlay
//                types, only when an explicit close control exists.
//   ambiguous  — modal/overlay with no explicit close control and no signal.
//                Blocked, returned to the agent for a manual decision.
//
// The ONLY click this content script ever performs is on an explicit
// close control (aria-label/title "close", data-qa/class "close", or a
// × glyph) of an overlay classified `safe` at the moment of dismissal —
// see dismissOverlay(). It never presses «Сохранить»/«Отмена»/submit
// buttons and never removes DOM nodes.

const ACTION_ALLOWLIST = new Set(['list_overlays', 'dismiss_overlay', 'check_element']);
const OVERLAY_SELECTORS = [
  '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]',
  '[class*="modal"]', '[class*="popup"]', '[class*="toast"]',
  '[class*="notification"]', '[class*="cookie"]'
];
// Danger anchors are matched against the overlay's own + descendant text.
// Intentionally narrow: a miss keeps the overlay merely blocked (safe side);
// the /удал|подтверд/ families also cover "подтвердите удаление" style
// confirmations. Known trade-off: a cookie banner phrased as «подтвердите
// согласие» would land here too — fail-closed, it stays and is reported.
const DANGEROUS_TEXT = [
  /captcha/i, /не робот/i,
  /удал/i, /отозвать/i, /отмена отклика/i, /withdraw/i,
  /необратим/i, /irreversible/i,
  /вы уверены/i, /are you sure/i,
  /подтверд/i, /confirm/i
];
// Apply-flow anchors: structural (form id, data-qa namespace, task-question
// classes — both response-modal shapes from CLAUDE.md) and text wording.
const APPLY_TEXT = [/сопроводительн/i, /тестовое задание/i, /анкет/i, /отклик/i];
const APPLY_QA = /vacancy-response/i;
const APPLY_CLASS = /task-question|task-body/i;
const APPLY_FORM_ID = /RESPONSE_MODAL_FORM_ID/;
const CLOSE_LABEL = /закрыт|close|dismiss/i;
const CLOSE_GLYPH = /^[×✕x]$/i;
const OVERLAY_GONE_POLL_MS = 100;
const OVERLAY_GONE_TIMEOUT_MS = 2000;

function isVisible(element) {
  // offsetWidth/offsetHeight/getClientRects() only react to display:none —
  // a visibility:hidden element still reports non-zero layout metrics, so it
  // would be treated as "visible" here (Codex round-3 review of #767: this
  // silently defeats the hide->show re-detect gate for that CSS pattern).
  // checkVisibility() (Chrome 105+) covers visibility/display/content-visibility
  // in one call; fall back to the layout check + an explicit visibility read
  // for older Chrome (MV3's floor is Chrome 88).
  if (typeof element.checkVisibility === 'function') {
    return element.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true });
  }
  const hasLayout = !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  if (!hasLayout) return false;
  return getComputedStyle(element).visibility !== 'hidden';
}

function classify(element) {
  const text = (element.innerText || element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 500);
  const role = element.getAttribute('role');
  const className = element.getAttribute('class') ?? '';
  const type = /cookie/i.test(className + text)
    ? 'cookie_banner' : role === 'dialog' || role === 'alertdialog' || /modal|popup/i.test(className)
    ? 'modal' : /toast|notification/i.test(className) ? 'notification' : 'overlay';
  return { type, text, role, className: className.slice(0, 200), visible: isVisible(element) };
}

// Overlay text plus every descendant's own text node. In a real browser
// element.textContent already includes the subtree (descendants double up,
// harmless for regex matching); the per-node loop keeps the js-harness stub,
// whose textContent is per-node, working with the same code path.
function collectText(element) {
  const parts = [element.textContent || ''];
  walkDescendants(element, (node) => parts.push(node.textContent || ''));
  return parts.join(' ').trim().replace(/\s+/g, ' ');
}

function walkDescendants(element, visit) {
  visit(element);
  (element.children || []).forEach((child) => walkDescendants(child, visit));
}

function findCloseControls(element) {
  const controls = [];
  walkDescendants(element, (node) => {
    if (node === element) return;
    const label = `${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''}`;
    // An explicit accessible "close" label is deliberate author intent and is
    // sufficient on its own; weaker markers (data-qa/class/glyph) must also
    // sit on something interactive.
    if (CLOSE_LABEL.test(label)) { controls.push(node); return; }
    const qa = (node.getAttribute('data-qa') || '').toLowerCase();
    const cls = (node.getAttribute('class') || '').toLowerCase();
    const role = (node.getAttribute('role') || '').toLowerCase();
    const text = (node.textContent || '').trim().replace(/\s+/g, ' ');
    const interactive = ['button', 'a'].includes(String(node.tagName).toLowerCase())
      || role === 'button' || qa !== '' || cls.includes('close');
    if (!interactive) return;
    if (/close/.test(qa) || /close/.test(cls) || CLOSE_GLYPH.test(text)) {
      controls.push(node);
    }
  });
  return controls;
}

function hasApplySignal(element, text) {
  if (APPLY_FORM_ID.test(element.getAttribute('id') || '')) return true;
  if (APPLY_TEXT.some((re) => re.test(text))) return true;
  let found = false;
  walkDescendants(element, (node) => {
    if (APPLY_QA.test(node.getAttribute('data-qa') || '')) found = true;
    if (APPLY_CLASS.test(node.getAttribute('class') || '')) found = true;
  });
  return found;
}

function classifyDisposition(element, info) {
  const text = collectText(element);
  if (DANGEROUS_TEXT.some((re) => re.test(text))) return 'dangerous';
  if (hasApplySignal(element, text)) return 'apply_step';
  // Toasts/notifications are harmless by nature even without a close
  // control (they auto-dismiss); modal-shaped overlays without an explicit
  // close control are ambiguous, never guessed at.
  if (info.type === 'notification' || info.type === 'cookie_banner') return 'safe';
  return findCloseControls(element).length > 0 ? 'safe' : 'ambiguous';
}

function isAttached(element) {
  let node = element;
  while (node.parentNode) node = node.parentNode;
  return node === document.documentElement;
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

function report(element) {
  const id = `overlay-${++registrySeq}`;
  const info = classify(element);
  registry.set(id, { element, info });
  const report = {
    kind: 'overlay_detected',
    observedAt: new Date().toISOString(),
    overlay: { ...info, id, disposition: classifyDisposition(element, info) }
  };
  chrome.runtime.sendMessage(report);
}

// `seen` gates a report only while the element stays visible: once it goes
// hidden it is dropped from the set, so a later show (same DOM node, same
// attribute toggle) is treated as a fresh detection instead of being
// permanently blocked. This is what makes the `attributes: true` observer
// (added for hide/show toggles) actually useful across repeat show events.
function reportIfNewlyVisible(node, seen) {
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
      obstructionChecked = true;
      covered = !!hit && hit !== element && !(typeof element.contains === 'function' && element.contains(hit));
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
  const controls = findCloseControls(entry.element);
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
