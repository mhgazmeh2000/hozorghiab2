/* sync.js — phase 3: همگام‌سازی tab. Ported 1:1 from legacy:
   auto-sync state/toggle (/api/sync GET, /api/sync/settings POST),
   manual sync all/one (/api/sync POST), stop (/api/sync/stop),
   per-device results table, live progress (/api/fetch-progress),
   ADMS panel (/api/adms) with queue-query buttons. */

import { $, el, faNum, faDate, toast, announce, busy } from './ui.js';
import { apiGet, apiPost } from './api.js';
import { createTable } from './table.js';
import { getDevices } from './devices.js';
import { RATES, rateTimer } from './poll.js';

let table = null;
let lastAuto = null;          // {enabled, interval} last seen
let stopBtnBound = false;

/* ---------- progress panel (same shape as logs tab) ---------- */
let PROG_LAST = {};

async function pollProgress() {
  try {
    const j = await apiGet('/api/fetch-progress', { timeout: 8000 });
    PROG_LAST = j.progress || {};
    const lines = [];
    for (const ip in PROG_LAST) {
      const s = PROG_LAST[ip];
      const mark = s.ok === true ? '✔' : (s.ok === false ? '✘' : '…');
      lines.push(`${mark} ${ip} — ${s.stage} ${s.note || ''} (${s.updated || ''})`);
    }
    const box = document.getElementById('sy-prog');
    if (box) box.textContent = lines.join('\n');
    const bar = document.getElementById('sy-prog-bar');
    if (bar) bar.style.width = '100%';          // indeterminate-active style
  } catch {}
}

/* ---------- main poll ---------- */
async function pollSync() {
  const panel = document.getElementById('t-sync');
  if (!panel || !panel.classList.contains('active')) return;
  let j;
  try { j = await apiGet('/api/sync', { timeout: 8000 }); } catch { return; }

  /* auto state */
  const auto = j.auto || {};
  const st = document.getElementById('sy-auto-state');
  if (st) {
    st.innerHTML = '';
    st.append(el('span', { class: 'status-dot ' + (auto.enabled ? 'on' : 'off') }));
    st.append(el('span', {},
      auto.enabled
        ? `روشن — هر ${faNum(Math.round((auto.interval || 900) / 60))} دقیقه`
        : 'خاموش'));
  }
  const interval = document.getElementById('sy-interval');
  if (interval && document.activeElement !== interval &&
      lastAuto && (auto.interval !== lastAuto.interval)) {
    interval.value = Math.round((auto.interval || 900) / 60);
  }
  const dbt = document.getElementById('sy-db-total');
  if (dbt && j.db_total != null) dbt.textContent = faNum(j.db_total) + ' رکورد بایگانی';
  lastAuto = auto;

  /* header stop button (global) — show while a sync runs */
  const running = !!j.running;
  const stop = $('#stop-sync-btn');
  if (stop) {
    stop.hidden = !running;
    if (running && !stopBtnBound) {
      stopBtnBound = true;
      stop.addEventListener('click', async (e) => {
        const done = busy(e.currentTarget);
        try {
          await apiPost('/api/sync/stop', {});
          toast('درخواست توقف ارسال شد');
          await pollSync();
        } catch (err) { toast(err.message, true); } finally { done(); }
      });
    }
  }

  /* results table */
  table?.update(j.devices || []);
}

/* ---------- actions ---------- */
async function saveAuto() {
  const enabled = document.getElementById('sy-auto-toggle')?.checked ?? false;
  const minutes = parseFloat(document.getElementById('sy-interval')?.value || '15');
  if (!Number.isFinite(minutes) || minutes <= 0) { toast('فاصلهٔ نامعتبر است', true); return; }
  const btn = document.getElementById('sy-auto-save');
  const done = busy(btn);
  try {
    await apiPost('/api/sync/settings', { enabled, interval: Math.round(minutes * 60) });
    toast('تنظیمات همگام‌سازی ذخیره شد');
    await pollSync();
  } catch (e) { toast(e.message, true); } finally { done(); }
}

async function syncNow(ip) {
  const running = await apiGet('/api/sync', { timeout: 8000 }).then(j => !!j.running).catch(() => false);
  if (running) { toast('همگام‌سازی در حال اجراست — اول توقف بزنید', true); return; }
  await apiPost('/api/sync', { device: ip });
  toast(ip === 'all' ? 'همگام‌سازی همه دستگاه‌ها شروع شد' : `همگام‌سازی ${ip} شروع شد`);
  announce('همگام‌سازی شروع شد');
  pollProgress();
}

async function syncOne(btn) {
  const done = busy(btn);
  const ip = document.getElementById('sy-one-dev')?.value || 'all';
  try { await syncNow(ip); } catch (e) { toast(e.message, true); } finally { done(); }
}

async function syncAll(btn) {
  const done = busy(btn);
  try { await syncNow('all'); } catch (e) { toast(e.message, true); } finally { done(); }
}

async function queryAdms(table2, btn) {
  const dev = document.getElementById('sy-adms-dev')?.value;
  const done = busy(btn);
  try {
    await apiPost('/api/adms/query', { device: dev, table: table2 });
    toast(`درخواست استعلام ${table2 === 'user' ? 'کاربران' : 'ترددها'} در صف ADMS قرار گرفت`);
  } catch (e) { toast(e.message, true); } finally { done(); }
}

/* ---------- ADMS panel ---------- */
async function pollAdms() {
  const panel = document.getElementById('t-sync');
  if (!panel || !panel.classList.contains('active')) return;
  let j;
  try { j = await apiGet('/api/adms', { timeout: 8000 }); } catch { return; }
  const port = document.getElementById('adms-port');
  if (port) port.textContent = faNum(j.port ?? 8081);
  const cnt = document.getElementById('adms-events');
  if (cnt) cnt.textContent = faNum(j.events ?? 0);
  const last = document.getElementById('adms-last');
  if (last) last.textContent = j.last?.sn ? `${j.last.sn} — ${faDate(j.last.time)}` : '—';
  const pend = document.getElementById('adms-pending');
  if (pend) {
    pend.innerHTML = '';
    if (!(j.pending || []).length) pend.append(el('span', { class: 'muted' }, 'صف خالی است'));
    else for (const p of j.pending) {
      pend.append(el('span', { class: 'badge warning' }, `${p.sn || p} (${p.ip || '؟'})`), ' ');
    }
  }
  const appr = document.getElementById('adms-approved');
  if (appr) {
    appr.innerHTML = '';
    if (!(j.approved || []).length) appr.append(el('span', { class: 'muted' }, '—'));
    else for (const sn of j.approved) {
      if (sn === 'SELFCHECK') continue;
      appr.append(el('span', { class: 'badge success' }, sn), ' ');
    }
  }
  const log = document.getElementById('adms-log');
  if (log) {
    log.textContent = (j.cmd_log || []).map(c =>
      `${c.time || ''} ${c.sn || ''} ${c.cmd || ''} ${c.sent ? '✓sent' : ''}`).join('\n');
  }
}

/* ---------- device selects ---------- */
function fillSelects() {
  const one = document.getElementById('sy-one-dev');
  const adm = document.getElementById('sy-adms-dev');
  for (const sel of [one, adm]) {
    if (!sel) continue;
    const cur = sel.value;
    sel.innerHTML = '';
    for (const d of getDevices()) {
      sel.append(el('option', { value: d.ip }, `${d.ip} ${d.label || d.model || ''}`.trim()));
    }
    if (cur) sel.value = cur;
  }
}

/* ---------- init ---------- */
export function initSyncPage() {
  const panel = document.getElementById('t-sync');
  if (!panel || panel.dataset.init) return;
  panel.dataset.init = '1';

  table = createTable({
    columns: [
      { key: 'device', label: 'دستگاه', render: r => el('span', { class: 'ltr mono' }, r.device) },
      { key: 'last_count', label: 'رکورد اخیر', num: true, render: r => faNum(r.last_count || 0) },
      { key: 'last_sync', label: 'آخرین اجرا', render: r => el('span', { class: 'ltr' }, r.last_sync || '—') },
      { key: 'last_error', label: 'وضعیت', render: r => r.last_error
          ? el('span', { class: 'badge danger' }, r.last_error)
          : el('span', { class: 'badge success' }, 'سالم') },
    ],
    pageSize: 25,
    emptyMsg: 'هنوز همگام‌سازی انجام نشده',
  });
  document.getElementById('sy-table-host')?.append(table.root);

  document.getElementById('sy-auto-save')?.addEventListener('click', saveAuto);
  document.getElementById('sy-all-btn')?.addEventListener('click', (e) => syncAll(e.currentTarget));
  document.getElementById('sy-one-btn')?.addEventListener('click', (e) => syncOne(e.currentTarget));
  document.getElementById('sy-refresh-btn')?.addEventListener('click', (e) => {
    const done = busy(e.currentTarget);
    Promise.all([pollSync(), pollAdms(), pollProgress()]).finally(done);
  });
  document.getElementById('adms-q-user')?.addEventListener('click', (e) => queryAdms('user', e.currentTarget));
  document.getElementById('adms-q-attlog')?.addEventListener('click', (e) => queryAdms('attlog', e.currentTarget));

  rateTimer(pollSync, 'sync_ms');
  rateTimer(() => Promise.all([pollAdms(), pollProgress()]), 'progress_active_ms');
}

/* hook for app.js show() */
window.HozorPages = window.HozorPages || {};
window.HozorPages.sync = { onShow: fillSelects };
