# HH.ru Live Overlay Diagnostics (stage 1)

Load this directory via `chrome://extensions` → Developer mode → Load unpacked. Open an already authenticated `https://hh.ru` tab and click the extension icon. The content script observes dynamically added dialog/modal/toast/notification/cookie elements and reports structured, detection-only events. It never closes or clicks anything.

The agent channel is `chrome.runtime.connect({name: "hhru-agent"})`, reachable only from an **external** caller (no code in this extension itself opens that port — content.js and popup.js never call `chrome.runtime.connect`); stage one has an explicit empty action allowlist and returns `action_not_allowed` for every command, and `background.js` validates the connecting sender's tab URL is an hh.ru origin before accepting messages. Diagnostics are stored in `chrome.storage.session` and rehydrated on service-worker startup so history survives an MV3 SW restart. Permissions are limited to the HH.ru hosts plus the `storage` API permission (required by `chrome.storage.session`; without it `chrome.storage` is `undefined` and the popup would always show an empty report list).

## CLI question

An MV3 extension cannot install or launch a local CLI: Chrome extension APIs do not provide arbitrary process execution. A future bridge can use **Native Messaging** (a separately installed host manifest and executable) or a CLI-owned local HTTP/WebSocket server. Native Messaging is the selected future option because it avoids exposing a listening network port; it is intentionally not part of this stage.

Issue #588 status: this PR delivers the MV3 structure, live-tab content script, MutationObserver detection, diagnostics popup, and allowlisted transport skeleton. Policy, safe-popup closing, richer classification, and production agent bridge remain follow-up work (including #586).
