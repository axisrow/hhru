// Policy core of issue #929: classification of overlays into
// dangerous / apply_step / safe / ambiguous, fail-closed, plus the
// explicit-close-control finder. Extracted verbatim from content.js
// (where it lived since the #935 MVP) so that the policy anchors live in
// one file and content.js only detects, registers and commands.
//
// Loaded by manifest.json BEFORE content.js: classic content scripts of
// the same isolated world share top-level bindings in load order, so
// content.js consumes these constants and functions as globals.
//
// The ONLY mutating action anywhere in the extension remains the close
// control click inside content.js dismissOverlay(); nothing here clicks.
//
// Anchor provenance — see README «Селекторы — статус проверки»: close
// markers and the cookie informer are confirmed against live DOM
// (#932, 2026-09-05); the apply-flow anchors remain hypotheses until a
// combat apply run (clicking «Откликнуться» creates a reply topic).

// Danger anchors are matched against the overlay's own + descendant text
// AND aria-labels (see collectText). Intentionally narrow: a miss keeps
// the overlay merely blocked (safe side); the /подтверд|удалени/ families
// also cover "подтвердите удаление" style confirmations. Known trade-off:
// a cookie banner phrased as «подтвердите согласие» would land here too —
// fail-closed, it stays and is reported.
const DANGEROUS_TEXT = [
  /captcha/i, /не робот/i,
  // Deliberately NOT the bare stem-удал pattern: that substring also matches
  // «удалённая работа», which would permanently mark routine remote-work
  // popups as dangerous (PR #935 review). The infinitive/noun forms still
  // cover «подтвердите удаление».
  /удалить|удалени/i, /отозвать/i, /отмена отклика/i, /withdraw/i,
  /необратим/i, /irreversible/i,
  /вы уверены/i, /are you sure/i,
  /подтверд/i, /confirm/i
];
// Apply-flow anchors: structural (form id, data-qa namespace, task-question
// classes — both response-modal shapes from CLAUDE.md) and text wording.
// Bare «отклик» is intentionally absent: hh.ru's post-submit success toast
// «Отклик отправлен» is a notification, not part of the form, and must stay
// dismissible (PR #935 review). The response form itself is covered by the
// structural anchors below.
const APPLY_TEXT = [/сопроводительн/i, /тестовое задание/i, /анкет/i];
// Статус #932 (2026-09-05): структурные apply-якоря (RESPONSE_MODAL_FORM_ID,
// vacancy-response data-qa) и APPLY_TEXT остаются ГИПОТЕЗАМИ — живая модалка
// отклика не снималась, потому что клик «Откликнуться» создаёт тему отклика
// (инцидент 2026-08-16); подтверждение возможно только в боевом apply
// (второй этап). Close-маркеры и cookie-информер при этом подтверждены
// живым DOM — см. README, таблицу статусов.
const APPLY_QA = /vacancy-response/i;
const APPLY_CLASS = /task-question|task-body/i;
const APPLY_FORM_ID = /RESPONSE_MODAL_FORM_ID/;
const CLOSE_LABEL = /закрыт|close|dismiss/i;
// #932, live DOM 2026-09-05 (залогиненный профиль + реестр селекторов):
// реальные крестики hh.ru — это data-qa с суффиксом -close
// (profile-modal-button-close, photo-viewer-close, bloko-modal-close,
// editor-modal-close-icon, resume-delete-close); aria-label «закрыть» и
// глифы ×/✕ на них НЕ встречаются (иконка — svg без текста). Значит
// рабочее плечо здесь — именно /close/ по data-qa/class; label- и
// glyph-плечи остаются как совместимость с прочими библиотечными
// модалками, живых опровержений нет.
// A lone latin "x" is weaker evidence than ×/✕: decorative spans with a
// data-qa attribute and an x glyph exist on real pages, and it must not be
// clickable just because of the attribute (PR #935 review).
const CLOSE_GLYPH = /^[×✕]$/;
const CLOSE_LATIN_X = /^x$/i;

function isVisible(element) {
  // offsetWidth/offsetHeight/getClientRects() only react to display:none —
  // a visibility:hidden element still reports non-zero layout metrics, so it
  // would be treated as "visible" here (Codex round-3 review of #767: this
  // silently defeats the hide->show re-detect gate for that CSS pattern).
  // checkVisibility() (Chrome 105+) covers visibility/display/content-visibility
  // in one call; fall back to the layout check + an explicit visibility read
  // for older Chrome (MV3's floor is Chrome 88).
  if (typeof element.checkVisibility === 'function') {
    return element.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true });
  }
  const hasLayout = !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  if (!hasLayout) return false;
  return getComputedStyle(element).visibility !== 'hidden';
}

function classify(element) {
  const text = (element.innerText || element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 500);
  const role = element.getAttribute('role');
  const className = element.getAttribute('class') ?? '';
  const type = /cookie/i.test(className + text)
    ? 'cookie_banner' : role === 'dialog' || role === 'alertdialog' || /modal|popup/i.test(className)
    ? 'modal' : /toast|notification/i.test(className) ? 'notification' : 'overlay';
  return { type, text, role, className: className.slice(0, 200), visible: isVisible(element) };
}

// Overlay text plus every descendant's own text node. In a real browser
// element.textContent already includes the subtree (descendants double up,
// harmless for regex matching); the per-node loop keeps the js-harness stub,
// whose textContent is per-node, working with the same code path.
// #932 (live DOM 2026-09-05): aria-label тоже в скане — в реестре селекторов
// зафиксирована живая hh.ru-нотификация, чей единственный close-контрол —
// button[aria-label="Удалить"] ([data-qa='notification-close'] button) —
// текстом «удалить» нигде не виден, и без этого такая нотификация
// классифицировалась бы safe с кликабельным «удалением».
function collectText(element) {
  const parts = [element.textContent || ''];
  walkDescendants(element, (node) => {
    parts.push(node.textContent || '');
    // aria-label — часть видимого пользователю смысла кнопки, хотя в
    // textContent не попадает никогда.
    parts.push(node.getAttribute ? (node.getAttribute('aria-label') || '') : '');
  });
  return parts.join(' ').trim().replace(/\s+/g, ' ');
}

function walkDescendants(element, visit) {
  visit(element);
  // element.children is an HTMLCollection: array-LIKE but without forEach
  // (unlike NodeList) — iterating it with .forEach throws. Array.from works
  // for both the live collection and the test stub's plain array.
  Array.from(element.children || []).forEach((child) => walkDescendants(child, visit));
}

function findCloseControls(element) {
  const controls = [];
  walkDescendants(element, (node) => {
    if (node === element) return;
    const label = `${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''}`;
    // An explicit accessible "close" label is deliberate author intent and is
    // sufficient on its own; weaker markers (data-qa/class/glyph) must also
    // sit on something interactive. Status-word buttons of dialogs (agree /
    // cancel / submit phrasings, #586) never match any anchor below by
    // construction — none of their texts is a close marker.
    if (CLOSE_LABEL.test(label)) { controls.push(node); return; }
    const qa = (node.getAttribute('data-qa') || '').toLowerCase();
    const cls = (node.getAttribute('class') || '').toLowerCase();
    const role = (node.getAttribute('role') || '').toLowerCase();
    const text = (node.textContent || '').trim().replace(/\s+/g, ' ');
    const tagInteractive = ['button', 'a'].includes(String(node.tagName).toLowerCase())
      || role === 'button';
    const weaklyInteractive = qa !== '' || cls.includes('close');
    if (!tagInteractive && !weaklyInteractive) return;
    if (/close/.test(qa) || /close/.test(cls) || CLOSE_GLYPH.test(text)) {
      controls.push(node);
      return;
    }
    // Latin "x" counts only on a real button/a/role=button: a data-qa or
    // class alone must not make a decorative x-glyph span clickable.
    if (CLOSE_LATIN_X.test(text) && tagInteractive) controls.push(node);
  });
  return controls;
}

function hasApplySignal(element, text) {
  if (APPLY_FORM_ID.test(element.getAttribute('id') || '')) return true;
  if (APPLY_TEXT.some((re) => re.test(text))) return true;
  let found = false;
  walkDescendants(element, (node) => {
    if (APPLY_QA.test(node.getAttribute('data-qa') || '')) found = true;
    if (APPLY_CLASS.test(node.getAttribute('class') || '')) found = true;
  });
  return found;
}

// Fail-closed priority, never reorder: dangerous outranks apply_step, both
// outrank any auto-dismissable disposition.
function classifyDisposition(element, info) {
  const text = collectText(element);
  if (DANGEROUS_TEXT.some((re) => re.test(text))) return 'dangerous';
  if (hasApplySignal(element, text)) return 'apply_step';
  // Toasts/notifications are harmless by nature even without a close
  // control (they auto-dismiss); modal-shaped overlays without an explicit
  // close control are ambiguous, never guessed at.
  if (info.type === 'notification' || info.type === 'cookie_banner') return 'safe';
  return findCloseControls(element).length > 0 ? 'safe' : 'ambiguous';
}
