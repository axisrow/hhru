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
  const safe = (el) => {
    const text = (el.innerText || '').trim();
    const cookie = /cookie|куки|файлов cookie/i.test(text);
    const notice = /резюме доставлено|уведомлен|notification|toast/i.test(text)
      || el.matches('[data-qa^="notification"]');
    if (!cookie && !notice) return;
    if (/captcha|подтверд|анкета|отклик|сохранить|status/i.test(text)
      && !cookie && !el.matches('[data-qa^="notification"]')) return;
    // Do not infer a dismiss action from button text.  These are the exact
    // controls observed in the authenticated live DOM; broad matches could
    // click an unrelated consent/delete/confirmation action in the subtree.
    const button = cookie
      ? el.querySelector('[data-qa="cookies-policy-informer-accept"]')
      : el.querySelector(
          '[data-qa="notification-close"] button[aria-label="Удалить"]'
        );
    if (!button) return;
    const qa = button.getAttribute('data-qa');
    const label = button.getAttribute('aria-label');
    window[key].push({text: text.slice(0, 500), html: el.outerHTML.slice(0, 12000),
      selector: qa ? `[data-qa="${qa}"]` : (label ? `[aria-label="${label}"]` : null)});
    button.click();
  };
  const scan = (root) => {
    if (root.nodeType !== 1) return;
    safe(root);
    root.querySelectorAll(
      'body > *, [role="dialog"], [role="alert"], [data-qa]'
    ).forEach(safe);
  };
  new MutationObserver(ms => ms.forEach(m => {
    // HH.ru often mounts the shell first and fills text/controls later.
    // Rescan the changed element's parent so incremental mutations are not
    // lost (text nodes themselves are not elements).
    const target = m.target.nodeType === 1 ? m.target : m.target.parentElement;
    const candidate = target && (target.closest(
      '[role="dialog"], [role="alert"], '
      '[data-qa="cookies-policy-informer"], [data-qa^="notification"]'
    ) || (target.matches(
      '[role="dialog"], [role="alert"], '
      '[data-qa="cookies-policy-informer"], [data-qa^="notification"]'
    ) ? target : null));
    if (candidate) scan(candidate);
    m.addedNodes.forEach(scan);
  })).observe(document.documentElement, {
    subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['data-qa', 'aria-label'],
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
        logger.info("Transient overlay captured: %s", json.dumps(item, ensure_ascii=False))
    return result or []
