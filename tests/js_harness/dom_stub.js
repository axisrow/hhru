// Minimal, dependency-free DOM + chrome.* stub for behaviorally executing
// extensions/hhru-live/content.js under Node's `vm` module (see
// tests/test_hhru_live_behavior.py). Implements exactly the surface content.js
// touches: Element (matches/getAttribute/setAttribute/classList/querySelectorAll/
// offsetWidth/offsetHeight/getClientRects), a documentElement-rooted tree, and a
// synchronous-batch MutationObserver whose callback semantics mirror the real
// spec closely enough for this content script (one microtask-flushed batch per
// group of synchronous mutations, delivered as {type, target, addedNodes}).
//
// No third-party dependency (jsdom/linkedom) is introduced: the project has no
// npm/package.json footprint at all, and content.js's actual DOM usage is a
// handful of primitives that are cheap and low-risk to hand-roll here, per the
// project's "maximum simplicity, do not add dependencies without justification"
// principle (see CLAUDE.md).

'use strict';

function matchesSimpleSelector(el, selector) {
  // Supports exactly the selector shapes content.js's OVERLAY_SELECTORS uses:
  // [attr="value"], [attr*="value"], [attr]
  const attrMatch = selector.match(/^\[([a-zA-Z-]+)(?:([*])?=("([^"]*)"|'([^']*)'))?\]$/);
  if (!attrMatch) throw new Error(`unsupported selector in stub: ${selector}`);
  const [, attr, op, , dq, sq] = attrMatch;
  const value = dq ?? sq;
  const actual = el.getAttribute(attr);
  if (actual === null) return false;
  if (value === undefined) return true;
  if (op === '*') return actual.includes(value);
  return actual === value;
}

class ClassList {
  constructor(el) {
    this._el = el;
  }
  add(...names) {
    const cur = new Set((this._el.getAttribute('class') || '').split(/\s+/).filter(Boolean));
    names.forEach((n) => cur.add(n));
    this._el.setAttribute('class', [...cur].join(' '));
  }
  remove(...names) {
    const cur = new Set((this._el.getAttribute('class') || '').split(/\s+/).filter(Boolean));
    names.forEach((n) => cur.delete(n));
    this._el.setAttribute('class', [...cur].join(' '));
  }
}

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this._attrs = new Map();
    this.children = [];
    this.parentNode = null;
    this.innerText = '';
    this.textContent = '';
    this._offsetWidth = 0;
    this._offsetHeight = 0;
    this._clientRects = [];
    this.classList = new ClassList(this);
  }

  get offsetWidth() {
    return this._offsetWidth;
  }
  get offsetHeight() {
    return this._offsetHeight;
  }
  getClientRects() {
    return this._clientRects;
  }

  // Test-only helper: flips the visibility signals content.js's isVisible()
  // reads (offsetWidth/offsetHeight/getClientRects().length), simulating a
  // real browser's post-layout state after a class/style toggle.
  _setVisible(visible) {
    this._offsetWidth = visible ? 100 : 0;
    this._offsetHeight = visible ? 40 : 0;
    this._clientRects = visible ? [{ width: 100, height: 40 }] : [];
  }

  getAttribute(name) {
    return this._attrs.has(name) ? this._attrs.get(name) : null;
  }

  setAttribute(name, value) {
    const root = this._ownerDocument();
    this._attrs.set(name, String(value));
    if (root) root._recordMutation({ type: 'attributes', target: this, addedNodes: [] });
  }

  matches(selector) {
    return matchesSimpleSelector(this, selector);
  }

  querySelectorAll(selectorList) {
    const selectors = selectorList.split(',').map((s) => s.trim());
    const out = [];
    const visit = (node) => {
      node.children.forEach((child) => {
        if (selectors.some((s) => matchesSimpleSelector(child, s))) out.push(child);
        visit(child);
      });
    };
    visit(this);
    return out;
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    const root = this._ownerDocument();
    if (root) root._recordMutation({ type: 'childList', target: this, addedNodes: [child] });
    return child;
  }

  _ownerDocument() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node._isDocumentRoot ? node : null;
  }
}

class DocumentElement extends Element {
  constructor() {
    super('html');
    this._isDocumentRoot = true;
    this._observers = [];
    this._pendingRecords = [];
    this._flushScheduled = false;
  }

  _recordMutation(record) {
    this._pendingRecords.push(record);
    if (this._flushScheduled) return;
    this._flushScheduled = true;
    // Real MutationObserver delivers a batch per microtask checkpoint, after
    // the current synchronous script (or task) finishes. queueMicrotask
    // mirrors that closely enough for this test harness.
    queueMicrotask(() => this._flush());
  }

  _flush() {
    const records = this._pendingRecords;
    this._pendingRecords = [];
    this._flushScheduled = false;
    if (records.length === 0) return;
    this._observers.forEach((cb) => cb(records));
  }
}

function makeChrome(sentMessages) {
  const listeners = [];
  return {
    runtime: {
      sendMessage(message) {
        sentMessages.push(message);
      },
      onMessage: {
        addListener(fn) {
          listeners.push(fn);
        },
      },
      _listeners: listeners,
    },
  };
}

function createEnvironment() {
  const documentElement = new DocumentElement();
  const document = {
    documentElement,
    querySelectorAll: (sel) => documentElement.querySelectorAll(sel),
    createElement: (tag) => new Element(tag),
  };
  const sentMessages = [];
  const chrome = makeChrome(sentMessages);

  class MutationObserver {
    constructor(callback) {
      this._callback = callback;
    }
    observe(target, _options) {
      target._observers.push((records) => this._callback(records));
    }
  }

  return {
    document,
    chrome,
    sentMessages,
    MutationObserver,
    Element,
    location: { href: 'https://hh.ru/vacancy/1' },
    flush: () => documentElement._flush(),
  };
}

module.exports = { createEnvironment, Element };
