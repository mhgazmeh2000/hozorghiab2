/* users.js — phase 2: users tab. Ported from legacy: device filter,
   create user, enroll incl. green-label template copy + status polling.
   Enroll polling rate comes from poll.js (RATES.enroll_ms). */

import { $, el, faNum, toast, announce, busy } from './ui.js';
import { apiGet, apiPost } from './api.js';
import { createTable } from './table.js';
import { getDevices } from './devices.js';
import { RATES } from './poll.js';

let table = null;
let enrollTimer = null;

/* ---------- enrollment status polling ---------- */
function pollEnroll(ip, id) {
  if (enrollTimer) clearInterval(enrollTimer);
  const sum = document.getElementById('u-sum');
  enrollTimer = setInterval(async () => {
    try {
      const j = await apiGet(`/api/devices/${encodeURIComponent(ip)}/enroll`);
      const e = j.enrollment;
      if (!e || e.user_id !== id) return;
      if (sum) sum.textContent = (e.stage ? `[${e.stage}] ` : '') + (e.message || '');
      if (!e.running) {
        clearInterval(enrollTimer); enrollTimer = null;
        toast(e.ok ? 'ثبت اثر انگشت کامل شد' : 'ثبت اثر انگشت ناتمام', !e.ok);
        load();
      }
    } catch {}
  }, RATES.enroll_ms);
}

/* ---------- list ---------- */
async function load() {
  const ip = document.getElementById('u-dev')?.value || 'all';
  const filter = document.getElementById('u-filter')?.value || '';
  const sum = document.getElementById('u-sum');
  try {
    const p = new URLSearchParams();
    if (ip && ip !== 'all') p.set('device', ip);
    if (filter) p.set('q', filter);
    const j = await apiGet('/api/users' + (p.toString() ? '?' + p : ''), { timeout: 120000 });
    const users = j.users || [];
    table?.update(users);
    const errs = j.errors ? Object.entries(j.errors).map(([k, v]) => k + ': ' + v).join(' | ') : '';
    if (sum) sum.textContent = `${faNum(users.length)} کاربر` + (errs ? ' — خطا: ' + errs : '');
  } catch (e) {
    if (sum) sum.textContent = '';
    toast(e.message, true);
  }
}

/* ---------- create user ---------- */
async function createUser(btn) {
  const done = busy(btn);
  try {
    const ip = document.getElementById('u-dev')?.value;
    if (!ip || ip === 'all') throw new Error('اول یک دستگاه انتخاب کنید');
    const id = document.getElementById('u-id')?.value.trim();
    const uid = document.getElementById('u-uid')?.value.trim();
    const name = document.getElementById('u-name')?.value.trim();
    const card = document.getElementById('u-card')?.value.trim();
    if (!id && !uid) throw new Error('شناسه کاربر یا UID لازم است');
    await apiPost(`/api/devices/${encodeURIComponent(ip)}/users`, {
      user_id: id || undefined, uid: uid || undefined,
      name: name || undefined, card: card || undefined,
    });
    toast('کاربر ساخته شد');
    announce('کاربر ساخته شد');
    load();
  } catch (e) { toast(e.message, true); } finally { done(); }
}

/* ---------- enroll (button in table row) ---------- */
async function enroll(ip, id, uid, btn) {
  const done = busy(btn);
  const src = document.getElementById('u-src')?.value || '';
  try {
    const j = await apiPost(`/api/devices/${encodeURIComponent(ip)}/enroll`, {
      user_id: id, uid, source: src || undefined,
    });
    const e = j.enrollment || {};
    toast(e.message || 'ثبت اثر انگشت شروع شد');
    pollEnroll(ip, id);
    load();
  } catch (e) { toast(e.message, true); } finally { done(); }
}

/* ---------- device selects ---------- */
function fillDevSelect() {
  for (const selId of ['u-dev', 'u-src']) {
    const sel = document.getElementById(selId);
    if (!sel) continue;
    const cur = sel.value;
    sel.innerHTML = '';
    if (selId === 'u-src') sel.append(el('option', { value: '' }, '—'));
    for (const d of getDevices()) {
      sel.append(el('option', { value: d.ip }, `${d.ip} ${d.label || d.model || ''}`.trim()));
    }
    if (cur) sel.value = cur;
  }
}

/* ---------- init ---------- */
export function initUsersPage() {
  const host = document.getElementById('users-table-host');
  if (!host || host.dataset.init) return;
  host.dataset.init = '1';

  table = createTable({
    columns: [
      { key: 'device', label: 'دستگاه', render: r => el('span', { class: 'ltr mono' }, r.device) },
      { key: 'user_id', label: 'شناسه', render: r => el('span', { class: 'ltr mono' }, r.user_id) },
      { key: 'uid', label: 'UID', num: true },
      { key: 'name', label: 'نام' },
      { key: 'role', label: 'سطح' },
      { key: 'card', label: 'کارت', render: r => el('span', { class: 'ltr mono' }, r.card || '—') },
      { key: 'actions', label: 'اثر انگشت', sortable: false, render: r =>
        el('button', {
          class: 'btn small',
          onclick: (e) => enroll(r.device, r.user_id, r.uid, e.currentTarget),
        }, 'ثبت اثر انگشت') },
    ],
    pageSize: 50,
    emptyMsg: 'کاربری نیست — دستگاه را انتخاب و بروزرسانی کنید',
  });
  host.append(table.root);

  const debouncedLoad = (() => { let t; return () => { clearTimeout(t); t = setTimeout(load, 250); }; })();
  document.getElementById('u-dev')?.addEventListener('change', load);
  document.getElementById('u-filter')?.addEventListener('input', debouncedLoad);
  document.getElementById('create-user-btn')?.addEventListener('click', (e) => createUser(e.currentTarget));
}

/* hook for app.js show() */
window.HozorPages = window.HozorPages || {};
window.HozorPages.users = {
  onShow() {
    fillDevSelect();
  },
};
