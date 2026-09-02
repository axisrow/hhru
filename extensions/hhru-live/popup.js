const status = document.querySelector('#status');
const reports = document.querySelector('#reports');
const scanButton = document.querySelector('#scan');
const results = document.querySelector('#results');

function sendCommand(action, params) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ kind: 'agent_command', action, ...params }, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(response ?? { ok: false, error: 'no_response' });
    });
  });
}

function overlayRow(overlay) {
  const row = document.createElement('div');
  row.className = 'overlay-row';
  const label = document.createElement('span');
  const close = overlay.closeControls === 1 ? '1 close ctrl' : `${overlay.closeControls} close ctrls`;
  label.textContent = `${overlay.id} · ${overlay.type} · ${overlay.disposition} · ${close}`;
  if (overlay.text) label.title = overlay.text.slice(0, 300);
  row.appendChild(label);
  if (overlay.disposition === 'safe') {
    const dismiss = document.createElement('button');
    dismiss.textContent = 'Close';
    dismiss.addEventListener('click', async () => {
      dismiss.disabled = true;
      const response = await sendCommand('dismiss_overlay', { id: overlay.id });
      if (!response.ok) {
        dismiss.textContent = response.error ?? 'failed';
      } else {
        dismiss.textContent = response.result.overlayGone ? 'closed' : 'still open';
      }
      scanOverlays();
    });
    row.appendChild(dismiss);
  } else {
    const blocked = document.createElement('span');
    blocked.className = 'blocked';
    blocked.textContent = 'blocked';
    row.appendChild(blocked);
  }
  return row;
}

async function scanOverlays() {
  results.textContent = 'Scanning…';
  const response = await sendCommand('list_overlays');
  results.textContent = '';
  if (!response.ok) {
    // content_script_unreachable means the content script is not injected in
    // the active tab at all (freshly navigated page, or not an hh.ru tab).
    const note = document.createElement('p');
    note.textContent = `Scan failed: ${response.error}`;
    results.appendChild(note);
    return;
  }
  if (response.overlays.length === 0) {
    results.appendChild(document.createTextNode('No overlays detected.'));
    return;
  }
  response.overlays.forEach((overlay) => results.appendChild(overlayRow(overlay)));
}

scanButton.addEventListener('click', scanOverlays);

chrome.tabs.query({ active: true, lastFocusedWindow: true }, async ([tab]) => {
  const connected = !!tab?.url && /^https:\/\/(?:[^/]+\.)?hh\.ru\//.test(tab.url);
  status.textContent = connected ? `Connected: ${tab.url}` : 'Not connected: open an https://hh.ru tab';
  const data = await chrome.storage.session?.get('recentReports');
  reports.textContent = JSON.stringify(data?.recentReports ?? [], null, 2);
});
