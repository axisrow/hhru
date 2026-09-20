const MAX_REPORTS = 50;
const HH_RU_ORIGIN = /^https:\/\/(?:[^/]+\.)?hh\.ru\//;
let recentReports = [];
let rehydrated = chrome.storage.session?.get('recentReports').then((data) => {
  recentReports = data?.recentReports ?? [];
});

function isTrustedSender(sender) {
  return !!sender.tab?.url && HH_RU_ORIGIN.test(sender.tab.url);
}

// Commands relayed to the active hh.ru tab (from the popup, or from the
// WebSocket bridge below). Mirrors the ACTION_ALLOWLIST in content.js —
// content.js rejects anything else anyway (fail-closed), this set only
// stops commands from reaching the tab. A guard test asserts the two
// literals match.
const RELAY_ACTIONS = new Set(['list_overlays', 'dismiss_overlay', 'check_element', 'click_element', 'wait_element', 'get_page_state', 'fill_element']);

// --- Agent bridge over the loopback WebSocket (stage 2, #1160). The server
// side is the CLI `live-serve` command (issue #1159); this is the extension
// half of the transport. Envelope {v, id, action, payload} in, answer
// {id, status, result} out. Unknown protocol version, unknown action and a
// missing command id fail closed with explicit errors; no command executes
// without an open connection — this socket is the only entry point.
const PROTOCOL_VERSION = 1;
const LIVE_SERVE_URL = 'ws://127.0.0.1:8765';
const HEARTBEAT_INTERVAL_MS = 15000;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
// Scalar payload fields copied onto the content-script command; anything
// else in a payload is dropped — the bridge never forwards unlisted shapes.
// text/allowApply — примитивы сценария отклика S4 (#1162).
const COMMAND_FIELDS = ['id', 'selector', 'dataQa', 'label', 'state', 'timeoutMs', 'text', 'allowApply'];
const WAIT_FIELDS = ['selector', 'dataQa', 'label', 'state', 'timeoutMs'];

let bridgeSocket = null;
let bridgeReconnectDelay = RECONNECT_BASE_MS;
let bridgeReconnectTimer = null;
let bridgeHeartbeatTimer = null;

function sendBridgeMessage(message) {
  if (!bridgeSocket || bridgeSocket.readyState !== 1) return;
  try {
    bridgeSocket.send(JSON.stringify(message));
  } catch (_) {
    // onclose fires for a broken socket; the reconnect path handles it.
  }
}

function respondToEnvelope(id, status, result) {
  sendBridgeMessage({ id, status, result });
}

function shapeCommand(envelope) {
  const payload = envelope.payload && typeof envelope.payload === 'object' ? envelope.payload : {};
  const command = { action: envelope.action };
  COMMAND_FIELDS.forEach((field) => {
    if (payload[field] !== undefined) command[field] = payload[field];
  });
  if (payload.waitFor && typeof payload.waitFor === 'object') {
    const waitFor = {};
    WAIT_FIELDS.forEach((field) => {
      if (payload.waitFor[field] !== undefined) waitFor[field] = payload.waitFor[field];
    });
    command.waitFor = waitFor;
  }
  return command;
}

// Command ids mirror the server protocol (protocol._is_valid_id, #1159):
// a non-empty string or an integer — booleans and floats are not ids.
// Rejecting integers here made live-serve burn the full response timeout:
// the error's null id never matched the pending command (#1178 review).
function envelopeId(value) {
  if (typeof value === 'string' && value.length > 0) return value;
  if (typeof value === 'number' && Number.isInteger(value)) return value;
  return null;
}

function handleEnvelope(envelope) {
  const id = envelope ? envelopeId(envelope.id) : null;
  if (!envelope || envelope.v !== PROTOCOL_VERSION) {
    respondToEnvelope(id, 'error', { code: 'unsupported_version', receivedV: envelope ? envelope.v ?? null : null });
    return;
  }
  if (id === null) {
    respondToEnvelope(null, 'error', { code: 'command_id_required' });
    return;
  }
  if (!RELAY_ACTIONS.has(envelope.action)) {
    respondToEnvelope(id, 'error', { code: 'action_not_allowed', action: envelope.action ?? null });
    return;
  }
  relayToTab(shapeCommand(envelope), (response) => {
    if (!response) {
      respondToEnvelope(id, 'error', { code: 'no_response' });
      return;
    }
    const result = { ...response };
    delete result.ok;
    respondToEnvelope(id, response.ok ? 'ok' : 'error', result);
  });
}

// Shared tab relay: the popup path (#931) and the WebSocket bridge both land
// here. `command` is already shaped for content.js; content responses pass
// through untouched (the bridge maps ok -> status itself).
//
// Target = ANY hh.ru tab (active one first), not the active tab of the last
// focused window: the agent's runs survive the user working in other
// windows — active-tab-only made every primitive poll a foreign DOM
// (battle run #1162, 2026-09-20). URL-scoped query needs only the
// host_permissions the manifest already declares.
function relayToTab(command, sendResponse) {
  chrome.tabs.query({ url: ['https://hh.ru/*', 'https://*.hh.ru/*'] }, (tabs) => {
    const tab = tabs.find((t) => t.active) ?? tabs[0];
    if (!tab || !isTrustedSender({ tab })) {
      sendResponse({ ok: false, error: 'no_hhru_tab' });
      return;
    }
    chrome.tabs.sendMessage(tab.id, command, (response) => {
      if (chrome.runtime.lastError) {
        // Two MV3 failure modes must not share one code (battle flake #1181):
        // "Receiving end does not exist" = the command never reached the
        // content script (nothing happened — safe to report as refused);
        // "message port closed before a response was received" = the content
        // script GOT the command but the answer was lost (the click may have
        // happened, e.g. the page navigated mid-wait). Collapsing the second
        // into the first lets the agent re-click an already-executed action.
        const message = chrome.runtime.lastError.message || '';
        const lost = /message port closed/i.test(message);
        sendResponse({ ok: false, error: lost ? 'response_lost' : 'content_script_unreachable' });
        return;
      }
      sendResponse(response ?? { ok: false, error: 'no_response' });
    });
  });
}

function stopBridgeHeartbeat() {
  if (bridgeHeartbeatTimer) {
    clearInterval(bridgeHeartbeatTimer);
    bridgeHeartbeatTimer = null;
  }
}

// Handshake diagnostics (#1163): announce protocol version, allowlist and
// manifest permissions so `live-doctor` can verify both sides agree without
// sending a single command. kind-namespaced, like the heartbeat — the server
// treats these as diagnostics, never as command responses.
function announceHello() {
  const manifest = chrome.runtime.getManifest();
  sendBridgeMessage({
    kind: 'hello',
    v: PROTOCOL_VERSION,
    actions: [...RELAY_ACTIONS].sort(),
    permissions: manifest.permissions ?? [],
    hostPermissions: manifest.host_permissions ?? [],
  });
}

// A dropped connection is never a scenario error (#1159): reconnect with
// capped backoff until the CLI server comes back. The in-memory backoff
// timer lives only as long as this SW does — the piece that keeps the worker
// (and with it this chain) alive while an hh.ru tab is open is the
// content-script keep-alive ping (#1187, #1197), not anything scheduled here.
function scheduleBridgeReconnect() {
  if (bridgeReconnectTimer) return;
  bridgeReconnectTimer = setTimeout(() => {
    bridgeReconnectTimer = null;
    connectLiveServe();
  }, bridgeReconnectDelay);
  bridgeReconnectDelay = Math.min(bridgeReconnectDelay * 2, RECONNECT_MAX_MS);
}

// MV3 SW-idle self-wake (#1181): the service worker dies ~30 s after its last
// event, and the setTimeout reconnect chain dies with it — a sleeping SW
// never retries, so the channel only came up if the server was started
// BEFORE the hh.ru tab woke the SW. A periodic alarm re-fires connectLiveServe
// from a cold start, so the order stops mattering (the CLI waits up to 120 s
// for the client). Foreground invariant (#1159) intact: this creates no
// daemon and no server — the SW wakes, tries one loopback connection and
// sleeps again; the WS server still lives only inside the CLI process.
const RECONNECT_ALARM = 'hhru-live-reconnect';
// MV3 minimum alarm period is 30 s (Chrome >= 120).
const RECONNECT_ALARM_PERIOD_MIN = 0.5;

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== RECONNECT_ALARM) return;
  if (!bridgeSocket) connectLiveServe();
});
chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: RECONNECT_ALARM_PERIOD_MIN });

// Keep-alive pings double as retry opportunities (Codex review of #1203):
// while the socket is down, ping time connects NOW instead of leaving the
// next attempt up to the 30s backoff cap — live-doctor keeps its server
// open for only 10s, so a capped backoff could miss it entirely. Cancels a
// pending backoff timer and restarts from the base delay; a no-op while
// connected or already connecting (connectLiveServe guards the socket).
// Complements the alarm above: the alarm wakes a suspended SW with no tabs,
// the ping keeps the SW and expedites the connect while a tab is open.
function expediteBridgeConnect() {
  if (bridgeSocket) return;
  if (bridgeReconnectTimer) {
    clearTimeout(bridgeReconnectTimer);
    bridgeReconnectTimer = null;
  }
  bridgeReconnectDelay = RECONNECT_BASE_MS;
  connectLiveServe();
}

function connectLiveServe() {
  if (bridgeSocket) return;
  let socket;
  try {
    socket = new WebSocket(LIVE_SERVE_URL);
  } catch (_) {
    scheduleBridgeReconnect();
    return;
  }
  bridgeSocket = socket;
  socket.onopen = () => {
    bridgeReconnectDelay = RECONNECT_BASE_MS;
    stopBridgeHeartbeat();
    announceHello();
    bridgeHeartbeatTimer = setInterval(() => {
      sendBridgeMessage({ kind: 'heartbeat', observedAt: new Date().toISOString() });
    }, HEARTBEAT_INTERVAL_MS);
  };
  socket.onmessage = (event) => {
    let message = null;
    try {
      message = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (!message || typeof message !== 'object') return;
    // kind-namespaced frames are diagnostics, not command envelopes.
    if (message.kind === 'heartbeat_ack') return;
    if (typeof message.kind === 'string') return;
    handleEnvelope(message);
  };
  socket.onclose = () => {
    if (bridgeSocket !== socket) return;
    bridgeSocket = null;
    stopBridgeHeartbeat();
    scheduleBridgeReconnect();
  };
  socket.onerror = () => { /* onclose always follows; nothing to add */ };
}

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
    relayToTab({
      action: message.action,
      id: message.id ?? null,
      selector: message.selector ?? null
    }, sendResponse);
    return true;
  }
  if (!isTrustedSender(sender)) {
    sendResponse({ ok: false, error: 'sender_not_allowed' });
    return false;
  }
  // Keep-alive ping from the hh.ru tab (#1187, #1197): a content-script
  // message wakes a suspended MV3 SW (module eval re-runs connectLiveServe)
  // and resets its ~30s idle timer, so the 20s ping keeps the worker — and
  // with it the WebSocket reconnect chain — alive while any hh.ru tab is
  // open. That is what makes "server started after Chrome" recoverable.
  // With the socket down a ping is also a free retry slot (Codex review of
  // #1203): attempt the connection NOW, see expediteBridgeConnect.
  if (message?.kind === 'keepalive') {
    expediteBridgeConnect();
    sendResponse({ ok: true });
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
    // The hhru-agent port remains reserved: no extension code opens it, and
    // the agent bridge travels over the loopback WebSocket above instead.
    port.postMessage({ ok: false, error: 'action_not_allowed', action: message?.action ?? null });
  });
});
// Browser startup must wake this MV3 service worker and connect the channel:
// without an onStartup listener the SW only starts on install or on a content
// script message, so `live-doctor`/`bump-live` running before an hh.ru page
// loads (or with the SW asleep) never see the extension (#1163).
chrome.runtime.onStartup.addListener(() => connectLiveServe());
connectLiveServe();
