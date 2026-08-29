// Executes extensions/hhru-live/content.js against a scenario where an
// overlay starts CSS `visibility:hidden` (non-zero layout metrics, unlike
// display:none) and is later revealed by clearing that property. Regression
// coverage for PR #767 Codex round-3 finding 1: offsetWidth/offsetHeight/
// getClientRects() alone don't react to visibility:hidden, so isVisible()
// must also check computed visibility (via checkVisibility() or
// getComputedStyle().visibility) or the initial scan would treat the
// still-hidden overlay as already "seen", permanently suppressing the real
// reveal that follows.
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
  getComputedStyle: env.getComputedStyle,
  location: env.location,
  console,
});

// Modal already in the DOM before content.js runs, laid out (non-zero
// offsetWidth/offsetHeight/getClientRects) but CSS visibility:hidden --
// the initial scan must NOT report it while hidden this way.
const modal = env.document.createElement('div');
modal.setAttribute('class', 'modal');
modal._setCssVisibility(true); // visibility:hidden
env.document.documentElement.appendChild(modal);
env.flush();

vm.runInContext(source, context);

async function run() {
  await Promise.resolve(); // flush the initial-scan microtask

  // The initial scan must NOT report the modal while it is still
  // visibility:hidden -- this is exactly the bug: offsetWidth/offsetHeight/
  // getClientRects() alone don't react to visibility:hidden, so without a
  // real visibility check, the still-hidden modal would be misreported here.
  const reportsBeforeReveal = env.sentMessages.filter((m) => m.kind === 'overlay_detected').length;

  // Reveal it by clearing visibility:hidden (a style mutation, observed via
  // the attributeFilter's 'style' entry) -> must report exactly once, and
  // only now.
  modal._setCssVisibility(false);
  modal.setAttribute('style', 'visibility:visible');
  env.flush();
  await Promise.resolve();

  const overlayReports = env.sentMessages.filter((m) => m.kind === 'overlay_detected');
  process.stdout.write(JSON.stringify({
    reportsBeforeReveal,
    overlayReportCount: overlayReports.length,
    messages: overlayReports,
  }));
}

run().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
