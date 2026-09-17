/* ui.js — vanilla UI toolkit: toast, modal/confirm (focus trap), tabs
   (keyboard), dropdown, debounce, Persian numerals, theme, skeleton,
   empty state. No dependencies. */

/* ---------- tiny DOM helpers ---------- */
export const $  = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;           // trusted internal markup only
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const icon = (name, cls = '') =>
  el('svg', { class: cls, 'aria-hidden': 'true', html:
    `<use href="/static/icons/sprite.svg#${name}"></use>` });

/* ---------- Persian numerals & dates ---------- */
const faNumFmt = new Intl.NumberFormat('fa-IR');
export const faNum = (v) => faNumFmt.format(Number(v) || 0);
export const faDate = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleString('fa-IR', { year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit' });
};

/* ---------- debounce ---------- */
export function debounce(fn, ms = 300) {
  let t;
  return function (...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

/* ---------- toast ---------- */
export function toast(msg, isError = false, ms = 3200) {
  const host = $('#toasts') || document.body;
  const t = el('div', { class: 'toast' + (isError ? ' err' : ''), role: 'status' },
    el('span', {}, msg));
  host.append(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 300);
  }, ms);
  return t;
}

/* live region for screen readers */
export function announce(msg) {
  let live = $('#live-status');
  if (!live) {
    live = el('span', { id: 'live-status', class: 'live-status', 'aria-live': 'polite' });
    document.body.append(live);
  }
  live.textContent = '';
  requestAnimationFrame(() => { live.textContent = msg; });
}

/* ---------- buttons: busy state ---------- */
export function busy(btn) {
  if (!btn) return () => {};
  btn.dataset.label = btn.innerHTML;
  btn.classList.add('busy');
  btn.disabled = true;
  btn.insertAdjacentHTML('beforeend', ' <span class="spinner" aria-hidden="true"></span>');
  return () => {
    btn.classList.remove('busy');
    btn.disabled = false;
    btn.innerHTML = btn.dataset.label;
  };
}

/* ---------- modal (focus trap + ESC) ---------- */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

export function modal({ title, body, footer, onClose } = {}) {
  const dlg = document.createElement('dialog');
  dlg.className = 'modal';
  dlg.setAttribute('aria-modal', 'true');
  const close = () => { dlg.close(); };

  if (title) {
    dlg.append(el('header', {},
      el('h2', { style: 'margin:0;font-size:1em' }, title),
      el('button', { class: 'btn icon ghost', 'aria-label': 'بستن', onclick: close }, icon('i-x'))));
  }
  const bodyDiv = el('div', { class: 'modal-body' });
  if (typeof body === 'string') bodyDiv.innerHTML = body; else if (body) bodyDiv.append(body);
  dlg.append(bodyDiv);
  if (footer) dlg.append(el('footer', {}, ...footer));

  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); onClose && onClose(); });
  dlg.addEventListener('close', () => { onClose && onClose(); dlg.remove(); });

  /* focus trap */
  dlg.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const f = $$(FOCUSABLE, dlg).filter(n => n.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  });

  document.body.append(dlg);
  dlg.showModal();
  ($('input, select, textarea, button:not(.ghost)', dlg) || dlg).focus?.();
  return { dlg, close };
}

/** confirm dialog — resolves true/false. Used for destructive actions. */
export function confirmDialog(message, { danger = true, okText = 'تأیید', cancelText = 'انصراف' } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    modal({
      title: 'تأیید عملیات',
      body: el('p', { style: 'margin:0' }, message),
      footer: [
        el('button', { class: 'btn', onclick: () => { m.close(); done(false); } }, cancelText),
        el('button', { class: 'btn ' + (danger ? 'danger' : 'primary'),
          onclick: () => { m.close(); done(true); } }, okText),
      ],
      onClose: () => done(false),
    }).then ? null : null;
    // modal() returns object synchronously; capture after creation:
    // (we re-query the last dialog in DOM)
    const dlgs = $$('dialog.modal');
    const m = { close: () => dlgs[dlgs.length - 1]?.close() };
  });
}

/* ---------- tabs (keyboard accessible) ---------- */
export function initTabs(navEl, { onChange } = {}) {
  const tabs = $$('button[role="tab"]', navEl);
  function select(tab, focus = false) {
    tabs.forEach(t => t.setAttribute('aria-selected', String(t === tab)));
    if (focus) tab.focus();
    onChange && onChange(tab.dataset.route);
  }
  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', (e) => {
      let j = null;
      if (e.key === 'ArrowLeft')  j = (i + 1) % tabs.length;   // RTL: left = next
      if (e.key === 'ArrowRight') j = (i - 1 + tabs.length) % tabs.length;
      if (e.key === 'Home') j = 0;
      if (e.key === 'End')  j = tabs.length - 1;
      if (j !== null) { e.preventDefault(); select(tabs[j], true); tabs[j].click(); }
    });
  });
  return { select };
}

/* ---------- dropdown (click-outside close) ---------- */
export function initDropdown(btn, menu) {
  const close = () => { menu.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = menu.classList.toggle('open');
    btn.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', (e) => {
    if (!menu.contains(e.target) && e.target !== btn) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  return close;
}

/* ---------- theme ---------- */
export function initTheme() {
  const saved = localStorage.getItem('hozor-theme');   // 'dark' | 'light' | null
  if (saved) document.documentElement.dataset.theme = saved;
  return {
    toggle() {
      const root = document.documentElement;
      const cur = root.dataset.theme ||
        (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      const next = cur === 'light' ? 'dark' : 'light';
      root.dataset.theme = next;
      localStorage.setItem('hozor-theme', next);
      return next;
    },
    current() {
      return document.documentElement.dataset.theme ||
        (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    },
  };
}

/* ---------- skeleton / empty state ---------- */
export function skeletonRows(container, n = 4) {
  container.innerHTML = '';
  const box = el('div', { style: 'padding:var(--space-3)' },
    el('div', { class: 'skeleton title' }),
    ...Array.from({ length: n }, () => el('div', { class: 'skeleton row' })));
  container.append(box);
}
export function skeletonKpis(container, n = 3) {
  container.innerHTML = '';
  container.append(...Array.from({ length: n }, (_, i) =>
    el('div', { class: 'kpi' }, el('div', { class: 'skeleton', style: 'height:30px;width:60%;margin:6px auto' }),
      el('div', { class: 'skeleton text', style: 'width:40%;margin:0 auto' }))));
}

export function emptyState(container, msg = 'موردی یافت نشد') {
  container.innerHTML = '';
  container.append(el('div', { class: 'empty-state' },
    icon('i-empty'), el('p', {}, msg)));
}

/* ---------- misc ---------- */
export function updateClock(node) {
  const tick = () => { node.textContent = new Date().toLocaleString('fa-IR'); };
  tick();
  return setInterval(tick, 1000);
}

/** mark table wrappers that truly overflow horizontally (edge fade) */
export function markOverflows() {
  $$('.table-wrap, .twrap').forEach(w => {
    w.classList.toggle('has-hscroll', w.scrollWidth > w.clientWidth + 4);
  });
}
