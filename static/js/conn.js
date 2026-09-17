/* conn.js — phase 3: لاگ ارتباط tab. /api/connection-logs with device +
   event filters, auto-refresh from RATES.devices_ms, client-side clear. */

import { $, el, faNum, faDate, toast } from './ui.js';
import { apiGet } from './api.js';
import { createTable } from './table.js';
import { getDevices } from './devices.js';
import { rateTimer } from './poll.js';

let table = null;
let cleared = false;          // client-side "clear view" flag
let eventFilter = '';

function okBadge(v) {
  if (v === true) return el('span', { class: 'badge success' }, 'موفق');
  if (v === false) return el('span', { class: 'badge danger' }, 'خطا');
  return el('span', { class: 'muted' }, '—');
}

async function refresh() {
  const panel = document.getElementById('t-conn');
  if (!panel || !panel.classList.contains('active')) return;
  let j;
  try {
    j = await apiGet('/api/connection-logs?limit=1000', { timeout: 10000 });
  } catch { return; }
  const logs = j.logs || [];
  const sel = document.getElementById('cn-dev');
  const dev = sel?.value || 'all';

  const rows = logs.filter(r =>
    (dev === 'all' || r.device === dev) &&
    (!eventFilter || r.event === eventFilter));

  /* event-type filter options (derived from data) */
  const evSel = document.getElementById('cn-event');
  if (evSel) {
    const events = [...new Set(logs.map(r => r.event).filter(Boolean))].sort();
    const cur = eventFilter;
    evSel.innerHTML = '';
    evSel.append(el('option', { value: '' }, 'همهٔ رویدادها'));
    for (const ev of events) evSel.append(el('option', { value: ev }, ev));
    evSel.value = cur;
    if (evSel.value !== cur) { evSel.value = ''; eventFilter = ''; }
  }

  const sum = document.getElementById('cn-sum');
  if (sum) {
    sum.innerHTML = '';
    sum.append(el('span', {},
      `${faNum(rows.length)} رویداد از ${faNum(logs.length)} (کل)`));
    if (cleared) sum.append(el('span', { class: 'badge warning', style: 'margin-inline-start:8px' },
      'نما پاک شده — رفرش دوباره پر می‌کند'));
  }

  table?.update(cleared ? [] : rows);
}

function fillDevSelect() {
  const sel = document.getElementById('cn-dev');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '';
  sel.append(el('option', { value: 'all' }, 'همه دستگاه‌ها'));
  for (const d of getDevices()) {
    sel.append(el('option', { value: d.ip }, d.ip));
  }
  if (cur) sel.value = cur;
}

export function initConnPage() {
  const panel = document.getElementById('t-conn');
  if (!panel || panel.dataset.init) return;
  panel.dataset.init = '1';

  table = createTable({
    columns: [
      { key: 'time', label: 'زمان', render: r => el('span', { class: 'ltr' }, r.time || '—') },
      { key: 'device', label: 'دستگاه', render: r => el('span', { class: 'ltr mono' }, r.device || '—') },
      { key: 'event', label: 'رویداد' },
      { key: 'detail', label: 'جزئیات' },
      { key: 'ok', label: 'نتیجه', render: r => okBadge(r.ok) },
      { key: 'source', label: 'منبع' },
    ],
    pageSize: 50,
    emptyMsg: 'رویدادی ثبت نشده',
  });
  document.getElementById('cn-table-host')?.append(table.root);

  document.getElementById('cn-dev')?.addEventListener('change', refresh);
  document.getElementById('cn-event')?.addEventListener('change', (e) => {
    eventFilter = e.target.value; refresh();
  });
  document.getElementById('cn-refresh-btn')?.addEventListener('click', (e) => {
    cleared = false;
    const done = e.currentTarget?.blur?.bind(e.currentTarget);
    refresh().finally(done);
  });
  document.getElementById('cn-clear-btn')?.addEventListener('click', () => {
    cleared = true;
    table?.update([]);
    toast('نمایش پاک شد (فقط ظاهری — داده‌ها سرجا هستند)');
    refresh();
  });

  rateTimer(refresh, 'devices_ms');
}

window.HozorPages = window.HozorPages || {};
window.HozorPages.conn = { onShow: fillDevSelect };
