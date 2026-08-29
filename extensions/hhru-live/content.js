// Detection only. Policy and popup closing belong to the follow-up overlay worker.
const ACTION_ALLOWLIST = new Set();
const OVERLAY_SELECTORS = [
  '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]',
  '[class*="modal"]', '[class*="popup"]', '[class*="toast"]',
  '[class*="notification"]', '[class*="cookie"]'
];

function classify(element) {
  const text = (element.innerText || element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 500);
  const role = element.getAttribute('role');
  const className = element.getAttribute('class') ?? '';
  const type = /cookie/i.test(className + text)
    ? 'cookie_banner' : role === 'dialog' || role === 'alertdialog' || /modal|popup/i.test(className)
    ? 'modal' : /toast|notification/i.test(className) ? 'notification' : 'overlay';
  return { type, text, role, className: className.slice(0, 200), visible: !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length) };
}

function report(element) {
  const report = { kind: 'overlay_detected', observedAt: new Date().toISOString(), overlay: classify(element) };
  chrome.runtime.sendMessage(report);
}

function isVisible(element) {
  return !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
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
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !ACTION_ALLOWLIST.has(message.action)) {
    sendResponse({ ok: false, error: 'action_not_allowed' });
    return false;
  }
  return false;
});
chrome.runtime.sendMessage({ kind: 'connected', url: location.href, observedAt: new Date().toISOString() });
