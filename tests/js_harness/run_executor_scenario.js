// Executes the full extension (policy.js + executor.js + content.js, in
// manifest order) inside a vm context wired to tests/js_harness/dom_stub.js
// and runs ONE named executor scenario (argv[2]) from the registry below,
// printing a JSON verdict on stdout. Companion to run_command_scenario.js:
// that one covers stage-1 overlay commands, this one covers the stage-2
// executor primitives (#1160) — policy-gated clicks, explicit waits, page
// state — including the fail-closed refusals that must never click.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createEnvironment } = require('./dom_stub');
const { runExtensionInContext } = require('./load_extension');

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

runExtensionInContext(context);

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
// synchronous-batch MutationObserver, so overlays land in the registry like
// they would on a real page right after insertion.
function append(...nodes) {
  nodes.forEach((node) => env.document.body.appendChild(node));
  env.flush();
}

// Delivers a command to content.js's chrome.runtime.onMessage listener with
// an own-extension sender (sender.id === chrome.runtime.id), as the real
// background relay would. Resolves with the sendResponse payload; for the
// executor's async commands that arrives only after the wait budget ends.
function send(message) {
  return new Promise((resolve) => {
    env.chrome.runtime._listeners.forEach((fn) => {
      fn(message, { id: env.chrome.runtime.id }, resolve);
    });
  });
}

const SCENARIOS = {
  // Happy path: an explicitly commanded click on a control inside a safe
  // overlay (notification toast) with a declared post-click condition. The
  // site's reaction (hiding the control) satisfies the wait.
  click_safe_allowed: async () => {
    const details = el('button', { 'data-qa': 'notification-details' }, 'Подробнее');
    const toast = el('div', { class: 'toast-notification' }, 'Резюме обновлено');
    toast.appendChild(details);
    append(toast);
    const pending = send({
      action: 'click_element',
      dataQa: 'notification-details',
      waitFor: { state: 'hidden', dataQa: 'notification-details', timeoutMs: 2000 },
    });
    setTimeout(() => details._setVisible(false), 50);
    const response = await pending;
    return {
      ok: response.ok,
      verdict: response.result?.policy?.verdict ?? null,
      context: response.result?.policy?.context ?? null,
      clicked: response.result?.clicked ?? null,
      targetText: response.result?.target?.text ?? null,
      waitMet: response.result?.wait?.met ?? null,
      clickCount: env.clicks.length,
      clickedDetails: env.clicks.includes(details),
      url: response.result?.finalState?.url ?? null,
      overlaysAfter: (response.result?.finalState?.overlays ?? []).length,
    };
  },

  // Danger anchors on the target's own text refuse WITHOUT any click — both
  // inside a modal and bare on the page.
  click_dangerous_refused: async () => {
    const submit = el('button', { 'data-qa': 'resume-delete-submit' }, 'Подтвердить удаление');
    const modal = el('div', { class: 'modal', role: 'alertdialog' }, 'Удалить резюме?');
    modal.appendChild(submit);
    const bare = el('button', { 'data-qa': 'row-delete' }, 'Удалить');
    append(modal, bare);
    const inModal = await send({
      action: 'click_element',
      dataQa: 'resume-delete-submit',
      waitFor: { state: 'hidden', dataQa: 'resume-delete-submit', timeoutMs: 1000 },
    });
    const onPage = await send({
      action: 'click_element',
      dataQa: 'row-delete',
      waitFor: { state: 'hidden', dataQa: 'row-delete', timeoutMs: 1000 },
    });
    return {
      modalError: inModal.error ?? null,
      modalReason: inModal.policy?.reason ?? null,
      pageError: onPage.error ?? null,
      pageReason: onPage.policy?.reason ?? null,
      clickCount: env.clicks.length,
    };
  },

  // Apply-flow signals refuse without any click: the executor never enters
  // the apply scenario on its own — an EXPLICIT allowApply command (#1162)
  // is the only key. Danger anchors outrank it: a dangerous target refuses
  // even with allowApply=true.
  click_apply_step_refused: async () => {
    const apply = el('a', { 'data-qa': 'vacancy-response-link' }, 'Откликнуться');
    append(apply);
    const response = await send({
      action: 'click_element',
      dataQa: 'vacancy-response-link',
      waitFor: { state: 'hidden', dataQa: 'vacancy-response-link', timeoutMs: 1000 },
    });
    return {
      error: response.error ?? null,
      reason: response.policy?.reason ?? null,
      clickCount: env.clicks.length,
    };
  },

  click_apply_allowed_only_with_permission: async () => {
    const apply = el('a', { 'data-qa': 'vacancy-response-link-top' }, 'Откликнуться');
    const danger = el('button', { 'data-qa': 'withdraw-button' }, 'Отозвать отклик');
    append(apply, danger);
    const allowed = await send({
      action: 'click_element',
      dataQa: 'vacancy-response-link-top',
      allowApply: true,
      waitFor: { state: 'hidden', dataQa: 'vacancy-response-link-top', timeoutMs: 1000 },
    });
    const dangerRefused = await send({
      action: 'click_element',
      dataQa: 'withdraw-button',
      allowApply: true,
      waitFor: { state: 'hidden', dataQa: 'withdraw-button', timeoutMs: 1000 },
    });
    return {
      allowedOk: allowed.ok ?? null,
      allowedContext: allowed.result?.policy?.context ?? null,
      allowedClicked: allowed.result?.clicked ?? null,
      clickedApply: env.clicks.includes(apply),
      dangerError: dangerRefused.error ?? null,
      dangerReason: dangerRefused.policy?.reason ?? null,
    };
  },

  // fill_element (#1162): the letter text lands in the field through the
  // native setter path and is read back; a mismatch is ok:false, never a
  // silent half-filled form.
  fill_element_sets_value: async () => {
    const letter = el('textarea', { 'data-qa': 'vacancy-response-popup-form-letter-input' });
    append(letter);
    const response = await send({
      action: 'fill_element',
      dataQa: 'vacancy-response-popup-form-letter-input',
      text: 'Здравствуйте! Готов обсудить задачу.',
    });
    return {
      ok: response.ok ?? null,
      filled: response.result?.filled ?? null,
      length: response.result?.length ?? null,
      valueInField: letter.value,
    };
  },

  // A captcha-shaped field is never filled: danger anchors gate fill too.
  fill_element_dangerous_refused: async () => {
    const captcha = el('input', { 'data-qa': 'account-captcha-input' });
    append(captcha);
    const response = await send({
      action: 'fill_element',
      dataQa: 'account-captcha-input',
      text: '1234',
    });
    return {
      error: response.error ?? null,
      reason: response.policy?.reason ?? null,
      untouched: captcha.value === undefined,
    };
  },

  // A control inside an unclassifiable (ambiguous) overlay is refused:
  // fail-closed, the verdict is returned to the agent for a decision.
  click_ambiguous_overlay_refused: async () => {
    const ok = el('button', { 'data-qa': 'mystery-ok' }, 'OK');
    const modal = el('div', { class: 'modal', role: 'dialog' }, 'Незнакомое окно');
    modal.appendChild(ok);
    append(modal);
    const response = await send({
      action: 'click_element',
      dataQa: 'mystery-ok',
      waitFor: { state: 'hidden', dataQa: 'mystery-ok', timeoutMs: 1000 },
    });
    return {
      error: response.error ?? null,
      reason: response.policy?.reason ?? null,
      overlayType: response.policy?.overlay?.type ?? null,
      clickCount: env.clicks.length,
    };
  },

  // visible != hydrated (CLAUDE.md): a click without a declared post-click
  // condition is refused BEFORE clicking — no wait declaration, no click.
  click_without_wait_refused: async () => {
    const details = el('button', { 'data-qa': 'notification-details' }, 'Подробнее');
    const toast = el('div', { class: 'toast-notification' }, 'Резюме обновлено');
    toast.appendChild(details);
    append(toast);
    const response = await send({ action: 'click_element', dataQa: 'notification-details' });
    return {
      error: response.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // Одинаковый data-qa во всех матчах — один контрол в нескольких местах
  // страницы (шапка + липкая панель hh.ru, боевой прогон #1162): кликается
  // первый видимый — и по явному dataQa, и по чистому [data-qa='X'].
  // Неточный селектор ([data-qa*='...']) остаётся ambiguous.
  click_ambiguous_target: async () => {
    append(el('button', { 'data-qa': 'dup-button' }, 'Один'));
    append(el('button', { 'data-qa': 'dup-button' }, 'Два'));
    const sameQa = await send({
      action: 'click_element',
      dataQa: 'dup-button',
      waitFor: { state: 'hidden', dataQa: 'dup-button', timeoutMs: 1000 },
    });
    const css = await send({
      action: 'click_element',
      selector: '[data-qa="dup-button"]',
      waitFor: { state: 'hidden', dataQa: 'dup-button', timeoutMs: 1000 },
    });
    const fuzzy = await send({
      action: 'click_element',
      selector: "[data-qa*='dup-']",
      waitFor: { state: 'hidden', dataQa: 'dup-button', timeoutMs: 1000 },
    });
    return {
      sameQaClicked: sameQa.result?.clicked ?? null,
      sameQaText: sameQa.result?.target?.text ?? null,
      cssClicked: css.result?.clicked ?? null,
      fuzzyError: fuzzy.error ?? null,
      fuzzyMatchCount: fuzzy.matchCount ?? null,
      clickCount: env.clicks.length,
    };
  },

  // An invisible match is an action without evidence behind it: refused.
  click_invisible_target: async () => {
    const hidden = el('button', { 'data-qa': 'hidden-button' }, 'Скрытая');
    hidden._setVisible(false);
    append(hidden);
    const response = await send({
      action: 'click_element',
      dataQa: 'hidden-button',
      waitFor: { state: 'visible', dataQa: 'hidden-button', timeoutMs: 1000 },
    });
    return {
      error: response.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // wait_element 'visible': the element does not exist yet; it appears (as
  // an overlay, so the detection observer must also register it — the
  // observer keeps working while executor commands run, issue #1160 п.5).
  wait_element_appears: async () => {
    const pending = send({
      action: 'wait_element',
      dataQa: 'late-modal',
      state: 'visible',
      timeoutMs: 2000,
    });
    setTimeout(() => {
      append(el('div', { class: 'modal', 'data-qa': 'late-modal' }, 'Поздняя модалка'));
    }, 50);
    const response = await pending;
    const listed = await send({ action: 'list_overlays' });
    return {
      ok: response.ok,
      waitMet: response.result?.wait?.met ?? null,
      state: response.result?.state ?? null,
      matchCount: response.result?.finalState?.matchCount ?? null,
      visible: response.result?.finalState?.visible ?? null,
      overlayListed: listed.overlays.some((o) => o.id && o.type === 'modal'),
      clickCount: env.clicks.length,
    };
  },

  // An explicit timeout is honored and reported as a negative RESULT (ok),
  // never as an error or an endless poll.
  wait_element_timeout: async () => {
    const response = await send({
      action: 'wait_element',
      dataQa: 'never-appears',
      state: 'visible',
      timeoutMs: 120,
    });
    return {
      ok: response.ok,
      waitMet: response.result?.wait?.met ?? null,
      elapsedMs: response.result?.wait?.elapsedMs ?? null,
      clickCount: env.clicks.length,
    };
  },

  // Fail-closed validation: no state, or no explicit timeout — no waiting.
  wait_element_validation: async () => {
    const noState = await send({ action: 'wait_element', dataQa: 'x', timeoutMs: 1000 });
    const noTimeout = await send({ action: 'wait_element', dataQa: 'x', state: 'visible' });
    const noTarget = await send({ action: 'wait_element', state: 'visible', timeoutMs: 1000 });
    return {
      noStateError: noState.error ?? null,
      noTimeoutError: noTimeout.error ?? null,
      noTargetError: noTarget.error ?? null,
      clickCount: env.clicks.length,
    };
  },

  // get_page_state: current URL (plus title/readyState where the runtime
  // provides them) without touching any element.
  get_page_state: async () => {
    const response = await send({ action: 'get_page_state' });
    return {
      ok: response.ok,
      url: response.page?.url ?? null,
      clickCount: env.clicks.length,
    };
  },

  // check_element gains a census-style text field (#1160 п.4: чтение
  // DOM-состояния — наличие/текст/visibility).
  check_element_reports_text: async () => {
    append(el('button', { 'data-qa': 'next-button' }, 'Далее'));
    const present = await send({ action: 'check_element', selector: '[data-qa="next-button"]' });
    const absent = await send({ action: 'check_element', selector: '[data-qa="missing"]' });
    return {
      text: present.element?.text ?? null,
      absentText: absent.element?.text ?? null,
      found: present.element?.found ?? null,
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
