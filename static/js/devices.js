/* devices.js — phase 1: dashboard KPIs + registered devices table.
   Ported 1:1 from the legacy inline UI: same endpoints, same behaviors.
   New: optimistic delete (with confirm), busy buttons, skeletons. */

import { $, el, icon, faNum, faDate, toast, announce, busy,
         confirmDialog, modal, skeletonKpis, emptyState } from './ui.js';
import { apiGet, apiPost, apiDelete } from './api.js';
import { createTable } from './table.js';

let DEVICES = [];
let table = null;       // devices tab table
let dashTable = null;   // dashboard quick-status table

/* ---------- helpers shared with other modules (phase 2+) ---------- */
export function getDevices() { return DEVICES; }

function statusCell(d) {
  const on = d.last_state === 'online';
  return el('span', { class: 'badge ' + (on ? 'success' : 'danger') },
    el('span', { class: 'status-dot ' + (on ? 'on' : 'off'), 'aria-hidden': 'true' }),
    on ? 'آنلاین' : 'آفلاین');
}

function modelCell(d) {
  return el('span', {}, d.label || d.model || '—');
}

/* ---------- KPIs ---------- */
function renderKpis(devs) {
  const host = $('#kpis');
  if (!host) return;
  const online = devs.filter(d => d.last_state === 'online').length;
  const offline = devs.length - online;
  host.innerHTML = '';
  host.append(
    kpi(devs.length, 'دستگاه ثبت‌شده', ''),
    kpi(online, 'آنلاین', 'ok'),
    kpi(offline, 'آفلاین', offline ? 'bad' : ''));
  announce(`${devs.length} دستگاه، ${online} آنلاین، ${offline} آفلاین`);
}
function kpi(v, label, cls) {
  return el('div', { class: 'kpi ' + cls }, el('b', {}, faNum(v)), el('span', {}, label));
}

/* ---------- actions ---------- */
export async function loadDevices(fresh = false) {
  const tbodyHost = $('#dev-table-host');
  if (!tbodyHost) return DEVICES;
  try {
    const j = await apiGet('/api/devices');
    DEVICES = Array.isArray(j) ? j : (j.devices || []);
    renderKpis(DEVICES);
    table?.update(DEVICES);
    dashTable?.update(DEVICES);
  } catch (e) {
    toast(e.message, true);
  }
  return DEVICES;
}

export async function checkAllDevices(btn) {
  const done = busy(btn);
  try {
    await Promise.allSettled(DEVICES.map(d =>
      apiPost(`/api/devices/${encodeURIComponent(d.ip)}/ping`)));
    toast('بررسی اتصال همه انجام شد');
    await loadDevices();
  } catch (e) {
    toast(e.message, true);
  } finally { done(); }
}

async function pingOne(ip, btn) {
  const done = busy(btn);
  try {
    const r = await apiPost(`/api/devices/${encodeURIComponent(ip)}/ping`, {}, { timeout: 20000 });
    toast(r?.ok === false ? `${ip}: آفلاین` : `${ip}: آنلاین ✔`, r?.ok === false);
    await loadDevices();
  } catch (e) { toast(e.message, true); } finally { done(); }
}

async function identifyOne(ip, btn) {
  const done = busy(btn);
  try {
    const r = await apiPost(`/api/devices/${encodeURIComponent(ip)}/identify`, {}, { timeout: 60000 });
    toast(r?.error ? `${ip}: ${r.error}` : `${ip}: شناسایی شد — ${r?.model || ''} ${r?.serial || ''}`,
          !!r?.error);
    await loadDevices();
  } catch (e) { toast(e.message, true); } finally { done(); }
}

async function rebootDevice(ip, btn) {
  const ok = await confirmDialog(`دستگاه ${ip} ری‌استارت شود؟`);
  if (!ok) return;
  const done = busy(btn);
  try {
    await apiPost(`/api/devices/${encodeURIComponent(ip)}/restart`);
    toast(`دستور ری‌استارت به ${ip} ارسال شد`);
  } catch (e) { toast(e.message, true); } finally { done(); }
}

/** optimistic delete: row leaves the table instantly, server call follows;
    on failure we reload to restore the truth. */
async function delDevice(ip) {
  const ok = await confirmDialog(`دستگاه ${ip} از لیست حذف شود؟`, { okText: 'حذف' });
  if (!ok) return;
  const snapshot = DEVICES;
  DEVICES = DEVICES.filter(d => d.ip !== ip);       // optimistic
  renderKpis(DEVICES);
  table?.update(DEVICES);
  toast(`${ip} حذف شد`);
  try {
    await apiDelete(`/api/devices/${encodeURIComponent(ip)}`);
  } catch (e) {
    toast('حذف در سرور ناموفق بود — بازگردانی', true);
    DEVICES = snapshot;
    renderKpis(DEVICES);
    table?.update(DEVICES);
  }
}

/* ---------- add device modal ---------- */
function addDeviceModal() {
  const ip = el('input', { type: 'text', inputmode: 'decimal', placeholder: '172.16.x.x', class: 'ltr' });
  const port = el('input', { type: 'number', value: '4370', min: '1', max: '65535', class: 'ltr' });
  const label = el('input', { type: 'text', placeholder: 'اختیاری (مثلاً WL50)' });
  const loc  = el('input', { type: 'text', placeholder: 'اختیاری' });
  const form = el('div', { class: 'form-row' },
    field('آدرس IP', ip), field('پورت', port), field('برچسب', label), field('محل', loc));

  const m = modal({
    title: 'افزودن دستگاه',
    body: form,
    footer: [
      el('button', { class: 'btn', onclick: () => m.close() }, 'انصراف'),
      el('button', { class: 'btn primary', onclick: async () => {
        if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip.value.trim())) {
          toast('آدرس IP نامعتبر است', true); return;
        }
        const done = busy(m.dlg.querySelector('.btn.primary'));
        try {
          await apiPost('/api/devices', { ip: ip.value.trim(),
            port: Number(port.value) || 4370, label: label.value.trim(),
            location: loc.value.trim() });
          toast('دستگاه اضافه شد');
          m.close();
          await loadDevices();
        } catch (e) { toast(e.message, true); } finally { done(); }
      } }, 'افزودن'),
    ],
  });
}
function field(label, inputEl) {
  return el('label', { class: 'field' }, el('span', {}, label), inputEl);
}

/* ---------- init ---------- */
export function initDevicesPage() {
  /* KPIs skeleton */
  const kpis = $('#kpis');
  if (kpis) skeletonKpis(kpis, 3);

  /* table */
  const host = $('#dev-table-host');
  if (!host) return;
  table = createTable({
    columns: [
      { key: 'last_state', label: 'وضعیت', render: statusCell },
      { key: 'ip', label: 'IP', render: d => el('span', { class: 'ltr mono' }, d.ip) },
      { key: 'model', label: 'مدل', render: modelCell },
      { key: 'serial', label: 'سریال', render: d => el('span', { class: 'ltr mono' }, d.serial || '—') },
      { key: 'last_log_ts', label: 'آخرین تردد', render: d => faDate(d.last_log_ts) },
      { key: 'last_check', label: 'آخرین بررسی', render: d => faDate(d.last_check) },
      { key: 'acts', label: 'عملیات', sortable: false, render: d => actionsCell(d) },
    ],
    emptyMsg: 'دستگاهی ثبت نشده است',
  });
  host.append(table.root);

  /* dashboard quick-status table (read-only subset) */
  const dashHost = $('#dash-tbl-host');
  if (dashHost) {
    dashTable = createTable({
      columns: [
        { key: 'last_state', label: 'وضعیت', render: statusCell },
        { key: 'ip', label: 'IP', render: d => el('span', { class: 'ltr mono' }, d.ip) },
        { key: 'model', label: 'مدل', render: modelCell },
        { key: 'serial', label: 'سریال', render: d => el('span', { class: 'ltr mono' }, d.serial || '—') },
        { key: 'last_log_ts', label: 'آخرین تردد', render: d => faDate(d.last_log_ts) },
        { key: 'last_check', label: 'آخرین بررسی', render: d => faDate(d.last_check) },
      ],
      emptyMsg: 'دستگاهی ثبت نشده است',
    });
    dashHost.append(dashTable.root);
  }

  /* add-device button */
  $('#add-dev-btn')?.addEventListener('click', addDeviceModal);
}

function actionsCell(d) {
  const ip = d.ip;
  const wrap = el('div', { class: 'actions col-actions' });
  const mk = (label, fn, cls = 'btn small', tip) => {
    const b = el('button', { class: cls, 'data-tip': tip || null, onclick: (e) => fn(ip, e.currentTarget) }, label);
    wrap.append(b);
    return b;
  };
  mk('اتصال', pingOne, 'btn small', 'تست اتصال');
  mk('شناسایی', identifyOne, 'btn small', 'خواندن مدل/سریال');
  mk('ری‌استارت', rebootDevice, 'btn small danger', 'ریبوت دستگاه');
  mk('حذف', (ip2) => delDevice(ip2), 'btn small danger', 'حذف از لیست');
  return wrap;
}
