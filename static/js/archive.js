/* archive.js — phase 2: بایگانی tab. Search /api/archive (device, dates,
   name/id query), totals line with source badges. Server-side limit,
   client pagination via table.js. */

import { $, el, faNum, toast } from './ui.js';
import { apiGet, qs } from './api.js';
import { createTable } from './table.js';
import { getDevices } from './devices.js';

let table = null;

async function search() {
  const p = {
    device: document.getElementById('a-dev')?.value || 'all',
    limit: 20000,
  };
  const from = document.getElementById('a-from')?.value;
  const to = document.getElementById('a-to')?.value;
  const q = document.getElementById('a-q')?.value.trim();
  if (from) p.from = from;
  if (to) p.to = to;
  if (q) p.q = q;

  const sum = document.getElementById('a-sum');
  if (sum) sum.textContent = 'در حال جستجو…';
  try {
    const j = await apiGet('/api/archive?' + qs(p), { timeout: 120000 });
    const recs = j.records || [];
    table?.update(recs);
    if (sum) {
      sum.innerHTML = '';
      sum.append(el('span', {}, `${faNum(j.total ?? recs.length)} رکورد در بایگانی — ${faNum(recs.length)} نمایش داده می‌شود `));
      const srcs = {};
      for (const r of recs) srcs[r.source || '?'] = (srcs[r.source || '?'] || 0) + 1;
      for (const [s, n] of Object.entries(srcs)) {
        sum.append(el('span', { class: 'badge' + (s === 'push' ? ' success' : s === 'live' ? ' accent' : '') },
          `${s}: ${faNum(n)}`), ' ');
      }
    }
  } catch (e) {
    if (sum) sum.textContent = '';
    toast(e.message, true);
  }
}

function fillDevSelect() {
  const sel = document.getElementById('a-dev');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '';
  sel.append(el('option', { value: 'all' }, 'همه دستگاه‌ها'));
  for (const d of getDevices()) {
    sel.append(el('option', { value: d.ip }, `${d.ip} ${d.label || d.model || ''}`.trim()));
  }
  if (cur) sel.value = cur;
}

export function initArchivePage() {
  const host = document.getElementById('arch-table-host');
  if (!host || host.dataset.init) return;
  host.dataset.init = '1';

  table = createTable({
    columns: [
      { key: 'label', label: 'دستگاه' },
      { key: 'device', label: 'IP', render: r => el('span', { class: 'ltr mono' }, r.device) },
      { key: 'user_id', label: 'شناسه', render: r => el('span', { class: 'ltr mono' }, r.user_id) },
      { key: 'name', label: 'نام' },
      { key: 'timestamp', label: 'زمان', render: r => el('span', { class: 'ltr' }, r.timestamp) },
      { key: 'punch_label', label: 'نوع' },
      { key: 'source', label: 'منبع', render: r => el('span', {
        class: 'badge' + (r.source === 'push' ? ' success' : r.source === 'live' ? ' accent' : '') },
        r.source || '—') },
    ],
    pageSize: 50,
    emptyMsg: 'بایگانی خالی است',
  });
  host.append(table.root);

  document.getElementById('arch-search-btn')?.addEventListener('click', search);
  document.getElementById('a-q')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') search();
  });
}

window.HozorPages = window.HozorPages || {};
window.HozorPages.archive = { onShow: fillDevSelect };
