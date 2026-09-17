/* api.js — fetch wrapper: timeout via AbortController, Persian errors.
   No external deps. Every call is abortable by passing {signal}. */

const DEFAULT_TIMEOUT = 30_000;

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

/** Internal: fetch JSON with timeout + friendly errors. */
async function request(method, url, { body, timeout = DEFAULT_TIMEOUT, signal } = {}) {
  const ac = new AbortController();
  const onOuter = () => ac.abort();
  if (signal) {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
    signal.addEventListener('abort', onOuter, { once: true });
  }
  const timer = setTimeout(() => ac.abort(), timeout);
  try {
    const res = await fetch(url, {
      method,
      signal: ac.signal,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch { data = text; } }
    if (!res.ok) {
      const msg = (data && (data.error || data.message)) ||
        (res.status === 400 ? 'درخواست نامعتبر است' :
         res.status === 404 ? 'یافت نشد' :
         res.status >= 500 ? 'خطای سرور' : `خطا (${res.status})`);
      throw new ApiError(String(msg), res.status, data);
    }
    return data;
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new ApiError(signal && signal.aborted ? 'لغو شد' : 'زمان انتظار تمام شد', 0, null);
    }
    if (e instanceof ApiError) throw e;
    throw new ApiError('اتصال به سرور برقرار نشد', 0, null);
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onOuter);
  }
}

export const apiGet    = (url, opt)    => request('GET', url, opt);
export const apiPost   = (url, body, opt) => request('POST', url, { ...opt, body });
export const apiPatch  = (url, body, opt) => request('PATCH', url, { ...opt, body });
export const apiPut    = (url, body, opt) => request('PUT', url, { ...opt, body });
export const apiDelete = (url, opt)    => request('DELETE', url, opt);

/** qs({a:1,b:''}) -> 'a=1' (skips empty/null) */
export function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== '' && v !== null && v !== undefined) p.set(k, v);
  }
  return p.toString();
}
