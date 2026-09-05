// Policy-core scenarios for issue #929: runs extensions/hhru-live/policy.js
// alone inside a vm context wired to tests/js_harness/dom_stub.js and asserts
// classification decisions DIRECTLY (classifyDisposition / findCloseControls),
// not through content.js's registry and commands — those have their own
// runners. Focus: fail-closed priority and the #932 live-verified anchors
// (aria-label in the danger scan, data-qa *-close cross, «Понятно» never a
// close control).
'use strict';

const vm = require('vm');
const { createEnvironment } = require('./dom_stub');

const { loadExtensionSources } = require('./load_extension');
const policy = loadExtensionSources().find((f) => f.name === 'policy.js');
if (!policy) {
  process.stderr.write('policy.js not found in extension sources\n');
  process.exit(1);
}

const env = createEnvironment();
const context = vm.createContext({
  document: env.document,
  MutationObserver: env.MutationObserver,
  Element: env.Element,
  getComputedStyle: env.getComputedStyle,
  location: env.location,
  console,
});
vm.runInContext(policy.source, context, { filename: 'policy.js' });

function el(tag, attrs = {}, text = '') {
  const element = env.document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
  if (text) element.textContent = text;
  element._setVisible(true);
  return element;
}

const SCENARIOS = {
  // #932 hazard: a notification whose only «close» control is a delete
  // button addressed by aria-label — invisible to textContent, so before
  // collectText included aria-labels this classified safe with a clickable
  // deletion. Must be dangerous.
  aria_label_delete_is_dangerous: () => {
    const del = el('button', { 'aria-label': 'Удалить' });
    const note = el('div', { class: 'notification-manager__item', 'data-qa': 'notification-close' }, 'Резюме обновлено');
    note.appendChild(del);
    return {
      disposition: context.classifyDisposition(note, context.classify(note)),
      dangerHit: Boolean(note.textContent.match(/удал/i)),
    };
  },

  // #932 live fact: the real hh.ru cookie informer carries «cookie» only in
  // data-qa (class is wrapper--*) — and its «Понятно» button must NOT be
  // found as a close control (it accepts consent!).
  cookie_informer_ponyatno_never_close: () => {
    const accept = el('button', { 'data-qa': 'cookies-policy-informer-accept' }, 'Понятно');
    const informer = el('div', { 'data-qa': 'cookies-policy-informer', class: 'wrapper--UZEraJ9YBXy3riZk' },
      'Чтобы сайт был удобнее, используем cookies');
    informer.appendChild(accept);
    const controls = context.findCloseControls(informer);
    return {
      type: context.classify(informer).type,
      disposition: context.classifyDisposition(informer, context.classify(informer)),
      closeCount: controls.length,
      clickedAcceptSafe: !controls.includes(accept),
    };
  },

  // Live hh.ru crosses are data-qa *-close WITHOUT aria-label or glyphs
  // (svg icon, no text) — the data-qa /close/ arm must find them.
  real_hhru_cross_via_data_qa: () => {
    const cross = el('button', { 'data-qa': 'profile-modal-button-close' });
    const modal = el('div', { class: 'magritte-modal___C4o5U', role: 'dialog' }, 'Язык');
    modal.appendChild(cross);
    modal.appendChild(el('button', { 'data-qa': 'profile-modal-button-save' }, 'Сохранить'));
    const controls = context.findCloseControls(modal);
    return {
      closeCount: controls.length,
      onlyCross: controls.length === 1 && controls[0] === cross,
      disposition: context.classifyDisposition(modal, context.classify(modal)),
    };
  },

  // Fail-closed priority: danger anchors outrank apply signals even when
  // both are present in the same modal.
  danger_outranks_apply: () => {
    const form = el('form', { id: 'RESPONSE_MODAL_FORM_ID' });
    form.appendChild(el('textarea', { 'data-qa': 'vacancy-response-popup-form-letter-input' }));
    const modal = el('div', { class: 'modal', role: 'dialog' },
      'Сопроводительное письмо. Подтвердите отправку — действие необратимо');
    modal.appendChild(form);
    return { disposition: context.classifyDisposition(modal, context.classify(modal)) };
  },

  // apply_step without any danger anchor.
  apply_step_structural: () => {
    const form = el('form', { id: 'RESPONSE_MODAL_FORM_ID' });
    form.appendChild(el('textarea', { 'data-qa': 'vacancy-response-popup-form-letter-input' }));
    const modal = el('div', { class: 'modal', role: 'dialog' }, 'Сопроводительное письмо');
    modal.appendChild(form);
    return { disposition: context.classifyDisposition(modal, context.classify(modal)) };
  },

  // «удалённая работа» must NOT be dangerous (PR #935 review): bare
  // stem-удал is not an anchor, only удалить/удалени forms are.
  remote_work_not_dangerous: () => {
    const modal = el('div', { class: 'modal', role: 'dialog' },
      'Формат работы: удалённая. Отменить | Сохранить');
    return { disposition: context.classifyDisposition(modal, context.classify(modal)) };
  },
};

async function main() {
  const name = process.argv[2];
  const scenario = SCENARIOS[name];
  if (!scenario) {
    process.stderr.write(`unknown scenario: ${name}; available: ${Object.keys(SCENARIOS).join(', ')}\n`);
    process.exit(1);
  }
  const result = scenario();
  process.stdout.write(JSON.stringify({ scenario: name, ...result }));
}

main().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
