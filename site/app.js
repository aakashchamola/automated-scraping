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

const REMEMBER_DAYS = 10;
const EXPIRY_KEY = 'dash-key-expires';
const DB_NAME = 'dashboard-auth';
const STORE = 'keys';

async function deriveKey(password, kdf) {
  const material = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt: b64(kdf.salt), iterations: kdf.iterations, hash: kdf.hash },
    material,
    { name: 'AES-GCM', length: 256 },
    /* extractable */ false,          // see rememberKey()
    ['decrypt']);
}

/* ── Staying signed in ────────────────────────────────────────────────────
   The derived CryptoKey is stored, never the password. Marking it
   non-extractable means the browser will decrypt with it but will not hand its
   bytes back to any script, so a cached session cannot give up a password the
   viewer may well have reused somewhere else. IndexedDB is used because it is
   the only browser store that can hold a live CryptoKey; localStorage would
   force us to keep the password as text. */

function idb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(key, value) {
  const db = await idb();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}

async function idbGet(key) {
  const db = await idb();
  const value = await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly');
    const req = tx.objectStore(STORE).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  db.close();
  return value;
}

async function rememberKey(key) {
  try {
    await idbPut('data-key', key);
    localStorage.setItem(EXPIRY_KEY, String(Date.now() + REMEMBER_DAYS * 864e5));
  } catch (e) {
    // Private browsing and blocked site data both land here. Staying signed in
    // is a convenience; losing it must not stop the page working.
    console.warn('could not remember this session:', e.name);
  }
}

async function recallKey() {
  try {
    const expires = Number(localStorage.getItem(EXPIRY_KEY) || 0);
    if (!expires || Date.now() > expires) {
      await forgetKey();
      return null;
    }
    return (await idbGet('data-key')) || null;
  } catch {
    return null;
  }
}

async function forgetKey() {
  try {
    localStorage.removeItem(EXPIRY_KEY);
    await idbPut('data-key', null);
  } catch { /* nothing to clean up */ }
}

function remainingDays() {
  const expires = Number(localStorage.getItem(EXPIRY_KEY) || 0);
  return expires ? Math.max(0, Math.ceil((expires - Date.now()) / 864e5)) : 0;
}

/* Decrypt one published file with an already-derived key. Throws if the key is
   wrong, which the caller treats as a failed or stale login. */
async function decryptWith(key, payload) {
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b64(payload.cipher.iv) }, key, b64(payload.data));
  return JSON.parse(new TextDecoder().decode(plain));
}

async function fetchPayload(name) {
  const res = await fetch(`data/${name}.enc.json`, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`could not load ${name} (HTTP ${res.status})`);
  return res.json();
}

async function fetchEncrypted(name, key) {
  return decryptWith(key, await fetchPayload(name));
}

/* ── Login gate ─────────────────────────────────────────────────────────── */

let KEY = null;          // the derived CryptoKey; the password is never kept
let MANIFEST = null;

async function unlock(password, remember) {
  // Derive from index's salt — every file in a publish shares it, so one
  // derivation covers the whole dashboard.
  const payload = await fetchPayload('index');
  const key = await deriveKey(password, payload.kdf);
  MANIFEST = await decryptWith(key, payload);           // throws on a bad password
  KEY = key;
  if (remember) await rememberKey(key);
  $('gate').hidden = true;
  $('app').hidden = false;
  await boot();
}

/* Resume a remembered session. A key that no longer decrypts means the
   password was rotated and the data republished, so the stale key is discarded
   and the viewer is asked again rather than shown a broken page. */
async function resume() {
  const key = await recallKey();
  if (!key) return false;
  try {
    MANIFEST = await fetchEncrypted('index', key);
    KEY = key;
    $('gate').hidden = true;
    $('app').hidden = false;
    await boot();
    return true;
  } catch {
    await forgetKey();
    return false;
  }
}

$('gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('gate-go');
  const err = $('gate-err');
  btn.disabled = true;
  btn.textContent = 'Unlocking…';
  err.textContent = '';
  try {
    await unlock($('gate-pw').value, $('gate-remember').checked);
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

$('btn-lock').addEventListener('click', async () => {
  await forgetKey();
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
      data.cache[worksheet] = await fetchEncrypted(worksheet, KEY);
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

/* Every option the workflow accepts. The `choice` is the exact value to pick in
   GitHub's Run workflow dialog, so the card and the dialog cannot drift apart
   without it being obvious. */
const AUTOMATIONS = [
  ['Full pipeline', 'Enrich companies → career pages → job boards → validate links.', 'full', true],
  ['Job-board scraping', 'Search LinkedIn for every keyword and append new jobs.', 'scrape-only'],
  ['Career pages', 'Scrape postings straight from company career pages (Greenhouse, Lever, Ashby, Workday).', 'career-pages-only'],
  ['Company enrichment', 'Fill in employee count, career page and LinkedIn URL.', 'enrich-only'],
  ['Job validation', 'Re-check every job link and mark it Active / Expired / Removed / Unknown — and delete rows if that is switched on in Settings.', 'validate-only', true],
  ['Data mismatch flagging', 'Flag cells where the scraped data disagrees with the Company sheet.', 'mismatch-only'],
  ['Organisation classification', 'Sort every organisation into Company / University / Government / Hospital / Nonprofit / Research.', 'classify-only'],
  ['Pagination analysis', 'Measure how many jobs sit behind “See More Jobs”. Read-only.', 'pagination-only'],
  ['Clear stale company rows', 'Remove company rows no longer referenced by Jobs. Dry-run unless confirmed in its config.', 'cleanup-rows'],
  ['Refresh this dashboard', 'Re-export and republish the data. Writes nothing to the sheet.', 'publish-only'],
];

function renderRunActions() {
  const grid = $('run-actions');
  grid.innerHTML = '';
  AUTOMATIONS.forEach(([label, blurb, choice, primary]) => {
    const card = el('div', `task${primary ? ' primary' : ''}`);
    const h = el('h3', '', label);
    if (primary) h.append(el('span', 'pill info', 'main'));
    card.append(h, el('p', '', blurb));
    card.append(el('div', 'detail', `Run workflow → “Which part of the pipeline to run” → ${choice}`));
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

/* ── Settings (read-only) ─────────────────────────────────────────────────
   The page has no credentials, so it can show the configuration but never
   change it. The Settings worksheet is where it is edited; this renders what
   that sheet currently says, grouped the way the sheet groups it. */

function renderSettings() {
  const host = $('settings-body');
  host.innerHTML = '';
  const payload = data.cache.Settings;
  if (!payload) {
    host.append(el('p', 'muted',
      'The last run published no Settings worksheet. Run “Refresh this dashboard”.'));
    return;
  }
  const groups = new Map();
  payload.rows.forEach((row) => {
    const g = row.Group || 'Other';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(row);
  });
  groups.forEach((rows, group) => {
    const box = el('div', 'setting-group');
    box.append(el('h3', '', group));
    rows.forEach((row) => {
      const field = el('div', 'field');
      const lbl = el('div', 'lbl');
      lbl.append(el('div', 'mono', row.Setting));
      if (row.Description) lbl.append(el('div', 'help', row.Description));
      const ctrl = el('div', 'ctrl');
      const value = (row.Value || '').trim();
      const shown = el('div', 'mono', value || '(default)');
      if (!value) shown.classList.add('faint');
      ctrl.append(shown);
      if (row.Options) ctrl.append(el('div', 'help', `options: ${row.Options}`));
      field.append(lbl, ctrl);
      box.append(field);
    });
    host.append(box);
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
    if (btn.dataset.panel === 'panel-settings') loadSettings();
  });
});

async function loadSettings() {
  banner($('settings-error'), '', '');
  try {
    if (!data.cache.Settings) {
      data.cache.Settings = await fetchEncrypted('Settings', KEY);
    }
    renderSettings();
    const link = $('settings-edit');
    link.href = `https://docs.google.com/spreadsheets/d/${MANIFEST.spreadsheet_id || ''}`;
    link.hidden = !MANIFEST.spreadsheet_id;
  } catch (err) {
    banner($('settings-error'), 'err', `Could not load Settings: ${err.message}`);
  }
}

async function boot() {
  const sel = $('sheet-select');
  sel.innerHTML = '';
  // Settings has its own panel; it is not a data table.
  const usable = (MANIFEST.worksheets || [])
    .filter((w) => !w.error && w.row_count && w.name !== 'Settings');
  usable.forEach((w) => sel.append(new Option(`${w.name} — ${w.row_count} rows`, w.name)));

  const captured = new Date(MANIFEST.captured_at);
  const ageHours = (Date.now() - captured) / 36e5;
  const chip = $('captured');
  chip.textContent = `data from ${captured.toLocaleString()}`;
  chip.className = `pill ${ageHours > 24 * 8 ? 'stale' : 'neutral'}`;
  chip.title = ageHours > 24 * 8
    ? 'Older than the weekly schedule — a run may have failed.'
    : 'Captured by the most recent successful run.';

  const days = remainingDays();
  $('btn-lock').title = days
    ? `Signed in for ${days} more day${days === 1 ? '' : 's'}. Lock to sign out now.`
    : 'Sign out';

  renderRunActions();
  if (usable.length) await loadSheet(usable[0].name);
  else banner($('data-error'), 'warn', 'The last run published no readable worksheets.');
}

resume().catch(() => { /* fall through to the login form */ });
