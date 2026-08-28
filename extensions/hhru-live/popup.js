const status = document.querySelector('#status');
const reports = document.querySelector('#reports');
chrome.tabs.query({ active: true, lastFocusedWindow: true }, async ([tab]) => {
  const connected = !!tab?.url && /^https:\/\/(?:[^/]+\.)?hh\.ru\//.test(tab.url);
  status.textContent = connected ? `Connected: ${tab.url}` : 'Not connected: open an https://hh.ru tab';
  const data = await chrome.storage.session?.get('recentReports');
  reports.textContent = JSON.stringify(data?.recentReports ?? [], null, 2);
});
