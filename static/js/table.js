/* table.js — generic data table: sort / debounced filter / pagination.
   createTable({columns, rows, pageSize, onRow}) -> {update(rows), root} */

import { el, faNum, emptyState, debounce } from './ui.js';

export function createTable({
  columns,              // [{key, label, num?, sortable?, render?(row)}]
  pageSize = 25,
  emptyMsg = 'موردی یافت نشد',
  rowAttrs,             // (row) => attrs object for <tr>
}) {
  let rows = [];
  let view = [];
  let sortKey = null, sortDir = 1;
  let page = 1;
  const filterInput = el('input', {
    type: 'search', placeholder: 'جستجو…',
    'aria-label': 'جستجو در جدول', style: 'max-width:230px',
  });

  const root = el('div', {},
    el('div', { class: 'table-toolbar', style: 'display:flex;justify-content:flex-end;margin-bottom:var(--space-2)' }, filterInput),
    el('div', { class: 'table-wrap' },
      el('table', { class: 'data' },
        el('thead'), el('tbody'))),
    el('div', { class: 'table-pager' }));

  const [thead, tbody] = ['thead', 'tbody'].map(s => root.querySelector(s));
  const pager = root.querySelector('.table-pager');
  const wrap = root.querySelector('.table-wrap');

  /* header */
  function renderHead() {
    thead.innerHTML = '';
    const tr = el('tr');
    for (const c of columns) {
      const sortable = c.sortable !== false;
      const th = el('th', {
        class: (c.num ? 'num ' : '') + (sortable ? 'sortable' : ''),
        scope: 'col',
        tabindex: sortable ? '0' : null,
        role: sortable ? 'button' : null,
        'aria-sort': sortKey === c.key ? (sortDir === 1 ? 'ascending' : 'descending') : null,
        onclick: sortable ? () => sortBy(c.key) : null,
        onkeydown: sortable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortBy(c.key); } } : null,
      }, c.label,
        sortable ? el('span', { class: 'sort-ind', 'aria-hidden': 'true' },
          sortKey === c.key ? (sortDir === 1 ? '▲' : '▼') : '⇅') : null);
      tr.append(th);
    }
    thead.append(tr);
  }

  function sortBy(key) {
    if (sortKey === key) sortDir = -sortDir;
    else { sortKey = key; sortDir = 1; }
    renderHead();
    apply();
  }

  /* filter (debounced 300ms) */
  let rawFilter = '';
  filterInput.addEventListener('input', debounce(() => {
    rawFilter = filterInput.value.trim().toLowerCase();
    page = 1;
    apply();
  }, 300));

  function apply() {
    view = rows;
    if (rawFilter) {
      view = view.filter(r => columns.some(c => {
        const v = c.render ? '' : r[c.key];
        return String(v ?? '').toLowerCase().includes(rawFilter);
      }));
    }
    if (sortKey) {
      view = [...view].sort((a, b) => {
        const x = a[sortKey], y = b[sortKey];
        const nx = Number(x), ny = Number(y);
        const cmp = (!isNaN(nx) && !isNaN(ny)) ? nx - ny
          : String(x ?? '').localeCompare(String(y ?? ''), 'fa');
        return cmp * sortDir;
      });
    }
    renderBody();
    renderPager();
    // edge-fade marker (kept from approved design)
    wrap.classList.toggle('has-hscroll', wrap.scrollWidth > wrap.clientWidth + 4);
  }

  function renderBody() {
    const start = (page - 1) * pageSize;
    const slice = view.slice(start, start + pageSize);
    tbody.innerHTML = '';
    if (!slice.length) {
      const tr = el('tr');
      const td = el('td', { colspan: String(columns.length) });
      emptyState(td, emptyMsg);
      tr.append(td);
      tbody.append(tr);
      return;
    }
    for (const r of slice) {
      const attrs = rowAttrs ? (rowAttrs(r) || {}) : {};
      const tr = el('tr', attrs);
      for (const c of columns) {
        const content = c.render ? c.render(r) : (r[c.key] ?? '—');
        const td = el('td', {
          class: c.num ? 'num' : null,
          'data-label': c.label,
        }, content);
        tr.append(td);
      }
      tbody.append(tr);
    }
  }

  function renderPager() {
    const total = view.length;
    const pages = Math.max(1, Math.ceil(total / pageSize));
    page = Math.min(page, pages);
    pager.innerHTML = '';
    if (pages <= 1 && total <= pageSize) {
      if (total) pager.append(el('span', {}, `${faNum(total)} ردیف`));
      return;
    }
    pager.append(el('span', {}, `مجموع ${faNum(total)} — صفحه ${faNum(page)} از ${faNum(pages)}`));
    const nav = el('div', { class: 'pages', role: 'navigation', 'aria-label': 'صفحه‌بندی' });
    const btn = (label, p, opts = {}) => el('button', {
      'aria-label': opts.aria || `صفحه ${p}`,
      'aria-current': p === page ? 'page' : null,
      disabled: opts.disabled || null,
      onclick: () => { page = p; apply(); },
    }, label);
    nav.append(btn('‹', page - 1, { disabled: page === 1, aria: 'قبلی' }));
    const win = pagerWindow(page, pages);
    for (const p of win) nav.append(btn(String(new Intl.NumberFormat('fa-IR').format(p)), p, { disabled: p === 0 }));
    nav.append(btn('›', page + 1, { disabled: page === pages, aria: 'بعدی' }));
    pager.append(nav);
  }

  function pagerWindow(cur, pages) {
    const out = [];
    for (let p = 1; p <= pages; p++) {
      if (p === 1 || p === pages || Math.abs(p - cur) <= 1) out.push(p);
      else if (out[out.length - 1] !== 0) out.push(0);   // 0 = ellipsis slot
    }
    return out.map(p => (p === 0 ? '…' : p)).filter((v, i, a) => v === '…' ? a[i - 1] !== '…' : true);
  }

  renderHead();
  apply();   // render empty state + pager immediately (was: blank body that looked 'loading')

  return {
    root,
    update(newRows) { rows = Array.isArray(newRows) ? newRows : []; page = 1; apply(); },
    refresh() { apply(); },
  };
}
