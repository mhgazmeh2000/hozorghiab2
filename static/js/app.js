/* app.js — entry point: hash routing (#dash, #devs, …) with back/forward
   support, theme toggle, clock, background schedulers (rates from
   poll.js / /api/settings). All 9 tabs wired. */

import { $, $$, initTabs, initTheme, updateClock,
         toast, markOverflows } from './ui.js';
import { loadDevices, checkAllDevices } from './devices.js';
import './placeholders.js';
import { initUsersPage } from './users.js';
import { initLogsPage } from './logs.js';
import { initArchivePage } from './archive.js';
import { initSyncPage } from './sync.js';
import { initConnPage } from './conn.js';
import { initScanPage } from './scan.js';
import { initSettingsPage } from './settings.js';
import { loadRates, rateTimer } from './poll.js';

/* ---------- tabs & routing ---------- */
const ROUTES = ['dash', 'devs', 'users', 'logs', 'arch', 'sync', 'conn', 'scan', 'settings'];

function currentRoute() {
  const h = location.hash.replace('#', '');
  return ROUTES.includes(h) ? h : 'dash';
}

function show(route) {
  for (const r of ROUTES) {
    const panel = $('#t-' + r);
    if (panel) panel.classList.toggle('active', r === route);
  }
  $$('.nav-tabs button').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.route === route)));
  document.body.classList.remove('nav-open');
  if (route === 'devs') loadDevices();
  if (route === 'users') window.HozorPages?.users?.onShow?.();
  if (route === 'logs')  window.HozorPages?.logs?.onShow?.();
  if (route === 'arch')  window.HozorPages?.archive?.onShow?.();
  if (route === 'sync')  window.HozorPages?.sync?.onShow?.();
  if (route === 'conn')  window.HozorPages?.conn?.onShow?.();
  if (route === 'scan')  window.HozorPages?.scan?.onShow?.();
}

window.addEventListener('hashchange', () => show(currentRoute()));

/* ---------- boot ---------- */
document.addEventListener('DOMContentLoaded', async () => {
  /* theme */
  const theme = initTheme();
  $('#theme-btn')?.addEventListener('click', () => {
    const next = theme.toggle();
    toast(next === 'light' ? 'تم روشن فعال شد' : 'تم تیره فعال شد');
    $('#theme-btn').innerHTML =
      `<svg aria-hidden="true" width="16" height="16"><use href="/static/icons/sprite.svg#${next === 'light' ? 'i-moon' : 'i-sun'}"></use></svg>`;
  });
  // set initial icon
  const tbtn = $('#theme-btn');
  if (tbtn) tbtn.innerHTML =
    `<svg aria-hidden="true" width="16" height="16"><use href="/static/icons/sprite.svg#${theme.current() === 'light' ? 'i-moon' : 'i-sun'}"></use></svg>`;

  /* clock */
  const clock = $('#clock');
  if (clock) updateClock(clock);

  /* nav */
  const nav = $('.nav-tabs');
  initTabs(nav, { onChange: (route) => { location.hash = route; } });

  /* hamburger */
  const navBtn = $('#nav-btn');
  if (navBtn) navBtn.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    navBtn.setAttribute('aria-expanded', String(open));
  });

  /* global buttons */
  $('#reload-btn')?.addEventListener('click', () => loadDevices(true));
  $('#checkall-btn')?.addEventListener('click', (e) => checkAllDevices(e.currentTarget));

  /* all pages */
  initDevicesPage();
  initUsersPage();
  initLogsPage();
  initArchivePage();
  initSyncPage();
  initConnPage();
  initScanPage();
  initSettingsPage();

  /* background schedulers — rates from poll.js (fed by /api/settings) */
  await loadRates();
  startSchedulers();

  /* initial route */
  show(currentRoute());
  markOverflows();
  setInterval(markOverflows, 1500);
  window.addEventListener('resize', markOverflows);
});

import { initDevicesPage } from './devices.js';

/* ---------- background schedulers ---------- */
function startSchedulers() {
  rateTimer(() => { if (!document.hidden) return loadDevices(); }, 'devices_ms');
}

/* unhandled errors -> toast (operator visibility) */
window.addEventListener('unhandledrejection', (e) => {
  const msg = e?.reason?.message;
  if (msg) toast(msg, true);
});
