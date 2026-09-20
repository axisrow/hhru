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

function handleEnvelope(envelope) {
  const id = envelope && typeof envelope.id === 'string' ? envelope.id : null;
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
function relayToTab(command, sendResponse) {
  chrome.tabs.query({ active: true, lastFocusedWindow: true }, ([tab]) => {
    if (!tab || !isTrustedSender({ tab })) {
      sendResponse({ ok: false, error: 'no_hhru_tab' });
      return;
    }
    chrome.tabs.sendMessage(tab.id, command, (response) => {
      if (chrome.runtime.lastError) {
        sendResponse({ ok: false, error: 'content_script_unreachable' });
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
// capped backoff until the CLI server comes back.
function scheduleBridgeReconnect() {
  if (bridgeReconnectTimer) return;
  bridgeReconnectTimer = setTimeout(() => {
    bridgeReconnectTimer = null;
    connectLiveServe();
  }, bridgeReconnectDelay);
  bridgeReconnectDelay = Math.min(bridgeReconnectDelay * 2, RECONNECT_MAX_MS);
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
