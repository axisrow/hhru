const MAX_REPORTS = 50;
const HH_RU_ORIGIN = /^https:\/\/(?:[^/]+\.)?hh\.ru\//;
let recentReports = [];
let rehydrated = chrome.storage.session?.get('recentReports').then((data) => {
  recentReports = data?.recentReports ?? [];
});

function isTrustedSender(sender) {
  return !!sender.tab?.url && HH_RU_ORIGIN.test(sender.tab.url);
}

// Commands relayed from the popup to the active hh.ru tab. Mirrors the
// ACTION_ALLOWLIST in content.js — content.js rejects anything else anyway
// (fail-closed), this set only stops commands from reaching the tab.
const RELAY_ACTIONS = new Set(['list_overlays', 'dismiss_overlay', 'check_element']);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Commands come from this extension's own popup (no sender.tab), so they
  // are handled BEFORE the hh.ru-tab gate below.
  if (message?.kind === 'agent_command') {
    if (sender.id !== chrome.runtime.id) {
      sendResponse({ ok: false, error: 'sender_not_allowed' });
      return false;
    }
    if (!RELAY_ACTIONS.has(message.action)) {
      sendResponse({ ok: false, error: 'action_not_allowed', action: message.action ?? null });
      return false;
    }
    chrome.tabs.query({ active: true, lastFocusedWindow: true }, ([tab]) => {
      if (!tab || !isTrustedSender({ tab })) {
        sendResponse({ ok: false, error: 'no_hhru_tab' });
        return;
      }
      chrome.tabs.sendMessage(tab.id, {
        action: message.action,
        id: message.id ?? null,
        selector: message.selector ?? null
      }, (response) => {
        if (chrome.runtime.lastError) {
          sendResponse({ ok: false, error: 'content_script_unreachable' });
          return;
        }
        sendResponse(response ?? { ok: false, error: 'no_response' });
      });
    });
    return true;
  }
  if (!isTrustedSender(sender)) {
    sendResponse({ ok: false, error: 'sender_not_allowed' });
    return false;
  }
  if (message?.kind === 'connected' || message?.kind === 'overlay_detected') {
    (async () => {
      await rehydrated;
      recentReports.unshift({ ...message, tabId: sender.tab?.id ?? null });
      recentReports.splice(MAX_REPORTS);
      try {
        await chrome.storage.session?.set({ recentReports });
        sendResponse({ ok: true });
      } catch (error) {
        sendResponse({ ok: false, error: 'storage_write_failed' });
      }
    })();
    return true;
  }
  sendResponse({ ok: false, error: 'message_not_allowed' });
  return false;
});
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'hhru-agent' || !isTrustedSender(port.sender ?? {})) { port.disconnect(); return; }
  port.onMessage.addListener((message) => {
    // The hhru-agent port remains reserved for the future external bridge
    // (stage two): commands travel through the popup -> relay path above.
    port.postMessage({ ok: false, error: 'action_not_allowed', action: message?.action ?? null });
  });
});
