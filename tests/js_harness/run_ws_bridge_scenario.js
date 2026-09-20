// Executes extensions/hhru-live/background.js inside a vm context with a
// WebSocket stub and runs ONE named bridge scenario (argv[2]) from the
// registry below, printing a JSON verdict on stdout. Companion to
// run_background_scenario.js: that one covers the popup relay, this one
// covers the stage-2 loopback WebSocket bridge (#1160, protocol #1159) —
// envelope in, {id, status, result} out, fail-closed on version/action,
// reconnect after a dropped connection.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const backgroundJsPath = path.join(__dirname, '..', '..', 'extensions', 'hhru-live', 'background.js');
const source = fs.readFileSync(backgroundJsPath, 'utf8');

function makeEnv({ activeTab, tabReply, tabError }) {
  const sent = { toTab: [], frames: [] };
  const sockets = [];
  // Minimal WebSocket surface the bridge uses: constructor, readyState,
  // send, and the onopen/onmessage/onclose/onerror handler slots.
  class WebSocketStub {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      sockets.push(this);
    }
    send(data) {
      sent.frames.push(data);
    }
    close() { /* not used by the bridge */ }
    _open() {
      this.readyState = 1;
      if (this.onopen) this.onopen();
    }
    _message(data) {
      if (this.onmessage) this.onmessage({ data });
    }
    _drop() {
      this.readyState = 3;
      if (this.onclose) this.onclose();
    }
  }
  const chrome = {
    runtime: {
      id: 'hhru-live-test-extension',
      lastError: null,
      sendMessage: () => {},
      onMessage: { addListener: (fn) => chrome.runtime._listeners.push(fn) },
      onConnect: { addListener: () => {} },
      onStartup: {
        _listeners: [],
        addListener: (fn) => chrome.runtime.onStartup._listeners.push(fn),
      },
      getManifest: () => ({
        permissions: ['storage', 'alarms'],
        host_permissions: ['https://hh.ru/*', 'https://*.hh.ru/*'],
      }),
      _listeners: [],
    },
    tabs: {
      query: (_opts, cb) => cb(activeTab ? [activeTab] : []),
      sendMessage: (_tabId, message, cb) => {
        sent.toTab.push(message);
        if (tabError) {
          chrome.runtime.lastError = { message: tabError };
        }
        cb(tabReply ?? null);
        chrome.runtime.lastError = null;
      },
    },
    // #1181: background.js создаёт reconnect-alarm на старте SW; стаб фиксирует
    // создание, чтобы манифест-обещание было подкреплено рантаймом.
    alarms: {
      create: (name, opts) => {
        sent.alarmCreated = { name, ...opts };
      },
      onAlarm: { addListener: () => {} },
    },
    storage: {
      session: {
        get: (key) => Promise.resolve({}),
        set: (data) => Promise.resolve(),
      },
    },
  };
  return { chrome, sent, sockets, WebSocketStub };
}

// The bridge schedules reconnects in real seconds; the harness scales every
// delay down so scenarios stay inside the suite's time budget.
function makeContext(env) {
  return vm.createContext({
    chrome: env.chrome,
    WebSocket: env.WebSocketStub,
    setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms || 0, 20)),
    clearTimeout,
    setInterval: (fn, ms) => setInterval(fn, Math.min(ms || 0, 500)),
    clearInterval,
    console,
  });
}

function runBridge(env) {
  vm.runInContext(source, makeContext(env), { filename: 'background.js' });
}

// The source connects on load; the scenario completes the handshake, then
// feeds the server frame to the open socket.
function handshake(sockets) {
  sockets[0]._open();
}

function sentFrames(sent) {
  return sent.frames.map((frame) => JSON.parse(frame));
}

// kind-namespaced frames (hello #1163, heartbeat) are diagnostics, not
// command envelopes; scenarios below assert on envelopes only and read the
// hello explicitly where it matters.
function sentEnvelopes(sent) {
  return sentFrames(sent).filter((frame) => typeof frame.kind !== 'string');
}

const ACTIVE_HHRU_TAB = { id: 7, url: 'https://hh.ru/applicant/me' };

const SCENARIOS = {
  // Happy path: an envelope is shaped, relayed to the hh.ru tab, and the
  // content response maps to {id, status, result} on the socket.
  envelope_relayed: async () => {
    const env = makeEnv({
      activeTab: ACTIVE_HHRU_TAB,
      tabReply: { ok: true, element: { found: true, visible: true } },
    });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._message(JSON.stringify({
      v: 1, id: 'c1', action: 'check_element', payload: { selector: '[data-qa="x"]' },
    }));
    return {
      envelopes: sentEnvelopes(env.sent),
      sentToTabCount: env.sent.toTab.length,
      sentAction: env.sent.toTab[0]?.action ?? null,
      sentSelector: env.sent.toTab[0]?.selector ?? null,
    };
  },

  // Fail-closed (#1159 п.4): an unknown protocol version is an explicit
  // error and never reaches the tab.
  unsupported_version_rejected: async () => {
    const env = makeEnv({ activeTab: ACTIVE_HHRU_TAB, tabReply: { ok: true } });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._message(JSON.stringify({ v: 99, id: 'c2', action: 'list_overlays' }));
    return {
      envelopes: sentEnvelopes(env.sent),
      sentToTabCount: env.sent.toTab.length,
    };
  },

  // Fail-closed: an action outside the allowlist is refused by the bridge
  // before any tab lookup (content.js would reject it again — twice).
  unknown_action_rejected: async () => {
    const env = makeEnv({ activeTab: ACTIVE_HHRU_TAB, tabReply: { ok: true } });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._message(JSON.stringify({ v: 1, id: 'c3', action: 'close_all_windows' }));
    return {
      envelopes: sentEnvelopes(env.sent),
      sentToTabCount: env.sent.toTab.length,
    };
  },

  // Only whitelisted scalar payload fields reach the content command;
  // anything else in a payload is dropped.
  payload_fields_whitelisted: async () => {
    const env = makeEnv({
      activeTab: ACTIVE_HHRU_TAB,
      tabReply: { ok: true, page: { url: 'https://hh.ru/' } },
    });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._message(JSON.stringify({
      v: 1,
      id: 'c4',
      action: 'click_element',
      payload: { dataQa: 'x', evil: { inject: true }, waitFor: { state: 'hidden', timeoutMs: 500, evil: 1 } },
    }));
    return {
      command: env.sent.toTab[0] ?? null,
      sentToTabCount: env.sent.toTab.length,
    };
  },

  // A dropped connection is not a scenario error: the bridge schedules a
  // reconnect (scaled timer fires within the scenario) and opens a second
  // socket, which then serves commands again.
  reconnect_after_drop: async () => {
    const env = makeEnv({
      activeTab: ACTIVE_HHRU_TAB,
      tabReply: { ok: true, overlays: [] },
    });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._drop();
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (!env.sockets[1]) return { secondSocket: false, frames: [] };
    env.sockets[1]._open();
    env.sockets[1]._message(JSON.stringify({ v: 1, id: 'c5', action: 'get_page_state' }));
    return {
      secondSocket: true,
      firstReadyState: env.sockets[0].readyState,
      envelopes: sentEnvelopes(env.sent),
      sentToTabCount: env.sent.toTab.length,
    };
  },

  // No active hh.ru tab: the bridge answers the server with the relay's
  // own error mapped into the envelope shape.
  no_hhru_tab_ws: async () => {
    const env = makeEnv({
      activeTab: { id: 3, url: 'https://example.com/page' },
      tabReply: { ok: true },
    });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._message(JSON.stringify({ v: 1, id: 'c6', action: 'list_overlays' }));
    return {
      envelopes: sentEnvelopes(env.sent),
      sentToTabCount: env.sent.toTab.length,
    };
  },

  // Handshake diagnostics (#1163): the bridge announces its protocol
  // version, allowlist and manifest permissions as the FIRST frame on open,
  // so live-doctor can verify both sides without sending any command.
  hello_announced_on_open: async () => {
    const env = makeEnv({ activeTab: null });
    runBridge(env);
    handshake(env.sockets);
    const all = sentFrames(env.sent);
    return {
      hello: all.find((frame) => frame.kind === 'hello') ?? null,
      helloIsFirstFrame: all[0]?.kind === 'hello',
    };
  },

  // Browser startup must wake the SW and connect (#1163): firing the
  // onStartup listener with no open socket opens a new one immediately,
  // instead of waiting for the slow reconnect backoff.
  startup_reconnect: async () => {
    const env = makeEnv({ activeTab: null });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._drop();
    const listener = env.chrome.runtime.onStartup._listeners[0];
    const socketsBefore = env.sockets.length;
    if (typeof listener === 'function') listener();
    return {
      listenerRegistered: typeof listener === 'function',
      socketsBefore,
      newSocketCreated: env.sockets.length > socketsBefore,
    };
  },

  // Server protocol allows integer command ids (protocol._is_valid_id):
  // the answer must echo the same numeric id, not command_id_required.
  integer_id_echoed: async () => {
    const env = makeEnv({
      activeTab: ACTIVE_HHRU_TAB,
      tabReply: { ok: true, overlays: [] },
    });
    runBridge(env);
    handshake(env.sockets);
    env.sockets[0]._message(JSON.stringify({ v: 1, id: 7, action: 'list_overlays' }));
    return {
      envelopes: sentEnvelopes(env.sent),
      sentToTabCount: env.sent.toTab.length,
    };
  },

  // Heartbeats flow only while the socket is open (kind-namespaced, so the
  // server can distinguish a live client from a dead one, #1159 п.3).
  heartbeat_only_when_open: async () => {
    const env = makeEnv({ activeTab: null });
    runBridge(env);
    await new Promise((resolve) => setTimeout(resolve, 30));
    const framesBeforeOpen = sentFrames(env.sent).length;
    handshake(env.sockets);
    await new Promise((resolve) => setTimeout(resolve, 600));
    const heartbeatFrames = sentFrames(env.sent).filter((f) => f.kind === 'heartbeat');
    return {
      framesBeforeOpen,
      heartbeatCount: heartbeatFrames.length,
    };
  },

  // #1187/#1197: the content-script keep-alive ping is what keeps the MV3 SW
  // (and its setTimeout reconnect chain) alive while an hh.ru tab is open —
  // each content-script message wakes a suspended worker and resets its idle
  // timer. The ping must be answered from behind the trusted-sender gate.
  keepalive_answered: async () => {
    const env = makeEnv({ activeTab: null });
    runBridge(env);
    handshake(env.sockets);
    const listener = env.chrome.runtime._listeners[0];
    const response = await new Promise((resolve) => {
      listener({ kind: 'keepalive' }, { tab: { id: 7, url: 'https://hh.ru/applicant/me' } }, resolve);
    });
    const foreign = await new Promise((resolve) => {
      listener({ kind: 'keepalive' }, { tab: { id: 9, url: 'https://example.com/page' } }, resolve);
    });
    return { response, foreign };
  },

  // Codex review of #1203 (P1): with the socket down, a keepalive ping must
  // EXPEDITE a connection attempt instead of only answering — otherwise the
  // next retry stays up to the 30s backoff cap away, while live-doctor keeps
  // its server open for just 10s, so a late-started server could still be
  // missed. A foreign ping touches no bridge state.
  keepalive_expedites_reconnect: async () => {
    const env = makeEnv({ activeTab: null });
    runBridge(env);
    env.sockets[0]._drop();
    const listener = env.chrome.runtime._listeners[0];
    const before = env.sockets.length;
    await new Promise((resolve) => {
      listener({ kind: 'keepalive' }, { tab: { id: 9, url: 'https://example.com/' } }, resolve);
    });
    const afterForeign = env.sockets.length;
    await new Promise((resolve) => {
      listener({ kind: 'keepalive' }, { tab: { id: 7, url: 'https://hh.ru/' } }, resolve);
    });
    const expedited = env.sockets.length > afterForeign && afterForeign === before;
    // The expedited attempt is a working socket: opening it connects, and a
    // further ping is answered while connected (and opens no extra socket).
    env.sockets[before]._open();
    const socketsWhileConnected = env.sockets.length;
    const connectedResponse = await new Promise((resolve) => {
      listener({ kind: 'keepalive' }, { tab: { id: 7, url: 'https://hh.ru/' } }, resolve);
    });
    return {
      expedited,
      connectedResponse,
      noExtraSocketWhileConnected: env.sockets.length === socketsWhileConnected,
    };
  },
};

async function main() {
  const name = process.argv[2];
  const scenario = SCENARIOS[name];
  if (!scenario) {
    process.stderr.write(`unknown scenario: ${name}; available: ${Object.keys(SCENARIOS).join(', ')}\n`);
    process.exit(1);
  }
  const result = await scenario();
  process.stdout.write(JSON.stringify({ scenario: name, ...result }));
  // The bridge legitimately keeps its heartbeat interval alive while the
  // socket is open; the harness is done here, so exit explicitly instead of
  // waiting on the interval.
  process.exit(0);
}

main().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
