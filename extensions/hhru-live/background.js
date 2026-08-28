const MAX_REPORTS = 50;
const HH_RU_ORIGIN = /^https:\/\/(?:[^/]+\.)?hh\.ru\//;
let recentReports = [];
let rehydrated = chrome.storage.session?.get('recentReports').then((data) => {
  recentReports = data?.recentReports ?? [];
});

function isTrustedSender(sender) {
  return !!sender.tab?.url && HH_RU_ORIGIN.test(sender.tab.url);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
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
    // Deliberately empty allowlist in stage one: transport and detection only.
    port.postMessage({ ok: false, error: 'action_not_allowed', action: message?.action ?? null });
  });
});
