// Loads the extension sources in manifest order (policy.js before
// content.js, #929) so every scenario runner mirrors what MV3 does for
// content_scripts of the same isolated world: top-level bindings of
// policy.js are visible to content.js.
'use strict';

const fs = require('fs');
const path = require('path');

const EXTENSION_DIR = path.join(__dirname, '..', '..', 'extensions', 'hhru-live');

function loadExtensionSources() {
  return ['policy.js', 'content.js'].map((name) => ({
    name,
    source: fs.readFileSync(path.join(EXTENSION_DIR, name), 'utf8'),
  }));
}

function runExtensionInContext(context) {
  const vm = require('vm');
  for (const { name, source } of loadExtensionSources()) {
    vm.runInContext(source, context, { filename: name });
  }
}

module.exports = { EXTENSION_DIR, loadExtensionSources, runExtensionInContext };
