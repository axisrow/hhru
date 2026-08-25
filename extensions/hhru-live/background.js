const recentReports = [];
const MAX_REPORTS = 50;
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.kind === 'connected' || message?.kind === 'overlay_detected') {
    recentReports.unshift({ ...message, tabId: sender.tab?.id ?? null });
    recentReports.splice(MAX_REPORTS);
    chrome.storage.session?.set({ recentReports });
    sendResponse({ ok: true });
  } else {
    sendResponse({ ok: false, error: 'message_not_allowed' });
  }
  return false;
});
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'hhru-agent') { port.disconnect(); return; }
  port.onMessage.addListener((message) => {
    // Deliberately empty allowlist in stage one: transport and detection only.
    port.postMessage({ ok: false, error: 'action_not_allowed', action: message?.action ?? null });
  });
});
