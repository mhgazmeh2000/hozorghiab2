/* scan.js — phase 3: اسکن شبکه tab. POST /api/scan starts, GET /api/scan
   polls (progress + results). Results table with device-type badge and
   "adopt into devices" action (POST /api/devices). */

import { $, el, faNum, toast, announce, busy } from './ui.js';
import { apiGet, apiPost } from './api.js';
import { createTable } from './table.js';
import { RATES, rateTimer } from './poll.js';

let table = null;
let scanning = false;

function typeBadge(item) {
  if (item.platform === 'FK5005' || item.port === 5005)
    return el('span', { class: 'badge accent' }, 'FK (چهره)');
  if (item.port === 4370)
    return el('span', { class: 'badge success' }, 'ZK');
  return el('span', { class: 'badge' }, 'ناشناخته');
}

async function pollScan() {
  const panel = document.getElementById('t-scan');
  if (!panel) return;
  let j;
  try { j = await apiGet('/api/scan', { timeout: 8000 }); } catch { return; }

  const bar = document.getElementById('sc-bar');
  const prog = document.getElementById('sc-progress');
  const startBtn = document.getElementById('sc-start-btn');
  const wasScanning = scanning;
  scanning = !!j.running;

  if (startBtn) {
    startBtn.disabled = scanning;
    startBtn.classList.toggle('busy', scanning);
  }

  if (scanning) {
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    if (bar) bar.style.width = pct + '%';
    if (prog) prog.textContent = `${j.progress || ''} — ${faNum(j.done)} از ${faNum(j.total)}`;
  } else if (wasScanning) {
    /* transition running -> done */
    if (bar) bar.style.width = '100%';
    if (prog) prog.textContent = `اسکن تمام شد — ${faNum((j.found || []).length)} دستگاه یافت شد`;
    announce('اسکن شبکه تمام شد');
    toast(`اسکن تمام شد — ${faNum((j.found || []).length)} دستگاه یافت شد`);
  } else if (prog && j.finished && !prog.textContent.startsWith('اسکن تمام شد')) {
    if (prog) prog.textContent = `اسکن قبلی: ${faNum((j.found || []).length)} دستگاه (${faDate(j.finished)})`;
    if (bar) bar.style.width = '100%';
  }

  table?.update(j.found || []);
}

async function startScan(btn) {
  const done = busy(btn);
  try {
    const subnets = (document.getElementById('sc-subnets')?.value || '')
      .split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    const timeout = parseFloat(document.getElementById('sc-timeout')?.value || '0.6') || 0.6;
    const deep = document.getElementById('sc-deep')?.checked ?? true;
    await apiPost('/api/scan', { subnets, timeout, deep });
    toast('اسکن شروع شد');
    announce('اسکن شبکه شروع شد');
    scanning = false;                     // let pollScan show the transition
    await pollScan();
  } catch (e) { toast(e.message, true); } finally { done(); }
}

async function adopt(ip, port, btn) {
  const done = busy(btn);
  try {
    await apiPost('/api/devices', { ip, port: port || 4370 });
    toast(`${ip} به دستگاه‌ها اضافه شد`);
    btn.textContent = '✔ افزوده شد';
    btn.disabled = true;
  } catch (e) {
    toast(e.message, true);
    if (String(e.message).includes('از قبل')) { btn.textContent = '✔ ثبت شده'; btn.disabled = true; }
  } finally { done(); }
}

export function initScanPage() {
  const panel = document.getElementById('t-scan');
  if (!panel || panel.dataset.init) return;
  panel.dataset.init = '1';

  table = createTable({
    columns: [
      { key: 'ip', label: 'IP', render: r => el('span', { class: 'ltr mono' }, r.ip) },
      { key: 'port', label: 'پورت', num: true, render: r => el('span', { class: 'ltr' }, String(r.port ?? '—')) },
      { key: 'latency_ms', label: 'تأخیر (ms)', num: true, render: r => r.latency_ms != null ? faNum(r.latency_ms) : '—' },
      { key: 'type', label: 'نوع', render: r => typeBadge(r) },
      { key: 'model', label: 'مدل', render: r => r.model || r.platform || '—' },
      { key: 'serial', label: 'سریال', render: r => el('span', { class: 'ltr mono' }, r.serial || '—') },
      { key: 'known', label: 'ثبت‌شده', render: r => r.known
          ? el('span', { class: 'badge success' }, 'در لیست')
          : el('button', {
              class: 'btn small primary',
              onclick: (e) => adopt(r.ip, r.port, e.currentTarget),
            }, 'افزودن به دستگاه‌ها') },
    ],
    pageSize: 50,
    emptyMsg: 'هنوز اسکنی انجام نشده',
  });
  document.getElementById('sc-table-host')?.append(table.root);

  document.getElementById('sc-start-btn')?.addEventListener('click', (e) => startScan(e.currentTarget));

  rateTimer(pollScan, 'scan_ms');
}

window.HozorPages = window.HozorPages || {};
window.HozorPages.scan = {};
