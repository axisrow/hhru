// Stage 2 executor of issue #1160: allowlisted DOM primitives that act on
// the live hh.ru tab on behalf of the external agent. The channel is the
// loopback WebSocket bridge in background.js (protocol per issue #1159);
// this file never talks to the network itself.
//
// Three commands, dispatched from content.js's single onMessage listener:
//   click_element  {dataQa|label|selector, waitFor: {state, timeoutMs, dataQa|label|selector}, allowApply?}
//   wait_element   {dataQa|label|selector, state: 'visible'|'hidden', timeoutMs}
//   get_page_state {}
//   fill_element   {dataQa|label|selector, text}   (#1162: letter textarea)
//
// Policy: EVERY click passes the #929 policy core (policy.js) BEFORE the
// click happens — danger anchors over the target's own subtree text and
// aria-labels, apply-flow signals, then the disposition of the overlay the
// target sits in (the TOPMOST overlay ANCESTOR — the modal family collapsed
// into one overlay, #1214; the target itself is judged by
// its content, so clicking an overlay container still scans everything
// inside it). dangerous / apply_step / ambiguous refuse WITHOUT any click;
// only a safe context clicks. This file adds no anchors and never widens
// the stage-1 gates — it reuses them verbatim.
//
// #1162 (S4 apply scenario): an explicit command MAY carry allowApply=true —
// the agent's own decision to run the apply flow, the same way it explicitly
// names the target. It downgrades ONLY the apply_step refusal of the target
// itself (context 'apply_flow') and of its overlay ancestor (context
// 'apply_flow_overlay'); DANGEROUS_TEXT always refuses without any click.
// An ambiguous-overlay ancestor refuses too — EXCEPT the picker-drop case
// (#1220): allowApply + ambiguous + a visible apply_step overlay on the page
// (the response form is open) is the apply flow's own floating UI (context
// 'apply_flow_picker_overlay'). Stage-1 auto-dismiss semantics (no
// allowApply) are unchanged.
//
// The ONLY click in this file is target.click() inside clickElement(), the
// same confirmed click method dismissOverlay() uses (a real DOM click, no
// synthetic event construction). It is reached only after: unambiguous
// target -> visible -> policy allowed -> an explicit waitFor declared.
// visible != hydrated (CLAUDE.md): the outcome of a click that triggers a
// React re-render is proven only by waiting for the declared DOM condition,
// never by the click call returning — so a click without waitFor is refused
// (wait_required) before any click happens.
//
// Explicit agent commands are not stage-1 auto-dismiss: dismiss_overlay
// still never picks action buttons («Понятно», «Сохранить») as close
// controls, while click_element executes the agent's explicit decision —
// behind the same classification.

const EXECUTOR_POLL_MS = 100;
// Explicit timeouts are required by the protocol; this cap only stops a
// stuck command from polling forever.
const EXECUTOR_TIMEOUT_CAP_MS = 60000;

function normalizeLabelText(value) {
  return String(value).trim().replace(/\s+/g, ' ').toLowerCase();
}

function findByDataQa(value) {
  const safe = String(value).replace(/[\\"]/g, '\\$&');
  return Array.from(document.querySelectorAll(`[data-qa="${safe}"]`));
}

// Accessible-name addressing (CLAUDE.md: Magritte inputs have no data-qa —
// they are addressed by label): aria-label / title / visible text, exact
// match after whitespace normalization, case-insensitive.
function findByLabel(value) {
  const wanted = normalizeLabelText(value);
  const nodes = document.querySelectorAll('button, a, [role="button"], input, textarea, select, [data-qa]');
  return Array.from(nodes).filter((node) => {
    const candidates = [node.getAttribute('aria-label'), node.getAttribute('title'), node.textContent];
    return candidates.some((text) => normalizeLabelText(text || '') === wanted);
  });
}

// Exactly one addressing mode per command (fail-closed: passing both or
// neither is an error, not a precedence guess).
function resolveTargets(params) {
  const modes = [];
  if (typeof params.dataQa === 'string' && params.dataQa.trim() !== '') modes.push('dataQa');
  if (typeof params.label === 'string' && params.label.trim() !== '') modes.push('label');
  if (typeof params.selector === 'string' && params.selector.trim() !== '') modes.push('selector');
  if (modes.length !== 1) return { error: 'target_required', modes };
  try {
    if (modes[0] === 'dataQa') return { matches: findByDataQa(params.dataQa) };
    if (modes[0] === 'label') return { matches: findByLabel(params.label) };
    return { matches: Array.from(document.querySelectorAll(params.selector)) };
  } catch (error) {
    return { error: 'selector_invalid' };
  }
}

// The overlay the target sits in — strictly ABOVE the target (hh.ru puts
// "cookie"/"modal" substrings into leaf data-qa attributes too, so counting
// the target itself would classify controls by substring luck). html/body
// are never overlays: same guard as reportIfNewlyVisible, because hh.ru
// marks cookie state with classes on <body>.
//
// #1214 (боевой прогон testing 2026-09-23): одна модалка — СЕМЕЙСТВО вложенных
// overlay-узлов (реестр учитывает их раздельно: outer apply_step, inner
// ambiguous), и клик бьёт по inner. Семейство сворачивается в один оверлей —
// вердикт снимается по ВЕРХНЕМУ overlay-предку: apply_step формы отклика
// снаружи понижается allowApply как раньше, а опасный/ambiguous наружный
// больше не обходится через безопасный внутренний узел (раньше ближайший
// safe-предок маскировал dangerous-модалку вокруг него).
function findOverlayContext(node) {
  let current = node.parentNode;
  let topmost = null;
  while (current && typeof current.matches === 'function') {
    if (current !== document.documentElement && current !== document.body
      && OVERLAY_SELECTORS.some((selector) => current.matches(selector))) topmost = current;
    current = current.parentNode;
  }
  return topmost;
}

// #1220 (боевой census testing 2026-09-24): drop-панель пикера резюме
// (magritte-drop-base, role=dialog) порталится Magritte'ом в body ВНЕ
// семейства модалки отклика — вердикт клика по опции снимается по
// панельному ambiguous (якорей и close-контролов у панели нет и быть не
// должно), а apply_step-модалка остаётся отдельным поддеревом. Признак
// «клик идёт внутри UI стека открытой формы отклика» — видимый
// apply_step-overlay на странице (решение в момент клика, не из реестра
// детект-тайма; listOverlays пере-классифицирует и подчищает невидимое).
function hasVisibleApplyStepOverlay() {
  return listOverlays().some((overlay) => overlay.disposition === 'apply_step');
}

// The #929 policy core applied to a click target. Same fail-closed priority
// as classifyDisposition: danger anchors outrank apply signals, both refuse.
// allowApply (#1162) downgrades apply_step refusals — the target's own AND
// its overlay ancestor's (the response modal IS the apply flow: picker,
// letter toggle and submit all sit inside it and match the structural
// anchors). DANGEROUS targets and ambiguous/dangerous overlays still refuse
// exactly as before; without the flag nothing changes (#929 stage-1).
function evaluateClickPolicy(target, allowApply) {
  const text = collectText(target);
  if (DANGEROUS_TEXT.some((re) => re.test(text))) {
    return { verdict: 'refused', reason: 'dangerous', targetText: text.slice(0, 200) };
  }
  const targetApply = hasApplySignal(target, text);
  if (targetApply && allowApply !== true) {
    return { verdict: 'refused', reason: 'apply_step' };
  }
  const overlay = findOverlayContext(target);
  if (!overlay) return { verdict: 'allowed', context: targetApply ? 'apply_flow' : 'page' };
  const info = classify(overlay);
  const disposition = classifyDisposition(overlay, info);
  if (disposition !== 'safe') {
    if (allowApply === true && disposition === 'apply_step') {
      return { verdict: 'allowed', context: 'apply_flow_overlay' };
    }
    // #1220: панель пикера резюме — отдельный ambiguous-оверлей вне семейства
    // модалки; флоу отклика продолжается, пока форма открыта. Dangerous
    // сюда не доходит (проверен выше и в classifyDisposition), без флага
    // и без открытой apply-модалки — прежний отказ.
    if (allowApply === true && disposition === 'ambiguous' && hasVisibleApplyStepOverlay()) {
      return { verdict: 'allowed', context: 'apply_flow_picker_overlay' };
    }
    return { verdict: 'refused', reason: disposition, overlay: info };
  }
  return { verdict: 'allowed', context: 'safe_overlay', disposition, overlayType: info.type };
}

function resolveTimeout(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    return { error: 'timeout_required' };
  }
  // ponytail: hard cap against a stuck poll loop; raise together with a
  // server-side budget if a legitimate wait ever needs more
  return { timeoutMs: Math.min(value, EXECUTOR_TIMEOUT_CAP_MS) };
}

// The post-click condition, declared by the command itself. Re-resolved on
// every poll: the waited-for element usually does not exist yet.
function resolveWait(waitFor) {
  if (!waitFor || typeof waitFor !== 'object') return { error: 'wait_required' };
  if (waitFor.state !== 'visible' && waitFor.state !== 'hidden') return { error: 'wait_invalid' };
  const timeout = resolveTimeout(waitFor.timeoutMs);
  if (timeout.error) return timeout;
  const target = resolveTargets(waitFor);
  if (target.error) {
    return { error: target.error === 'target_required' ? 'wait_invalid' : target.error };
  }
  return { params: waitFor, state: waitFor.state, timeoutMs: timeout.timeoutMs };
}

function waitConditionMet(wait) {
  // A poll-time resolution failure (practically unreachable: resolveWait
  // pre-validated this exact selector and querySelectorAll is deterministic)
  // must degrade to "not met" — the poll loop then ends at the explicit
  // timeout and the command always answers, never hangs silently.
  const matches = resolveTargets(wait.params).matches || [];
  const anyVisible = matches.some((element) => isVisible(element));
  return wait.state === 'visible' ? anyVisible : !anyVisible;
}

function waitForCondition(wait, onDone) {
  const startedAt = Date.now();
  const poll = () => {
    if (waitConditionMet(wait)) { onDone({ met: true, elapsedMs: Date.now() - startedAt }); return; }
    if (Date.now() - startedAt >= wait.timeoutMs) {
      onDone({ met: false, elapsedMs: Date.now() - startedAt });
      return;
    }
    setTimeout(poll, EXECUTOR_POLL_MS);
  };
  poll();
}

function describeTarget(element) {
  return {
    tag: String(element.tagName || '').toLowerCase(),
    dataQa: element.getAttribute('data-qa'),
    // Census-style text only (CLAUDE.md: no raw HTML ever leaves the tab).
    text: (element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 200)
  };
}

function describeMatches(params) {
  const matches = resolveTargets(params).matches;
  return {
    matchCount: matches.length,
    visible: matches.some((element) => isVisible(element))
  };
}

function clickElement(params, sendResponse) {
  const resolved = resolveTargets(params);
  if (resolved.error) { sendResponse({ ok: false, error: resolved.error, modes: resolved.modes ?? null }); return; }
  if (resolved.matches.length === 0) {
    sendResponse({ ok: false, error: 'element_not_found' });
    return;
  }
  if (resolved.matches.length > 1) {
    // Одинаковый data-qa на ВСЕХ матчах — один контрол в нескольких местах
    // страницы (hh.ru дублирует «Откликнуться» в шапке и липкой панели,
    // боевой прогон #1162): берём первый видимый, как first_locator
    // боевого пути. Распознаётся и явный dataQa, и чистый селектор вида
    // [data-qa='X'] — оба называют контрол семантически. Прочие селекторы
    // остаются ambiguous (fail-closed, никакого «кликнем первый молча»).
    let identityQa = null;
    if (typeof params.dataQa === 'string' && params.dataQa.trim() !== '') {
      identityQa = params.dataQa;
    } else if (typeof params.selector === 'string') {
      const m = params.selector.match(/^\[data-qa=['"]([^'"]+)['"]\]$/);
      if (m) identityQa = m[1];
    }
    const sameQa = identityQa !== null
      && resolved.matches.every((el) => el.getAttribute('data-qa') === identityQa);
    if (sameQa) {
      const visibleMatch = resolved.matches.find((el) => isVisible(el));
      if (!visibleMatch) {
        sendResponse({ ok: false, error: 'element_not_visible' });
        return;
      }
      resolved.matches = [visibleMatch];
    } else {
      sendResponse({ ok: false, error: 'ambiguous_target', matchCount: resolved.matches.length });
      return;
    }
  }
  const target = resolved.matches[0];
  if (!isVisible(target)) {
    sendResponse({ ok: false, error: 'element_not_visible' });
    return;
  }
  const policy = evaluateClickPolicy(target, params.allowApply === true);
  if (policy.verdict !== 'allowed') {
    sendResponse({ ok: false, error: 'policy_refused', policy });
    return;
  }
  // Declared post-click condition BEFORE the click (fail-closed): no wait
  // declaration, no click.
  const wait = resolveWait(params.waitFor);
  if (wait.error) { sendResponse({ ok: false, error: wait.error }); return; }
  const descriptor = describeTarget(target);
  target.click();
  waitForCondition(wait, (outcome) => {
    sendResponse({
      ok: true,
      result: {
        action: 'click_element',
        policy,
        target: descriptor,
        clicked: true,
        wait: { state: wait.state, timeoutMs: wait.timeoutMs, met: outcome.met, elapsedMs: outcome.elapsedMs },
        finalState: {
          url: location.href,
          targetAfter: describeMatches(params),
          // The detection observer kept running during the command; the
          // agent sees what appeared (id/type/disposition only, no text).
          overlays: listOverlays().map(({ id, type, disposition }) => ({ id, type, disposition }))
        }
      }
    });
  });
}

// #1162 (S4): text input primitive — the letter textarea of the response
// form. React-controlled fields ignore plain value assignment, so the value
// goes through the native prototype setter followed by input+change events
// (the standard React-visible path). Gates: exactly one visible match and NO
// danger anchor over the subtree (a captcha input is never filled); apply
// signals are intentionally NOT a refusal here — filling the response letter
// is this primitive's whole purpose, same authorization model as
// click_element allowApply (the scenario's explicit command). The value is
// read back and reported; a value that did not stick is ok:false, never a
// silent half-filled form.
function fillElement(params, sendResponse) {
  const resolved = resolveTargets(params);
  if (resolved.error) { sendResponse({ ok: false, error: resolved.error, modes: resolved.modes ?? null }); return; }
  if (resolved.matches.length === 0) {
    sendResponse({ ok: false, error: 'element_not_found' });
    return;
  }
  if (resolved.matches.length > 1) {
    sendResponse({ ok: false, error: 'ambiguous_target', matchCount: resolved.matches.length });
    return;
  }
  const target = resolved.matches[0];
  if (!isVisible(target)) {
    sendResponse({ ok: false, error: 'element_not_visible' });
    return;
  }
  const text = collectText(target);
  // Inputs carry their meaning in attributes, not text content (an empty
  // <input data-qa="...captcha-input"> has no text to match) — the danger
  // gate reads both, the same /captcha|не робот/ anchors as DANGEROUS_TEXT.
  const attrs = `${target.getAttribute('data-qa') || ''} ${target.getAttribute('id') || ''} ${target.getAttribute('aria-label') || ''}`;
  const dangerous = DANGEROUS_TEXT.some((re) => re.test(text)) || /captcha|не робот/i.test(attrs);
  if (dangerous) {
    sendResponse({ ok: false, error: 'policy_refused', policy: { verdict: 'refused', reason: 'dangerous', targetText: text.slice(0, 200) } });
    return;
  }
  const wanted = String(params.text ?? '');
  const proto = Object.getPrototypeOf(target);
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  if (descriptor && typeof descriptor.set === 'function') {
    descriptor.set.call(target, wanted);
  } else {
    target.value = wanted;
  }
  ['input', 'change'].forEach((type) => {
    if (typeof document.createEvent === 'function' && typeof target.dispatchEvent === 'function') {
      target.dispatchEvent(new Event(type, { bubbles: true }));
    }
  });
  const matches = target.value === wanted;
  sendResponse({
    ok: matches,
    result: {
      action: 'fill_element',
      target: describeTarget(target),
      filled: matches,
      length: target.value.length,
      finalState: { url: location.href }
    }
  });
}

function waitElement(params, sendResponse) {
  if (params.state !== 'visible' && params.state !== 'hidden') {
    sendResponse({ ok: false, error: 'state_required' });
    return;
  }
  const timeout = resolveTimeout(params.timeoutMs);
  if (timeout.error) { sendResponse({ ok: false, error: timeout.error }); return; }
  const resolved = resolveTargets(params);
  if (resolved.error) { sendResponse({ ok: false, error: resolved.error, modes: resolved.modes ?? null }); return; }
  const wait = { params, state: params.state, timeoutMs: timeout.timeoutMs };
  waitForCondition(wait, (outcome) => {
    const finalMatches = resolveTargets(params).matches;
    sendResponse({
      ok: true,
      result: {
        action: 'wait_element',
        state: wait.state,
        wait: { state: wait.state, timeoutMs: wait.timeoutMs, met: outcome.met, elapsedMs: outcome.elapsedMs },
        finalState: {
          url: location.href,
          matchCount: finalMatches.length,
          visible: finalMatches.some((element) => isVisible(element)),
          text: finalMatches[0] ? describeTarget(finalMatches[0]).text : null
        }
      }
    });
  });
}

function getPageState() {
  return {
    url: location.href,
    title: typeof document.title === 'string' ? document.title : '',
    readyState: typeof document.readyState === 'string' ? document.readyState : null
  };
}
