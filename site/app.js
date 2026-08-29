/* Static dashboard for GitHub Pages.

   No server and no Google credentials: the data arrives as AES-GCM ciphertext
   produced by the GitHub Actions run, and the password typed below is the
   decryption key. A wrong password fails as a decryption error, so the data is
   genuinely unreadable rather than hidden behind a JavaScript comparison. */

const CFG = window.DASHBOARD_CONFIG || {};
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const banner = (host, kind, msg) => {
  host.innerHTML = '';
  if (msg) host.append(el('div', `banner ${kind}`, msg));
};

/* ── Crypto ─────────────────────────────────────────────────────────────── */

const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

async function deriveKey(password, kdf) {
  const material = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt: b64(kdf.salt), iterations: kdf.iterations, hash: kdf.hash },
    material, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
}

/* Decrypt one published file. Throws if the password is wrong — the caller
   treats that as a failed login rather than a corrupt file. */
async function fetchEncrypted(name, password) {
  const res = await fetch(`data/${name}.enc.json`, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`could not load ${name} (HTTP ${res.status})`);
  const payload = await res.json();
  const key = await deriveKey(password, payload.kdf);
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b64(payload.cipher.iv) }, key, b64(payload.data));
  return JSON.parse(new TextDecoder().decode(plain));
}

/* ── Login gate ─────────────────────────────────────────────────────────── */

let PASSWORD = null;
let MANIFEST = null;

async function unlock(password) {
  MANIFEST = await fetchEncrypted('index', password);   // throws on a bad password
  PASSWORD = password;
  sessionStorage.setItem('dash-pw', password);          // this tab only
  $('gate').hidden = true;
  $('app').hidden = false;
  await boot();
}

$('gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('gate-go');
  const err = $('gate-err');
  btn.disabled = true;
  btn.textContent = 'Unlocking…';
  err.textContent = '';
  try {
    await unlock($('gate-pw').value);
  } catch (ex) {
    err.textContent = ex.name === 'OperationError'
      ? 'Wrong password.'
      : `Could not unlock: ${ex.message}`;
    $('gate-pw').select();
  } finally {
    btn.disabled = false;
    btn.textContent = 'Unlock';
  }
});

$('btn-lock').addEventListener('click', () => {
  sessionStorage.removeItem('dash-pw');
  location.reload();
});

/* ── Data ───────────────────────────────────────────────────────────────── */

const data = { worksheet: null, columns: [], rows: [], cache: {},
               search: '', facets: {}, sort: null, sortDir: 1, page: 0, pageSize: 100 };

function facetableColumns(columns, rows) {
  return columns.filter((col) => {
    if (col.endsWith('__url') || /link|url/i.test(col)) return false;
    const seen = new Set();
    for (const row of rows) {
      const v = (row[col] || '').trim();
      if (v) seen.add(v);
      if (seen.size > 40) return false;
    }
    return seen.size > 1;
  });
}

function distinct(col) {
  const counts = new Map();
  for (const row of data.rows) {
    const v = (row[col] || '').trim() || '(blank)';
    counts.set(v, (counts.get(v) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]);
}

function filteredRows() {
  const q = data.search.trim().toLowerCase();
  let rows = data.rows.filter((row) => {
    for (const [col, want] of Object.entries(data.facets)) {
      if (!want) continue;
      if (((row[col] || '').trim() || '(blank)') !== want) return false;
    }
    if (!q) return true;
    return data.columns.some((c) => (row[c] || '').toLowerCase().includes(q));
  });
  if (data.sort) {
    const col = data.sort;
    rows = rows.slice().sort((a, b) => {
      const x = a[col] || '', y = b[col] || '';
      const nx = parseFloat(x.replace(/,/g, '')), ny = parseFloat(y.replace(/,/g, ''));
      const numeric = !isNaN(nx) && !isNaN(ny) && x.trim() && y.trim();
      return (numeric ? nx - ny : x.localeCompare(y, undefined, { numeric: true })) * data.sortDir;
    });
  }
  return rows;
}

function statusPill(col, value) {
  const map = { active: 'ok', expired: 'err', removed: 'err', unknown: 'warn',
    university: 'info', government: 'info', 'nonprofit / ngo': 'info',
    'hospital / medical': 'info', 'research institute': 'info',
    'educational institution': 'info', company: 'neutral', other: 'neutral' };
  const v = (value || '').toLowerCase();
  if (!/status|type/i.test(col) || !map[v]) return null;
  return el('span', `pill ${map[v]}`, value);
}

function renderTable() {
  const rows = filteredRows();
  const pages = Math.max(1, Math.ceil(rows.length / data.pageSize));
  data.page = Math.min(data.page, pages - 1);
  const slice = rows.slice(data.page * data.pageSize, (data.page + 1) * data.pageSize);

  const head = $('data-head');
  head.innerHTML = '';
  head.append(el('th', '', '#'));
  data.columns.forEach((col) => {
    const th = el('th', '', col);
    if (data.sort === col) th.append(el('span', 'arrow', data.sortDir > 0 ? ' ▲' : ' ▼'));
    th.addEventListener('click', () => {
      data.sortDir = data.sort === col ? -data.sortDir : 1;
      data.sort = col;
      renderTable();
    });
    head.append(th);
  });

  const body = $('data-body');
  body.innerHTML = '';
  if (!slice.length) {
    const tr = el('tr'), td = el('td', 'muted', 'No rows match these filters.');
    td.colSpan = data.columns.length + 1;
    tr.append(td); body.append(tr);
  }
  slice.forEach((row) => {
    const tr = el('tr');
    tr.append(el('td', 'rownum', row._row));
    data.columns.forEach((col) => {
      const td = el('td');
      const value = row[col] || '';
      const url = row[`${col}__url`] || (/^https?:\/\//.test(value) ? value : null);
      const pill = statusPill(col, value);
      if (pill) td.append(pill);
      else if (url) {
        const a = el('a', '', value.length > 60 ? value.slice(0, 57) + '…' : value);
        a.href = url; a.target = '_blank'; a.rel = 'noopener';
        td.append(a);
      } else td.textContent = value;
      td.title = value;
      tr.append(td);
    });
    body.append(tr);
  });

  $('page-info').textContent = `page ${data.page + 1} of ${pages}`;
  $('page-prev').disabled = data.page === 0;
  $('page-next').disabled = data.page >= pages - 1;
  $('data-count').textContent = rows.length === data.rows.length
    ? `${data.rows.length} rows` : `${rows.length} of ${data.rows.length} rows`;
}

function renderFacets() {
  const host = $('facets');
  host.innerHTML = '';
  facetableColumns(data.columns, data.rows).forEach((col) => {
    const group = el('div', 'filter-group');
    group.append(el('label', '', col));
    const sel = el('select');
    sel.append(new Option('All', ''));
    distinct(col).forEach(([v, n]) => sel.append(new Option(`${v} (${n})`, v)));
    sel.value = data.facets[col] || '';
    sel.addEventListener('change', () => { data.facets[col] = sel.value; data.page = 0; renderTable(); });
    group.append(sel);
    host.append(group);
  });
}

async function loadSheet(worksheet) {
  banner($('data-error'), '', '');
  $('data-count').textContent = 'decrypting…';
  try {
    if (!data.cache[worksheet]) {
      data.cache[worksheet] = await fetchEncrypted(worksheet, PASSWORD);
    }
    const payload = data.cache[worksheet];
    data.worksheet = worksheet;
    data.columns = payload.columns;
    data.rows = payload.rows;
    data.facets = {}; data.page = 0; data.sort = null;
    renderFacets();
    renderTable();
  } catch (err) {
    $('data-count').textContent = '';
    banner($('data-error'), 'err', `Could not load “${worksheet}”: ${err.message}`);
  }
}

$('search').addEventListener('input', (e) => { data.search = e.target.value; data.page = 0; renderTable(); });
$('btn-clear').addEventListener('click', () => {
  data.search = ''; $('search').value = ''; data.facets = {}; data.page = 0;
  renderFacets(); renderTable();
});
$('page-prev').addEventListener('click', () => { data.page--; renderTable(); });
$('page-next').addEventListener('click', () => { data.page++; renderTable(); });
$('page-size').addEventListener('change', (e) => { data.pageSize = +e.target.value; data.page = 0; renderTable(); });
$('sheet-select').addEventListener('change', (e) => loadSheet(e.target.value));

$('btn-export').addEventListener('click', () => {
  const rows = filteredRows();
  const esc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const csv = [data.columns.map(esc).join(',')]
    .concat(rows.map((r) => data.columns.map((c) => esc(r[c])).join(','))).join('\n');
  const a = el('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  a.download = `${data.worksheet}-filtered.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ── Runs ───────────────────────────────────────────────────────────────── */

/* The repository is public, so run history reads without any token. Starting a
   run does need write access, which a static page cannot hold without exposing
   it — so the button hands over to GitHub's own Run workflow dialog, where the
   viewer's existing GitHub session authorises it. */
const API = `https://api.github.com/repos/${CFG.repo}`;

function renderRunActions() {
  const grid = $('run-actions');
  grid.innerHTML = '';
  [
    ['Full pipeline', 'Enrich → career pages → job boards → validate links.', 'full'],
    ['Job-board scraping', 'Search LinkedIn for every keyword and append new jobs.', 'scrape-only'],
    ['Job validation', 'Re-check every job link and update its status.', 'validate-only'],
    ['Career pages', 'Scrape postings straight from company career pages.', 'career-pages-only'],
  ].forEach(([label, blurb, choice]) => {
    const card = el('div', 'task');
    card.append(el('h3', '', label), el('p', '', blurb));
    const row = el('div', 'run-row');
    const a = el('a', 'btn primary', 'Run on GitHub ↗');
    a.href = `https://github.com/${CFG.repo}/actions/workflows/${CFG.workflow}`;
    a.target = '_blank'; a.rel = 'noopener';
    a.title = `Opens the Run workflow dialog — choose "${choice}"`;
    row.append(a);
    card.append(row);
    grid.append(card);
  });
}

function fmtDuration(a, b) {
  if (!a || !b) return '—';
  const s = Math.round((new Date(b) - new Date(a)) / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

async function loadRuns() {
  banner($('runs-error'), '', '');
  try {
    const res = await fetch(`${API}/actions/runs?per_page=15`, {
      headers: { Accept: 'application/vnd.github+json' } });
    if (!res.ok) throw new Error(res.status === 403
      ? 'GitHub rate limit reached — try again in a few minutes.'
      : `HTTP ${res.status}`);
    const { workflow_runs = [] } = await res.json();
    const body = $('runs-body');
    body.innerHTML = '';
    if (!workflow_runs.length) {
      const tr = el('tr'), td = el('td', 'muted', 'No runs yet.');
      td.colSpan = 5; tr.append(td); body.append(tr);
    }
    workflow_runs.forEach((r) => {
      const state = r.status !== 'completed' ? 'running' : (r.conclusion || 'unknown');
      const cls = { success: 'ok', failure: 'err', cancelled: 'warn', running: 'warn' }[state] || 'neutral';
      const tr = el('tr');
      tr.append(el('td', 'faint', new Date(r.created_at).toLocaleString()),
                el('td', '', r.name || '—'));
      const st = el('td'); st.append(el('span', `pill ${cls}`, state)); tr.append(st);
      tr.append(el('td', 'faint', fmtDuration(r.run_started_at, r.updated_at)));
      const link = el('td');
      const a = el('a', '', 'view ↗');
      a.href = r.html_url; a.target = '_blank'; a.rel = 'noopener';
      link.append(a); tr.append(link);
      body.append(tr);
    });
    $('runs-updated').textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    banner($('runs-error'), 'err', `Could not read run history: ${err.message}`);
  }
}

$('btn-refresh-runs').addEventListener('click', loadRuns);

/* ── Tabs & boot ────────────────────────────────────────────────────────── */

document.querySelectorAll('nav button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach((b) => {
      const on = b === btn;
      b.setAttribute('aria-selected', String(on));
      $(b.dataset.panel).hidden = !on;
    });
    if (btn.dataset.panel === 'panel-runs') loadRuns();
  });
});

async function boot() {
  const sel = $('sheet-select');
  sel.innerHTML = '';
  const usable = (MANIFEST.worksheets || []).filter((w) => !w.error && w.row_count);
  usable.forEach((w) => sel.append(new Option(`${w.name} — ${w.row_count} rows`, w.name)));

  const captured = new Date(MANIFEST.captured_at);
  const ageHours = (Date.now() - captured) / 36e5;
  const chip = $('captured');
  chip.textContent = `data from ${captured.toLocaleString()}`;
  chip.className = `pill ${ageHours > 24 * 8 ? 'stale' : 'neutral'}`;
  chip.title = ageHours > 24 * 8
    ? 'Older than the weekly schedule — a run may have failed.'
    : 'Captured by the most recent successful run.';

  renderRunActions();
  if (usable.length) await loadSheet(usable[0].name);
  else banner($('data-error'), 'warn', 'The last run published no readable worksheets.');
}

const saved = sessionStorage.getItem('dash-pw');
if (saved) {
  unlock(saved).catch(() => sessionStorage.removeItem('dash-pw'));
}
