// Executes extensions/hhru-live/background.js inside a vm context and runs
// ONE named relay scenario (argv[2]) from the registry below, printing a
// JSON verdict on stdout. Issue #931: the popup -> relay -> hh.ru-tab path
// was previously covered only by grep guards — this runner exercises the
// real listener with a chrome.tabs/storage stub, the same way
// run_command_scenario.js exercises content.js.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const backgroundJsPath = path.join(__dirname, '..', '..', 'extensions', 'hhru-live', 'background.js');
const source = fs.readFileSync(backgroundJsPath, 'utf8');

// chrome stub with tabs + storage.session (unlike dom_stub.makeChrome, which
// only models what content.js needs). lastError is modelled the way MV3
// actually delivers it: set synchronously before the sendMessage callback.
function makeEnv({ activeTab, tabReply, tabError }) {
  const sent = { toTab: [], toStorage: null };
  const chrome = {
    runtime: {
      id: 'hhru-live-test-extension',
      lastError: null,
      sendMessage: () => {},
      onMessage: { addListener: (fn) => chrome.runtime._listeners.push(fn) },
      onConnect: { addListener: () => {} },
      _listeners: [],
    },
    tabs: {
      query: (_opts, cb) => cb(activeTab ? [activeTab] : []),
      sendMessage: (_tabId, message, cb) => {
        sent.toTab.push({ tabId: _tabId, message });
        if (tabError) {
          chrome.runtime.lastError = { message: tabError };
        }
        cb(tabReply ?? null);
        chrome.runtime.lastError = null;
      },
    },
    storage: {
      session: {
        get: (key) => Promise.resolve(sent.storageGet ?? {}),
        set: (data) => {
          sent.toStorage = data;
          return Promise.resolve();
        },
      },
    },
  };
  return { chrome, sent };
}

function deliver(chrome, message, sender) {
  return new Promise((resolve) => {
    chrome.runtime._listeners.forEach((fn) => fn(message, sender, resolve));
  });
}

const OWN = 'hhru-live-test-extension';
const hhruSender = { id: OWN, tab: { id: 7, url: 'https://hh.ru/applicant/profile/me' } };

const SCENARIOS = {
  // The happy relay path: an allowlisted command from the popup travels to
  // the active hh.ru tab and the content script's response comes back
  // untouched.
  relay_forwards_to_hhru_tab: async () => {
    const overlays = [{ id: 'overlay-1', type: 'modal', disposition: 'safe', closeControls: 1 }];
    const { chrome, sent } = makeEnv({
      activeTab: { id: 7, url: 'https://hh.ru/applicant/profile/me' },
      tabReply: { ok: true, overlays },
    });
    vm.runInContext(source, vm.createContext({ chrome }));
    const response = await deliver(chrome, { kind: 'agent_command', action: 'list_overlays' }, { id: OWN });
    return {
      response,
      sentToTabCount: sent.toTab.length,
      sentAction: sent.toTab[0]?.message.action ?? null,
      sentTabId: sent.toTab[0]?.tabId ?? null,
      tabReplyPreserved: response?.overlays === overlays || response?.overlays?.[0]?.id === 'overlay-1',
    };
  },

  // No hh.ru tab focused (or the active tab is a foreign origin): the relay
  // must refuse WITHOUT touching any tab.
  relay_no_hhru_tab: async () => {
    const { chrome, sent } = makeEnv({
      activeTab: { id: 3, url: 'https://example.com/page' },
    });
    vm.runInContext(source, vm.createContext({ chrome }));
    const response = await deliver(chrome, { kind: 'agent_command', action: 'list_overlays' }, { id: OWN });
    return {
      error: response?.error ?? null,
      sentToTabCount: sent.toTab.length,
    };
  },

  // The tab is hh.ru but the content script is not there (freshly navigated,
  // extension just reloaded): MV3 reports it via runtime.lastError.
  relay_content_script_unreachable: async () => {
    const { chrome, sent } = makeEnv({
      activeTab: { id: 7, url: 'https://hh.ru/' },
      tabError: 'Could not establish connection. Receiving end does not exist.',
    });
    vm.runInContext(source, vm.createContext({ chrome }));
    const response = await deliver(chrome, { kind: 'agent_command', action: 'check_element', selector: '[data-qa="x"]' }, { id: OWN });
    return {
      error: response?.error ?? null,
      sentToTabCount: sent.toTab.length,
    };
  },

  // A command from another extension (sender.id mismatch) is rejected before
  // any tab lookup happens.
  relay_rejects_foreign_sender: async () => {
    const { chrome, sent } = makeEnv({
      activeTab: { id: 7, url: 'https://hh.ru/' },
      tabReply: { ok: true },
    });
    vm.runInContext(source, vm.createContext({ chrome }));
    const response = await deliver(chrome, { kind: 'agent_command', action: 'list_overlays' }, { id: 'some-other-extension' });
    return {
      error: response?.error ?? null,
      sentToTabCount: sent.toTab.length,
    };
  },

  // An action outside the relay allowlist never reaches the tab (content.js
  // would reject it anyway — fail-closed twice).
  relay_rejects_unknown_action: async () => {
    const { chrome, sent } = makeEnv({
      activeTab: { id: 7, url: 'https://hh.ru/' },
      tabReply: { ok: true },
    });
    vm.runInContext(source, vm.createContext({ chrome }));
    const response = await deliver(chrome, { kind: 'agent_command', action: 'close_all_windows' }, { id: OWN });
    return {
      error: response?.error ?? null,
      sentToTabCount: sent.toTab.length,
    };
  },

  // Diagnostics from a real hh.ru tab (connected / overlay_detected) still
  // land in storage.session — the relay must not have broken the
  // detection-storage path (#644) when the command path was added (#930).
  diagnostics_stored: async () => {
    const { chrome, sent } = makeEnv({ activeTab: null });
    vm.runInContext(source, vm.createContext({ chrome }));
    const response = await deliver(chrome, {
      kind: 'overlay_detected',
      observedAt: '2026-09-05T00:00:00Z',
      overlay: { id: 'overlay-1', type: 'toast', disposition: 'safe' },
    }, hhruSender);
    return {
      responseOk: response?.ok ?? null,
      storedReports: sent.toStorage?.recentReports?.length ?? 0,
      storedKind: sent.toStorage?.recentReports?.[0]?.kind ?? null,
    };
  },

  // A diagnostics message from a NON-hh.ru origin is rejected even though it
  // claims to be an overlay report (isTrustedSender gate, #743 pattern).
  diagnostics_foreign_origin_rejected: async () => {
    const { chrome, sent } = makeEnv({ activeTab: null });
    vm.runInContext(source, vm.createContext({ chrome }));
    const response = await deliver(chrome, { kind: 'overlay_detected', overlay: {} }, {
      id: OWN,
      tab: { id: 3, url: 'https://example.com/' },
    });
    return {
      error: response?.error ?? null,
      storedReports: sent.toStorage?.recentReports?.length ?? 0,
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
}

main().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
