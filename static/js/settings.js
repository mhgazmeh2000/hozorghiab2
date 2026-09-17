/* settings.js — phase 3: تنظیمات tab. Renders the 7 settings sections as
   cards with numeric inputs (min/max mirrored from the server whitelist),
   dirty-only save, per-section "reset to defaults", read-only backup dir,
   and live re-apply of ui_polling rates. */

import { $, el, faNum, toast } from './ui.js';
import { apiGet, apiPost } from './api.js';
import { loadRates, applyRates } from './poll.js';

/* section metadata: key, Persian title, short description, per-key labels,
   per-key min/max (mirrors _SETTINGS_RANGE in app.py), step, note */
const SECTIONS = [
  {
    key: 'cache_ttl', title: 'کش سرور (TTL)', icon: 'i-refresh',
    desc: 'مدت کش‌شدن پاسخ‌های خواندنی در سرور؛ عدد بالاتر = پاسخ سریع‌تر ولی دادهٔ کهنه‌تر. ثانیه.',
    note: 'بعد از ذخیره، کش سرور خودکار خالی می‌شود.',
    fields: [
      ['devices', 'وضعیت دستگاه‌ها', 0, 600],
      ['scan', 'اسکن شبکه', 0, 600],
      ['connection_logs', 'لاگ ارتباط', 0, 600],
      ['archive', 'بایگانی', 0, 600],
      ['sync', 'وضعیت همگام‌سازی', 0, 600],
    ],
  },
  {
    key: 'lock_timeout', title: 'تایم‌اوت قفل دستگاه', icon: 'i-dev',
    desc: 'حداکثر انتظار برای آزادشدن قفل یک دستگاه وقتی عملیات دیگری آن را گرفته. ثانیه.',
    fields: [
      ['default', 'عمومی (پول green-label)', 3, 600],
      ['fk', 'دستگاه‌های FK (چهره)', 1, 120],
      ['set_time', 'تنظیم ساعت', 1, 120],
    ],
  },
  {
    key: 'sync', title: 'همگام‌سازی خودکار', icon: 'i-sync',
    desc: 'فاصلهٔ پول خودکار همهٔ دستگاه‌ها و حداکثر انتظار در صف همگام‌سازی. ثانیه.',
    note: 'تغییر auto_interval بدون ری‌استارت از دورهٔ بعدی اعمال می‌شود.',
    fields: [
      ['auto_interval', 'فاصلهٔ همگام‌سازی خودکار', 60, 86400],
      ['lock_queue_timeout', 'تایم‌اوت صف همگام‌سازی', 5, 600],
    ],
  },
  {
    key: 'backup', title: 'پشتیبان‌گیری دیتابیس', icon: 'i-arch',
    desc: 'هر چند ساعت یک کپی از attendance.db گرفته شود و چند نسخهٔ آخر نگه داشته شود.',
    fields: [
      ['interval_hours', 'فاصله (ساعت)', 1, 168],
      ['keep', 'تعداد نسخه‌های نگه‌داشته', 1, 100],
    ],
  },
  {
    key: 'log', title: 'چرخش فایل لاگ', icon: 'i-log',
    desc: 'سقف حجم هر فایل لاگ و تعداد فایل‌های چرخشی. بایت.',
    fields: [
      ['max_bytes', 'حداکثر حجم هر فایل', 100000, 100000000],
      ['backups', 'تعداد فایل‌های پشتیبان لاگ', 0, 20],
    ],
  },
  {
    key: 'web', title: 'وب‌سرور', icon: 'i-dash',
    desc: 'تعداد thread های waitress برای پاسخ‌دهی همزمان.',
    note: '⚠ این مقدار فقط در استارت سرور اعمال می‌شود — بعد از ذخیره، ری‌استارت لازم است.',
    fields: [
      ['threads', 'تعداد thread', 1, 64],
    ],
  },
  {
    key: 'ui_polling', title: 'نرخ به‌روزرسانی صفحه', icon: 'i-set',
    desc: 'فاصلهٔ polling های مرورگر. میلی‌ثانیه — بلافاصله بعد از ذخیره اعمال می‌شود.',
    fields: [
      ['devices_ms', 'بروزرسانی دستگاه‌ها/لاگ ارتباط', 2000, 600000],
      ['sync_ms', 'بروزرسانی تب همگام‌سازی', 2000, 600000],
      ['scan_ms', 'بروزرسانی اسکن', 1000, 600000],
      ['enroll_ms', 'بروزرسانی ثبت اثر انگشت', 500, 60000],
      ['progress_active_ms', 'پیشرفت در حال کار', 500, 60000],
      ['progress_idle_ms', 'پیشرفت در حالت بیکار', 2000, 600000],
    ],
  },
];

let STATE = null;      // {settings, defaults, backup_dir, env_overrides}

function inputId(sec, key) { return `st-${sec}-${key}`; }

function buildSection(sec) {
  const card = el('div', { class: 'card' });
  card.append(el('div', { class: 'card-header' },
    el('svg', { 'aria-hidden': 'true', width: 16, height: 16,
      html: `<use href="/static/icons/sprite.svg#${sec.icon}"></use>` }),
    sec.title));

  const body = el('div', { class: 'card-body' });
  body.append(el('div', { class: 'muted', style: 'font-size:.88em;margin-bottom:var(--space-3)' }, sec.desc));

  const row = el('div', { class: 'form-row' });
  for (const [key, label, min, max] of sec.fields) {
    const id = inputId(sec.key, key);
    const field = el('label', { class: 'field', style: 'max-width:230px' });
    field.append(el('span', {}, label));
    const inp = el('input', {
      id, type: 'number', min: String(min), max: String(max), step: 'any',
      'data-sec': sec.key, 'data-key': key, 'data-min': String(min), 'data-max': String(max),
      style: 'direction:ltr',
    });
    inp.addEventListener('input', markDirty);
    field.append(inp);
    row.append(field);
  }
  body.append(row);

  if (sec.note) body.append(el('div', {
    class: 'muted', style: 'font-size:.82em;margin-top:var(--space-2)' }, sec.note));

  const actions = el('div', { class: 'form-row', style: 'margin-top:var(--space-3)' });
  const saveBtn = el('button', { class: 'btn small primary', 'data-save': sec.key }, 'ذخیرهٔ این بخش');
  saveBtn.addEventListener('click', () => saveSection(sec.key, saveBtn));
  const resetBtn = el('button', { class: 'btn small', 'data-reset': sec.key }, 'بازگشت به پیش‌فرض');
  resetBtn.addEventListener('click', () => resetSection(sec.key, resetBtn));
  const status = el('span', { class: 'muted', style: 'align-self:center;font-size:.85em', 'data-status': sec.key });
  actions.append(saveBtn, resetBtn, status);
  body.append(actions);

  card.append(body);
  return card;
}

function setVal(sec, key, v) {
  const inp = document.getElementById(inputId(sec, key));
  if (inp && v != null) inp.value = String(v);
}

function fillForm(s) {
  for (const sec of SECTIONS) {
    const sub = s?.[sec.key] || {};
    for (const [key] of sec.fields) setVal(sec.key, key, sub[key]);
    const st = document.querySelector(`[data-status="${sec.key}"]`);
    if (st) st.textContent = '';
  }
}

function markDirty() {
  if (!STATE) return;
  for (const sec of SECTIONS) {
    const cur = STATE.settings?.[sec.key] || {};
    for (const [key] of sec.fields) {
      const inp = document.getElementById(inputId(sec.key, key));
      if (!inp) continue;
      const changed = String(cur[key] ?? '') !== String(inp.value);
      inp.classList.toggle('invalid', changed && !inRange(inp));
      inp.style.borderColor = changed ? 'var(--color-accent)' : '';
    }
    const st = document.querySelector(`[data-status="${sec.key}"]`);
    if (st) st.textContent = sectionDirty(sec.key) ? 'تغییرات ذخیرهٔ نشده' : '';
  }
}

function inRange(inp) {
  const v = parseFloat(inp.value);
  const min = parseFloat(inp.dataset.min), max = parseFloat(inp.dataset.max);
  return Number.isFinite(v) && v >= min && v <= max;
}

function sectionDirty(secKey) {
  const sec = SECTIONS.find(s => s.key === secKey);
  const cur = STATE?.settings?.[secKey] || {};
  return sec.fields.some(([key]) => {
    const inp = document.getElementById(inputId(secKey, key));
    return inp && String(cur[key] ?? '') !== String(inp.value);
  });
}

function collectSection(secKey) {
  const sec = SECTIONS.find(s => s.key === secKey);
  const out = {};
  for (const [key, label, min, max] of sec.fields) {
    const inp = document.getElementById(inputId(secKey, key));
    if (!inp) continue;
    const v = parseFloat(inp.value);
    if (!Number.isFinite(v)) throw new Error(`${label}: عدد وارد کنید`);
    if (v < min || v > max) throw new Error(`${label}: باید بین ${faNum(min)} و ${faNum(max)} باشد`);
    out[key] = v % 1 === 0 ? Math.round(v) : v;
  }
  return { [secKey]: out };
}

async function saveSection(secKey, btn) {
  const done = btn ? (() => { btn.disabled = true; return () => { btn.disabled = false; }; })() : () => {};
  try {
    const payload = collectSection(secKey);
    const j = await apiPost('/api/settings', payload);
    STATE.settings = j.settings || STATE.settings;
    fillStatus();
    if (secKey === 'ui_polling' && j.settings?.ui_polling) applyRates(j.settings.ui_polling);
    const st = document.querySelector(`[data-status="${secKey}"]`);
    if (st) st.textContent = '✔ ذخیره شد';
    toast('تنظیمات ذخیره شد و بلافاصله اعمال شد');
  } catch (e) { toast(e.message, true); } finally { done(); }
}

async function resetSection(secKey, btn) {
  const done = btn ? (() => { btn.disabled = true; return () => { btn.disabled = false; }; })() : () => {};
  try {
    const sec = SECTIONS.find(s => s.key === secKey);
    const defaults = STATE?.defaults?.[secKey] || {};
    for (const [key] of sec.fields) setVal(secKey, key, defaults[key]);
    if (sectionDirty(secKey)) await saveSection(secKey);
    else { const st = document.querySelector(`[data-status="${secKey}"]`); if (st) st.textContent = 'همان پیش‌فرض است'; }
  } catch (e) { toast(e.message, true); } finally { done(); }
}

function fillStatus() {
  markDirty();
  for (const sec of SECTIONS) {
    const st = document.querySelector(`[data-status="${sec.key}"]`);
    if (st && !sectionDirty(sec.key)) st.textContent = '';
  }
}

export function initSettingsPage() {
  const panel = document.getElementById('t-settings');
  if (!panel || panel.dataset.init) return;
  panel.dataset.init = '1';

  const host = document.getElementById('st-sections');
  if (host) for (const sec of SECTIONS) host.append(buildSection(sec));

  /* read-only backup dir + env overrides line */
  apiGet('/api/settings', { timeout: 10000 }).then((j) => {
    STATE = j;
    fillForm(j.settings);
    fillStatus();
    const bd = document.getElementById('st-backup-dir');
    if (bd) bd.textContent = j.backup_dir || '—';
    const env = document.getElementById('st-env');
    if (env) {
      env.textContent = (j.env_overrides && j.env_overrides.length)
        ? `متغیرهای محیطی فعال: ${j.env_overrides.join(', ')} (اولویت بالاتر از پیش‌فرض، پایین‌تر از این فرم)` : '';
    }
  }).catch(() => toast('خواندن تنظیمات ناموفق بود', true));
}

window.HozorPages = window.HozorPages || {};
window.HozorPages.settings = {};
