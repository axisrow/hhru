// Prints the extension's known-overlays registry table (overlay_registry.js,
// #1227) as JSON on stdout for the Python steward test
// tests/test_overlay_registry_js.py: the JS data layer and the CLI layers
// (modal_registry.py + generated selectors) must agree on family names and
// anchors mechanically — the same two-copies-plus-guard pattern as the
// action allowlist (extensions/hhru-live/README.md, «Команды (allowlist)»).
// Loads ONLY the data file: the steward checks data, not executor behavior
// (that is run_executor_scenario.js's job).
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const EXTENSION_DIR = path.join(__dirname, '..', '..', 'extensions', 'hhru-live');
const source = fs.readFileSync(path.join(EXTENSION_DIR, 'overlay_registry.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context, { filename: 'overlay_registry.js' });
const rules = vm.runInContext('JSON.parse(JSON.stringify(OVERLAY_CONTEXT_RULES))', context);
process.stdout.write(JSON.stringify({ rules }));
