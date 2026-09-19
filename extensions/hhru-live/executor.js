// Stage 2 executor of issue #1160: allowlisted DOM primitives that act on
// the live hh.ru tab on behalf of the external agent. The channel is the
// loopback WebSocket bridge in background.js (protocol per issue #1159);
// this file never talks to the network itself.
//
// Three commands, dispatched from content.js's single onMessage listener:
//   click_element  {dataQa|label|selector, waitFor: {state, timeoutMs, dataQa|label|selector}}
//   wait_element   {dataQa|label|selector, state: 'visible'|'hidden', timeoutMs}
//   get_page_state {}
//
// Policy: EVERY click passes the #929 policy core (policy.js) BEFORE the
// click happens — danger anchors over the target's own subtree text and
// aria-labels, apply-flow signals, then the disposition of the overlay the
// target sits in (nearest overlay ANCESTOR; the target itself is judged by
// its content, so clicking an overlay container still scans everything
// inside it). dangerous / apply_step / ambiguous refuse WITHOUT any click;
// only a safe context clicks. This file adds no anchors and never widens
// the stage-1 gates — it reuses them verbatim.
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
function findOverlayContext(node) {
  let current = node.parentNode;
  while (current && typeof current.matches === 'function') {
    if (current !== document.documentElement && current !== document.body
      && OVERLAY_SELECTORS.some((selector) => current.matches(selector))) return current;
    current = current.parentNode;
  }
  return null;
}

// The #929 policy core applied to a click target. Same fail-closed priority
// as classifyDisposition: danger anchors outrank apply signals, both refuse.
function evaluateClickPolicy(target) {
  const text = collectText(target);
  if (DANGEROUS_TEXT.some((re) => re.test(text))) {
    return { verdict: 'refused', reason: 'dangerous', targetText: text.slice(0, 200) };
  }
  if (hasApplySignal(target, text)) {
    return { verdict: 'refused', reason: 'apply_step' };
  }
  const overlay = findOverlayContext(target);
  if (!overlay) return { verdict: 'allowed', context: 'page' };
  const info = classify(overlay);
  const disposition = classifyDisposition(overlay, info);
  if (disposition !== 'safe') {
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
    sendResponse({ ok: false, error: 'ambiguous_target', matchCount: resolved.matches.length });
    return;
  }
  const target = resolved.matches[0];
  if (!isVisible(target)) {
    sendResponse({ ok: false, error: 'element_not_visible' });
    return;
  }
  const policy = evaluateClickPolicy(target);
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
