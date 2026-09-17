/* poll.js — shared UI polling rates, fed from /api/settings (ui_polling).
   Every long-lived timer asks RATES instead of hardcoding; rateTimer()
   re-reads the delay each tick, so a settings change applies on the next
   tick without restart. */

export const RATES = {
  devices_ms: 8000,
  sync_ms: 5000,
  scan_ms: 2000,
  enroll_ms: 1200,
  progress_active_ms: 1000,
  progress_idle_ms: 5000,
};

let loading = null;

/** Fetch ui_polling once (idempotent; concurrent callers share the promise).
    force=true re-fetches (used right after saving settings). */
export function loadRates(force = false) {
  if (!loading || force) {
    loading = (async () => {
      try {
        const { apiGet } = await import('./api.js');
        const s = await apiGet('/api/settings', { timeout: 8000 });
        const p = s?.settings?.ui_polling;
        if (p) applyRates(p);
      } catch {}
      return RATES;
    })();
  }
  return loading;
}

/** Merge a ui_polling object into RATES (clamped to sane bounds). */
export function applyRates(p) {
  const MIN = { devices_ms: 2000, sync_ms: 2000, scan_ms: 1000, enroll_ms: 500,
                progress_active_ms: 500, progress_idle_ms: 2000 };
  for (const k of Object.keys(RATES)) {
    const v = Number(p?.[k]);
    if (Number.isFinite(v) && v >= (MIN[k] ?? 200)) RATES[k] = v;
  }
  return RATES;
}

/** Self-rescheduling timer whose delay is re-read from RATES[key] every
    tick. Returns a stop() function. The callback may be async; errors are
    swallowed so a failing endpoint never kills the loop. */
export function rateTimer(fn, key) {
  let alive = true;
  (function tick() {
    if (!alive) return;
    setTimeout(async () => {
      if (!alive) return;
      try { await fn(); } catch {}
      tick();
    }, RATES[key]);
  })();
  return () => { alive = false; };
}
