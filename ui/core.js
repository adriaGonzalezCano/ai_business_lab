/* Core of the lab UI: DOM helpers, API client, sign-in, router, room rail, spend meter, toasts, dialogs.
 * Rules for every view: build DOM with Lab.h (text goes in as text, never as HTML); no inline styles or scripts. */
(() => {
  'use strict';
  const Lab = (window.Lab = { routes: [], cleanups: [], currency: 'EUR', summary: null });
  const $ = (sel, root = document) => root.querySelector(sel);
  Lab.$ = $;

  // ---------------------------------------------------------------- DOM helpers
  const BOOL_PROPS = new Set(['checked', 'disabled', 'selected', 'hidden', 'open', 'required', 'readOnly', 'multiple']);
  const VALUE_PROPS = new Set(['value', 'id', 'name', 'type', 'min', 'max', 'step', 'rows', 'placeholder', 'title', 'href']);

  function applyProps(el, props) {
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.setAttribute('class', Array.isArray(v) ? v.filter(Boolean).join(' ') : v);
      else if (k === 'for') el.setAttribute('for', v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (BOOL_PROPS.has(k)) el[k] = Boolean(v);
      else if (VALUE_PROPS.has(k) && !(el instanceof SVGElement)) el[k] = v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  function appendKids(el, kids) {
    for (const kid of kids) {
      if (kid == null || kid === false) continue;
      if (Array.isArray(kid)) appendKids(el, kid);
      else if (kid instanceof Node) el.appendChild(kid);
      else el.appendChild(document.createTextNode(String(kid)));
    }
  }
  Lab.h = (tag, props, ...kids) => {
    const el = document.createElement(tag);
    applyProps(el, props);
    appendKids(el, kids);
    return el;
  };
  Lab.svg = (tag, props, ...kids) => {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    applyProps(el, props);
    appendKids(el, kids);
    return el;
  };
  const h = Lab.h;

  // ---------------------------------------------------------------- formatting
  Lab.eur = (n, digits = 2) => {
    if (n == null || Number.isNaN(Number(n))) return 'n/a';
    return new Intl.NumberFormat('en-GB', { style: 'currency', currency: Lab.currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
  };
  Lab.num = (n, digits = 0) => (n == null || Number.isNaN(Number(n)) ? 'n/a' : new Intl.NumberFormat('en-GB', { maximumFractionDigits: digits }).format(n));
  Lab.pct = (n, digits = 0) => (n == null || Number.isNaN(Number(n)) ? 'n/a' : `${Lab.num(n, digits)}%`);
  Lab.label = (s) => String(s ?? '').replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
  Lab.when = (iso) => {
    if (!iso) return h('span', { class: 'muted' }, 'never');
    const d = new Date(iso);
    const secs = Math.round((d.getTime() - Date.now()) / 1000);
    const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
    const units = [['day', 86400], ['hour', 3600], ['minute', 60]];
    let text = 'just now';
    for (const [unit, size] of units) {
      if (Math.abs(secs) >= size) { text = rtf.format(Math.round(secs / size), unit); break; }
    }
    return h('time', { datetime: iso, title: d.toLocaleString('en-GB') }, text);
  };
  Lab.badge = (status) => h('span', { class: `badge s-${status}` }, Lab.label(status));
  Lab.syntheticMark = (on = true) => on
    ? h('span', { class: 'synthetic', title: 'Produced by the built-in mock provider. It follows fixed formulas and is not real analysis.' }, 'synthetic')
    : null;

  // ---------------------------------------------------------------- storage (can be unavailable)
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode: stay signed in for this page only */ } },
    del(k) { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
  };
  let token = store.get('lab_token') || '';

  // ---------------------------------------------------------------- API client
  class ApiError extends Error {
    constructor(err, status) {
      super((err && err.message) || `Request failed (HTTP ${status})`);
      this.type = (err && err.type) || 'error';
      this.status = status;
    }
  }
  Lab.ApiError = ApiError;
  Lab.api = async (method, path, body) => {
    let res;
    try {
      res = await fetch(`/api${path}`, {
        method,
        headers: { Authorization: `Bearer ${token}`, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch {
      throw new ApiError({ type: 'network', message: 'Cannot reach the lab server. Check that it is still running.' }, 0);
    }
    let data = null;
    try { data = await res.json(); } catch { /* empty or non-JSON body */ }
    if (res.status === 401 && token) { signOut('Your token was not accepted. Sign in again.'); }
    if (!res.ok) throw new ApiError(data && data.error, res.status);
    return data;
  };
  Lab.get = (path) => Lab.api('GET', path);
  Lab.post = (path, body = {}) => Lab.api('POST', path, body);
  Lab.put = (path, body) => Lab.api('PUT', path, body);
  Lab.patch = (path, body) => Lab.api('PATCH', path, body);

  // ---------------------------------------------------------------- toasts, dialog, actions
  Lab.toast = (message, kind = 'ok') => {
    const t = h('div', { class: `toast ${kind}` }, message);
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), kind === 'error' ? 9000 : 4500);
  };
  Lab.fail = (e) => Lab.toast(e instanceof Error ? e.message : String(e), 'error');

  /** Wrap a button handler: disables the button while it runs, reports errors, and toasts success. */
  Lab.act = (button, fn, okMessage) => async (ev) => {
    if (ev && ev.preventDefault) ev.preventDefault();
    button.disabled = true;
    try {
      const out = await fn();
      if (okMessage) Lab.toast(okMessage);
      return out;
    } catch (e) {
      Lab.fail(e);
    } finally {
      button.disabled = false;
    }
  };

  Lab.dialog = (title, content, actions = []) => {
    const dlg = $('#dialog');
    const close = () => { if (dlg.open) dlg.close(); };
    dlg.replaceChildren(
      h('h2', null, title),
      content,
      h('div', { class: 'row' }, actions, h('button', { type: 'button', onclick: close }, 'Close')),
    );
    if (!dlg.open) dlg.showModal();
    return close;
  };
  Lab.closeDialog = () => { const d = $('#dialog'); if (d.open) d.close(); };

  // ---------------------------------------------------------------- polling (stops when the view changes)
  Lab.poll = (fn, ms) => {
    const id = setInterval(() => { if (!document.hidden) fn().catch(() => {}); }, ms);
    Lab.cleanups.push(() => clearInterval(id));
  };
  const runCleanups = () => { while (Lab.cleanups.length) { try { Lab.cleanups.pop()(); } catch { /* ignore */ } } };

  // ---------------------------------------------------------------- rooms and router
  Lab.rooms = [
    { id: 'overview', label: 'Lab floor', path: '/', match: /^\/$/ },
    { id: 'research', label: 'Market research', path: '/research', match: /^\/research/ },
    { id: 'ideas', label: 'Idea generation', path: '/ideas', match: /^\/ideas/ },
    { id: 'committee', label: 'Evaluation and consensus', path: '/committee', match: /^\/committee/ },
    { id: 'incubation', label: 'Business incubation', locked: 'Planned for phase 2. Not available yet.' },
    { id: 'businesses', label: 'Active businesses', locked: 'Planned for phase 2. Not available yet.' },
    { id: 'analytics', label: 'Analytics and optimization', path: '/analytics', match: /^\/analytics/ },
    { id: 'control', label: 'Owner control room', path: '/control', match: /^\/control/ },
  ];

  /** Register a view. handler(match) returns a Node, an array of Nodes, or a promise of either. */
  Lab.route = (pattern, handler) => Lab.routes.push({ pattern, handler });

  function buildRail() {
    const list = h('ul');
    for (const room of Lab.rooms) {
      list.appendChild(h('li', null, room.locked
        ? h('div', { class: 'locked' }, room.label, h('span', { class: 'sub' }, room.locked))
        : h('a', { href: `#${room.path}`, dataset: { room: room.id } }, room.label)));
    }
    $('#rail').replaceChildren(list);
  }
  function markRail(path) {
    for (const a of document.querySelectorAll('#rail a')) {
      const room = Lab.rooms.find((r) => r.id === a.dataset.room);
      if (room && room.match.test(path)) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
    $('#rail').classList.remove('open');
    $('#nav-toggle').setAttribute('aria-expanded', 'false');
  }

  const notBuilt = (path) => h('div', { class: 'empty' },
    h('h3', null, 'This page is not built yet'),
    h('p', null, `Nothing is registered for ${path}. Use the room list to go back.`),
    h('a', { class: 'button', href: '#/' }, 'Go to the lab floor'));

  const errorBlock = (e, retry) => h('div', { class: 'callout', role: 'alert' },
    h('p', null, h('strong', null, 'This page could not load. '), e && e.message ? e.message : String(e)),
    h('button', { type: 'button', onclick: retry }, 'Try again'));

  let renderSeq = 0;
  async function render() {
    const seq = ++renderSeq;
    runCleanups();
    const path = (location.hash || '#/').slice(1) || '/';
    const view = $('#view');
    markRail(path);
    const route = Lab.routes.find((r) => r.pattern.test(path));
    if (!route) { view.replaceChildren(notBuilt(path)); document.title = 'Not built yet | AI Business Lab'; return; }
    view.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
    try {
      const out = await route.handler(path.match(route.pattern));
      if (seq !== renderSeq) return;
      view.replaceChildren(...[].concat(out));
      const heading = $('h1', view);
      document.title = `${heading ? heading.textContent : 'AI Business Lab'} | AI Business Lab`;
      view.focus({ preventScroll: true });
    } catch (e) {
      if (seq !== renderSeq) return;
      view.replaceChildren(errorBlock(e, render));
    }
  }
  Lab.render = render;

  /** Page header used by every view: one h1 and an optional one-line purpose. */
  Lab.header = (title, purpose, ...actions) => h('header', null,
    h('div', { class: 'row spread' }, h('h1', null, title), actions.length ? h('div', { class: 'row' }, actions) : null),
    purpose ? h('p', null, purpose) : null);

  Lab.empty = (title, text, ...actions) => h('div', { class: 'empty' }, h('h3', null, title), h('p', null, text), actions.length ? h('div', { class: 'row' }, actions) : null);

  // ---------------------------------------------------------------- spend meter (top bar)
  Lab.refreshMeter = async () => {
    const s = await Lab.get('/costs/summary');
    Lab.summary = s;
    Lab.currency = s.currency || 'EUR';
    const pct = s.limit > 0 ? Math.min(100, (s.spent_total / s.limit) * 100) : 0;
    const state = s.new_work_blocked ? 'stop' : (s.pct_used != null && s.pct_used >= s.warn_pct ? 'warn' : '');
    const words = s.new_work_blocked ? 'new AI work is blocked' : (state === 'warn' ? 'warning threshold passed' : 'within budget');
    const text = `AI spend this month: ${Lab.eur(s.spent_total)} of ${Lab.eur(s.limit, 0)}, ${words}`;
    const fill = h('div', { class: ['fill', state] });
    fill.style.setProperty('--pct', pct.toFixed(1));
    const tick = h('div', { class: 'tick' });
    tick.style.setProperty('--at', String(s.warn_pct));
    $('#meter-slot').replaceChildren(h('a', { class: 'meter', href: '#/analytics', 'aria-label': text, title: text },
      h('span', null, `AI spend ${Lab.eur(s.spent_total)} of ${Lab.eur(s.limit, 0)}`),
      h('div', { class: 'bar', 'aria-hidden': 'true' }, fill, tick)));
  };

  // ---------------------------------------------------------------- sign-in / sign-out / boot
  function showLogin(message) {
    $('#shell').hidden = true;
    $('#login').hidden = false;
    const err = $('#login-error');
    err.hidden = !message;
    err.textContent = message || '';
    $('#token-input').value = '';
    $('#token-input').focus();
  }
  function signOut(message) {
    token = '';
    store.del('lab_token');
    runCleanups();
    if (Lab.meterTimer) clearInterval(Lab.meterTimer);
    showLogin(message);
  }
  Lab.signOut = signOut;

  async function enter() {
    $('#login').hidden = true;
    $('#shell').hidden = false;
    buildRail();
    Lab.refreshMeter().catch(() => {});
    if (Lab.meterTimer) clearInterval(Lab.meterTimer);
    Lab.meterTimer = setInterval(() => { if (!document.hidden && token) Lab.refreshMeter().catch(() => {}); }, 20000);
    await render();
  }

  Lab.boot = async () => {
    $('#login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const candidate = $('#token-input').value.trim();
      if (!candidate) return;
      const btn = $('#login-form button[type="submit"]');
      btn.disabled = true;
      token = candidate;
      try {
        await Lab.get('/costs/summary');
        store.set('lab_token', candidate);
        await enter();
      } catch (e) {
        token = '';
        const msg = e.status === 401 ? 'That token was not accepted. Check data/owner_token or your LAB_OWNER_TOKEN setting.' : e.message;
        showLogin(msg);
      } finally {
        btn.disabled = false;
      }
    });
    $('#logout').addEventListener('click', () => signOut('You are signed out.'));
    $('#nav-toggle').addEventListener('click', () => {
      const open = $('#rail').classList.toggle('open');
      $('#nav-toggle').setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') markRail((location.hash || '#/').slice(1) || '/'); });
    window.addEventListener('hashchange', () => { if (token) render(); });

    if (!token) return showLogin('');
    try {
      await Lab.get('/costs/summary');
      await enter();
    } catch {
      showLogin('');
    }
  };
})();
