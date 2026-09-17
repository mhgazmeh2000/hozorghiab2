/* logs.js — phase 2: attendance inquiry tab. Ported 1:1 from legacy:
   live fetch (/api/logs), new-only fetch (/api/logs/new), archive view
   (/api/archive), dynamic progress polling (/api/fetch-progress),
   CSV/Excel export. Same query params. Progress rates from poll.js. */

import { $, el, faNum, toast, announce, busy } from './ui.js';
import { apiGet, qs } from './api.js';
import { createTable } from './table.js';
import { getDevices } from './devices.js';
import { RATES } from './poll.js';

let table = null;
let lastQ = null;

/* ---------- dynamic progress polling (from legacy progTick) ---------- */
let P_TIMER = null, P_ACTIVE = false, PROG_LAST = {};

function progDelay() {
  for (const k in PROG_LAST) {
    const s = PROG_LAST[k];
    if (s.stage && s.stage !== 'done' && s.stage !== 'error') return RATES.progress_active_ms;
  }
  return RATES.progress_idle_ms;
}

async function pollProg() {
  try {
    const j = await apiGet('/api/fetch-progress', { timeout: 8000 });
    PROG_LAST = j.progress || {};
    const p = j.progress || {};
    const selected = document.getElementById('q-dev')?.value || 'all';
    const lines = [];
    for (const ip in p) {
      const s = p[ip];
      if (selected !== 'all' && ip !== selected) continue;
      const mark = s.ok === true ? '✔' : (s.ok === false ? '✘' : '…');
      lines.push(`${mark} ${ip} — ${s.stage} ${s.note || ''} (${s.updated || ''})`);
    }
    const box = document.getElementById('fetch-prog');
    if (box) box.textContent = lines.join('\n');
  } catch {}
}

function progTick() {
  pollProg();
  if (!P_ACTIVE) return;
  P_TIMER = setTimeout(progTick, progDelay());
}
function startProgPolling() { P_ACTIVE = true; if (!P_TIMER) progTick(); }
function stopProgPolling() {
  P_ACTIVE = false;
  if (P_TIMER) { clearTimeout(P_TIMER); P_TIMER = null; }
}

/* ---------- query builders (same params as legacy) ---------- */
function qLive() {
  const p = { device: document.getElementById('q-dev')?.value || 'all' };
  const from = document.getElementById('q-from')?.value;
  const to = document.getElementById('q-to')?.value;
  const timeout = document.getElementById('q-timeout')?.value.trim();
  if (from) p.from = from;
  if (to) p.to = to;
  if (timeout) p.timeout = timeout;
  return qs(p);
}
function qArchiveForLogs() {
  const p = { device: document.getElementById('q-dev')?.value || 'all', limit: 20000 };
  const from = document.getElementById('q-from')?.value;
  const to = document.getElementById('q-to')?.value;
  if (from) p.from = from;
  if (to) p.to = to;
  return qs(p);
}

/* ---------- fetch modes ---------- */
async function fetchLogs() {
  const q = qLive(); lastQ = q;
  const sum = document.getElementById('log-sum');
  const prog = document.getElementById('fetch-prog');
  if (sum) sum.textContent = 'در حال دریافت…';
  if (prog) prog.textContent = '';
  startProgPolling();
  try {
    const j = await apiGet('/api/logs?' + q, { timeout: 600000 });
    renderResult(j, `${j.total} رکورد تردد`);
  } catch (e) {
    if (sum) sum.textContent = '';
    toast(e.message, true);
  } finally {
    stopProgPolling();
    setTimeout(pollProg, 300);
  }
}

async function fetchArchivedLogs() {
  const q = qArchiveForLogs(); lastQ = q;
  const sum = document.getElementById('log-sum');
  if (sum) sum.textContent = 'در حال خواندن بایگانی…';
  try {
    const j = await apiGet('/api/archive?' + q, { timeout: 120000 });
    renderResult(j, `${j.records.length} رکورد از بایگانی محلی (بدون اتصال به دستگاه)`);
  } catch (e) {
    if (sum) sum.textContent = '';
    toast(e.message, true);
  }
}

async function fetchNewLogs() {
  const q = qLive(); lastQ = q;
  const sum = document.getElementById('log-sum');
  const prog = document.getElementById('fetch-prog');
  if (sum) sum.textContent = 'در حال دریافت زنده و مقایسه با دیتابیس…';
  if (prog) prog.textContent = 'دریافت کامل دستگاه ممکن است چند دقیقه طول بکشد؛ بعد فقط رکوردهای جدید نمایش داده می‌شود.';
  startProgPolling();
  try {
    const j = await apiGet('/api/logs/new?' + q, { timeout: 600000 });
    renderResult(j);
    if (j.pending) {
      if (sum) sum.textContent = 'درخواست دریافت جدید ثبت شد؛ منتظر polling دستگاه';
      setTimeout(fetchArchivedLogs, 7000);
    } else {
      const errs = Object.entries(j.errors || {});
      if (sum) sum.textContent = `${j.total} رکورد جدید که در دیتابیس موجود نیست` +
        (errs.length ? ` — خطا: ${errs.map(([k, v]) => k + ': ' + v).join(' | ')}` : '');
    }
  } catch (e) {
    if (sum) sum.textContent = '';
    toast(e.message, true);
  } finally {
    stopProgPolling();
    setTimeout(pollProg, 300);
  }
}

function renderResult(j, msg) {
  const sum = document.getElementById('log-sum');
  const errs = Object.entries(j.errors || {});
  if (sum && msg !== undefined) sum.textContent = msg;
  else if (sum) sum.textContent = `${j.total} رکورد تردد` +
    (errs.length ? ` — خطا: ${errs.map(([k, v]) => k + ': ' + v).join(' | ')}` : '');
  table?.update(j.records || []);
  announce(`${(j.records || []).length} رکورد نمایش داده شد`);
}

/* ---------- export ---------- */
function exportData(kind) {
  if (!lastQ) { toast('اول استعلام بگیرید', true); return; }
  window.open('/api/export.' + kind + '?' + lastQ);
  toast('خروجی در حال دانلود است');
}

/* ---------- device selects ---------- */
function fillDevSelect() {
  const sel = document.getElementById('q-dev');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '';
  sel.append(el('option', { value: 'all' }, 'همه دستگاه‌ها'));
  for (const d of getDevices()) {
    sel.append(el('option', { value: d.ip }, `${d.ip} ${d.label || d.model || ''}`.trim()));
  }
  if (cur) sel.value = cur;
}

/* ---------- init ---------- */
export function initLogsPage() {
  const host = document.getElementById('log-table-host');
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
      { key: 'status', label: 'وضعیت', num: true },
    ],
    pageSize: 50,
    emptyMsg: 'رکوردی نیست — دریافت بزنید',
  });
  host.append(table.root);

  document.getElementById('fetch-logs-btn')?.addEventListener('click', (e) => {
    const done = busy(e.currentTarget); fetchLogs().finally(done);
  });
  document.getElementById('fetch-new-btn')?.addEventListener('click', (e) => {
    const done = busy(e.currentTarget); fetchNewLogs().finally(done);
  });
  document.getElementById('logs-arch-btn')?.addEventListener('click', fetchArchivedLogs);
  document.getElementById('export-csv-btn')?.addEventListener('click', () => exportData('csv'));
  document.getElementById('export-xlsx-btn')?.addEventListener('click', () => exportData('xlsx'));

  /* default dates: last 6 days (same as legacy) */
  const from = document.getElementById('q-from');
  const to = document.getElementById('q-to');
  if (from) from.value = new Date(Date.now() - 6 * 864e5).toISOString().slice(0, 10);
  if (to) to.value = new Date().toISOString().slice(0, 10);
}

/* hook for app.js show() */
window.HozorPages = window.HozorPages || {};
window.HozorPages.logs = {
  onShow() {
    fillDevSelect();
  },
};
