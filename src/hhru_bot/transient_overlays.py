"""Fail-closed handling for transient HH.ru overlays.

The observer is installed before the page is used.  This is intentional:
reading the DOM after a toast has disappeared cannot discover its selectors.
Only dismiss controls on notifications and cookie banners are eligible; form,
CAPTCHA and confirmation dialogs are never touched.
"""

from __future__ import annotations

import json
import logging

from playwright.sync_api import Page

logger = logging.getLogger("hhru_bot.transient_overlays")

_SCRIPT = r"""
(() => {
  const key = '__hhruTransientOverlayEvidence';
  window[key] = window[key] || [];
  const cookieSelector = '[data-qa="cookies-policy-informer"]';
  const notificationSelector =
    '[data-qa^="notification"]:not([data-qa="notification-close"])';
  const candidateSelector = `${cookieSelector}, ${notificationSelector}`;
  const pending = new WeakMap();
  const unsafe = /captcha|подтверд|анкета|отклик|сохранить|status/i;
  const enclosing = (el) => el.closest(candidateSelector);
  const schedule = (overlay) => {
    if (!overlay || pending.has(overlay)) return;
    const state = {attempts: 0, deadline: Date.now() + 5000, timer: null, evidence: null};
    pending.set(overlay, state);
    const finish = () => {
      if (state.evidence) window[key].push(state.evidence);
      pending.delete(overlay);
    };
    const retry = () => {
      if (!overlay.isConnected) {
        finish();
        return;
      }
      if (Date.now() > state.deadline || state.attempts >= 12) {
        pending.delete(overlay);
        return;
      }
      const cookie = overlay.matches(cookieSelector);
      const text = (overlay.innerText || '').trim();
      if (unsafe.test(text)) {
        pending.delete(overlay);
        return;
      }
      const button = cookie
        ? overlay.querySelector('[data-qa="cookies-policy-informer-accept"]')
        : overlay.querySelector(
            '[data-qa="notification-close"] button[aria-label="Удалить"]'
          );
      if (!button) {
        if (state.evidence) {
          finish();
          return;
        }
        state.timer = setTimeout(retry, 100 + state.attempts * 100);
        return;
      }
      state.attempts += 1;
      const qa = button.getAttribute('data-qa');
      const label = button.getAttribute('aria-label');
      const evidence = {text: text.slice(0, 500), html: overlay.outerHTML.slice(0, 12000),
        selector: qa ? `[data-qa="${qa}"]` : (label ? `[aria-label="${label}"]` : null)};
      state.evidence = evidence;
      button.click();
      // SSR can expose a control before its React handler is hydrated. Only
      // report success after the exact overlay/control positively disappears.
      if (!overlay.isConnected || !overlay.querySelector(button.matches(
        '[data-qa="cookies-policy-informer-accept"]'
      ) ? '[data-qa="cookies-policy-informer-accept"]' :
        '[data-qa="notification-close"] button[aria-label="Удалить"]')) {
        finish();
        return;
      }
      state.timer = setTimeout(retry, 100 + state.attempts * 150);
    };
    retry();
  };
  const scan = (root, discoverDescendants = true) => {
    if (!root || root.nodeType !== 1) return;
    const candidate = enclosing(root) ||
      (root.matches(candidateSelector) ? root : null);
    if (candidate) schedule(candidate);
    if (discoverDescendants) root.querySelectorAll(candidateSelector).forEach(schedule);
  };
  new MutationObserver(ms => ms.forEach(m => {
    // Rescan only the changed node's enclosing candidate overlay. This keeps
    // incremental shell/text/control hydration covered without page scans.
    const target = m.target.nodeType === 1 ? m.target : m.target.parentElement;
    scan(target, false);
    m.addedNodes.forEach(node => scan(node));
  })).observe(document, {
    subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['data-qa', 'aria-label', 'role'],
  });
})();
"""


def install_transient_overlay_observer(page: Page) -> None:
    """Install the observer before the action which may create an overlay."""
    page.add_init_script(_SCRIPT)


def drain_transient_overlay_evidence(page: Page) -> list[dict[str, str]]:
    """Return and clear DOM captured at insertion time (diagnostic evidence)."""
    try:
        result = page.evaluate(
            """() => {
                const k = '__hhruTransientOverlayEvidence';
                const v = window[k] || [];
                window[k] = [];
                return v;
            }"""
        )
    except Exception:  # diagnostics must not change the business result
        return []
    for item in result or []:
        # В лог — только структура (text/role/qa/visible), не html: сырая
        # разметка в логах читается агентом как «поля» (#998-класс).
        compact = {k: v for k, v in item.items() if k != "html"}
        if item.get("html"):
            compact["html_bytes"] = len(str(item["html"]))
        logger.info("Transient overlay captured: %s", json.dumps(compact, ensure_ascii=False))
    return result or []
