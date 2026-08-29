// Executes extensions/hhru-live/content.js inside a vm context wired to
// tests/js_harness/dom_stub.js, runs the hide->show->re-detect scenario for
// issue #743 finding 1, and prints the sequence of reported overlay `kind`s
// as JSON so tests/test_hhru_live_behavior.py can assert on real execution
// instead of grepping source text.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createEnvironment } = require('./dom_stub');

const contentJsPath = path.join(__dirname, '..', '..', 'extensions', 'hhru-live', 'content.js');
const source = fs.readFileSync(contentJsPath, 'utf8');

const env = createEnvironment();
const context = vm.createContext({
  document: env.document,
  chrome: env.chrome,
  MutationObserver: env.MutationObserver,
  Element: env.Element,
  location: env.location,
  console,
});

// Build a cookie-consent banner already present in the DOM before content.js
// runs, matching the OVERLAY_SELECTORS `[class*="cookie"]` rule, and started
// hidden — content.js's initial scan (document.querySelectorAll(...)) must
// not report it while hidden.
const banner = env.document.createElement('div');
banner.setAttribute('class', 'cookie-banner');
banner._setVisible(false);
env.document.documentElement.appendChild(banner);
env.flush();

vm.runInContext(source, context);

async function run() {
  await Promise.resolve(); // flush the initial-scan microtask, if any queued

  // Step 1: show the banner (attribute toggle a real site would use to
  // reveal a pre-mounted cookie banner) -> should report once.
  banner.classList.add('cookie-banner--visible');
  banner._setVisible(true);
  banner.setAttribute('class', banner.getAttribute('class')); // re-trigger attributes record
  env.flush();
  await Promise.resolve();

  // Step 2: hide it again (dismissed) -> no report expected for a hide.
  banner._setVisible(false);
  banner.setAttribute('class', 'cookie-banner');
  env.flush();
  await Promise.resolve();

  // Step 3: show it again via the same toggle -> must report a SECOND time.
  // This is exactly the regression from issue #743 finding 1: a permanent
  // WeakSet would silently swallow this second report.
  banner._setVisible(true);
  banner.setAttribute('class', 'cookie-banner cookie-banner--visible');
  env.flush();
  await Promise.resolve();

  const kinds = env.sentMessages.map((m) => m.kind);
  process.stdout.write(JSON.stringify({ kinds, messages: env.sentMessages }));
}

run().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
