// Executes extensions/hhru-live/content.js inside a vm context wired to
// tests/js_harness/dom_stub.js and runs ONE named command scenario
// (argv[2]) from the registry below, printing a JSON verdict on stdout.
// Companion to run_content_scenario.js / run_visibility_hidden_scenario.js:
// those cover detection; this one covers the policy layer (classification,
// allowlisted commands, safe-dismiss) added for issue #588 stage 1.
//
// Each scenario returns only the fields the pytest wrapper asserts on, so a
// failure message quotes the actual observed values, not the whole world.
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
  setTimeout,
  clearTimeout,
  console,
});

vm.runInContext(source, context);

function el(tag, attrs = {}, text = '') {
  const element = env.document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
  if (text) element.textContent = text;
  // A freshly appended element on a real page has layout; the stub starts at
  // zero, so simulate the rendered state explicitly.
  element._setVisible(true);
  return element;
}

// Appends nodes to document.body (like a real page would) and flushes the
// synchronous-batch MutationObserver, so the overlay lands in the registry
// like it would on a real page right after insertion.
function append(...nodes) {
  nodes.forEach((node) => env.document.body.appendChild(node));
  env.flush();
}

// Delivers a command to content.js's chrome.runtime.onMessage listener with
// an own-extension sender (sender.id === chrome.runtime.id), as the real
// background relay would. Resolves with the sendResponse payload (for
// dismiss_overlay this arrives only after the gone-polling completes).
function send(message) {
  return new Promise((resolve) => {
    env.chrome.runtime._listeners.forEach((fn) => {
      fn(message, { id: env.chrome.runtime.id }, resolve);
    });
  });
}

const SCENARIOS = {
  // Plain toast with a labelled close control: safe, dismissible, and the
  // click lands on the close control only.
  toast_safe: async () => {
    const close = el('span', { 'aria-label': 'Закрыть' }, '×');
    const toast = el('div', { class: 'toast-notification' }, 'Резюме обновлено');
    toast.appendChild(close);
    append(toast);
    const listed = await send({ action: 'list_overlays' });
    const pending = send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    // The site's own reaction to the close click: hide the toast.
    setTimeout(() => toast._setVisible(false), 50);
    const dismissed = await pending;
    return {
      listedCount: listed.overlays.length,
      listedType: listed.overlays[0].type,
      listedDisposition: listed.overlays[0].disposition,
      dismissedOk: dismissed.ok,
      overlayGone: dismissed.result?.overlayGone ?? null,
      clickCount: env.clicks.length,
      clickedClose: env.clicks.includes(close),
    };
  },

  // Cookie banner closed via its class-marked close button.
  cookie_banner: async () => {
    const close = el('button', { class: 'cookie-banner-close' }, '×');
    const banner = el('div', { class: 'cookie-banner' }, 'Мы используем cookie');
    banner.appendChild(close);
    append(banner);
    const listed = await send({ action: 'list_overlays' });
    const pending = send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    setTimeout(() => banner._setVisible(false), 50);
    const dismissed = await pending;
    return {
      listedType: listed.overlays[0].type,
      listedDisposition: listed.overlays[0].disposition,
      dismissedOk: dismissed.ok,
      overlayGone: dismissed.result?.overlayGone ?? null,
      clickCount: env.clicks.length,
      clickedClose: env.clicks.includes(close),
    };
  },

  // The #586 popup: «Резюме доставлено» with a status-saving «Сохранить»
  // button and an × close. Only the × may ever be clicked.
  resume_delivered_never_saves: async () => {
    const save = el('button', { 'data-qa': 'save-status-button' }, 'Сохранить');
    const close = el('button', { 'aria-label': 'Закрыть' }, '×');
    const modal = el('div', { class: 'modal', role: 'dialog' }, 'Резюме доставлено');
    modal.appendChild(save);
    modal.appendChild(close);
    append(modal);
    const listed = await send({ action: 'list_overlays' });
    const pending = send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    setTimeout(() => modal._setVisible(false), 50);
    const dismissed = await pending;
    return {
      listedDisposition: listed.overlays[0].disposition,
      dismissedOk: dismissed.ok,
      overlayGone: dismissed.result?.overlayGone ?? null,
      action: dismissed.result?.action ?? null,
      clickCount: env.clicks.length,
      clickedClose: env.clicks.includes(close),
      clickedSave: env.clicks.includes(save),
    };
  },

  // Response-form modal (apply_step): never auto-dismissed.
  apply_step_blocked: async () => {
    const close = el('button', { 'aria-label': 'Закрыть' }, '×');
    const form = el('form', { id: 'RESPONSE_MODAL_FORM_ID', name: 'vacancy_response' });
    form.appendChild(el('textarea', { 'data-qa': 'vacancy-response-popup-form-letter-input' }));
    form.appendChild(close);
    const modal = el('div', { class: 'modal', role: 'dialog' }, 'Сопроводительное письмо');
    modal.appendChild(form);
    append(modal);
    const listed = await send({ action: 'list_overlays' });
    const dismissed = await send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    return {
      listedDisposition: listed.overlays[0].disposition,
      dismissedOk: dismissed.ok,
      error: dismissed.error ?? null,
      errorDisposition: dismissed.disposition ?? null,
      clickCount: env.clicks.length,
    };
  },

  // Irreversible-action confirmation: dangerous, blocked despite a close
  // control being present.
  danger_confirm_blocked: async () => {
    const close = el('button', { 'aria-label': 'Закрыть' }, '×');
    const modal = el('div', { class: 'modal', role: 'alertdialog' },
      'Подтвердите удаление резюме? Это действие необратимо');
    modal.appendChild(close);
    append(modal);
    const listed = await send({ action: 'list_overlays' });
    const dismissed = await send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    return {
      listedDisposition: listed.overlays[0].disposition,
      dismissedOk: dismissed.ok,
      error: dismissed.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // CAPTCHA text is dangerous even without any other danger anchor.
  danger_captcha_blocked: async () => {
    const modal = el('div', { class: 'modal', role: 'dialog' }, 'Подтвердите, что вы не робот');
    append(modal);
    const listed = await send({ action: 'list_overlays' });
    const dismissed = await send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    return {
      listedDisposition: listed.overlays[0].disposition,
      dismissedOk: dismissed.ok,
      error: dismissed.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // Unknown modal without a close control: ambiguous, blocked.
  ambiguous_blocked: async () => {
    const modal = el('div', { class: 'modal', role: 'dialog' }, 'Незнакомое окно без кнопок');
    append(modal);
    const listed = await send({ action: 'list_overlays' });
    const dismissed = await send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    return {
      listedDisposition: listed.overlays[0].disposition,
      dismissedOk: dismissed.ok,
      error: dismissed.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // Anything outside the allowlist is rejected before touching the DOM.
  unknown_action_rejected: async () => {
    const response = await send({ action: 'close_all_windows' });
    return {
      ok: response.ok,
      error: response.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // Overlay hidden (or detached) between listing and dismissal: no click —
  // acting on an invisible control is an action without evidence behind it.
  dismiss_hidden_overlay: async () => {
    const close = el('button', { 'aria-label': 'Закрыть' }, '×');
    const toast = el('div', { class: 'toast-notification' }, 'Всплывашка');
    toast.appendChild(close);
    append(toast);
    const listed = await send({ action: 'list_overlays' });
    toast._setVisible(false);
    const dismissed = await send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    return {
      listedCount: listed.overlays.length,
      dismissedOk: dismissed.ok,
      error: dismissed.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // The overlay is visible but its only close control is a hidden
  // template/duplicate (display:none): no click at all — clicking an
  // invisible control is an action without evidence behind it (PR #935
  // review), answered no_close_control instead of a silent no-op click.
  dismiss_hidden_close_control: async () => {
    const close = el('button', { 'aria-label': 'Закрыть' }, 'x');
    close._setVisible(false);
    const toast = el('div', { class: 'toast-notification' }, 'Тост со скрытым крестиком');
    toast.appendChild(close);
    append(toast);
    const listed = await send({ action: 'list_overlays' });
    const dismissed = await send({ action: 'dismiss_overlay', id: listed.overlays[0].id });
    return {
      listedCount: listed.overlays.length,
      dismissedOk: dismissed.ok,
      error: dismissed.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // html/body carry state classes (hh.ru: cookie-policy-banner-enabled on
  // <body>, confirmed live 2026-09-02) that match [class*="cookie"] — the
  // page shell must never register as an overlay, or the whole page text
  // gets classified.
  body_state_class_never_registered: async () => {
    env.document.body.setAttribute('class', 'cookie-policy-banner-enabled');
    env.flush();
    const listed = await send({ action: 'list_overlays' });
    return { listedCount: listed.overlays.length };
  },

  // check_element: confirmation that the next step's element is reachable.
  check_element: async () => {
    const next = el('button', { 'data-qa': 'next-button' }, 'Далее');
    append(next);
    const present = await send({ action: 'check_element', selector: '[data-qa="next-button"]' });
    const absent = await send({ action: 'check_element', selector: '[data-qa="missing-button"]' });
    const noSelector = await send({ action: 'check_element' });
    return {
      found: present.element?.found ?? null,
      visible: present.element?.visible ?? null,
      obstructionChecked: present.element?.obstructionChecked ?? null,
      absentFound: absent.element?.found ?? null,
      noSelectorFound: noSelector.element?.found ?? null,
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
