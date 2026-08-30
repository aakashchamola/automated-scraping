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

async function rememberKey(key, token) {
  try {
    await idbPut('data-key', key);
    await idbPut('session-token', token || null);
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
    SESSION_TOKEN = (await idbGet('session-token')) || null;
    return (await idbGet('data-key')) || null;
  } catch {
    return null;
  }
}

async function forgetKey() {
  try {
    localStorage.removeItem(EXPIRY_KEY);
    await idbPut('data-key', null);
    await idbPut('session-token', null);
    SESSION_TOKEN = null;
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
/* Issued by the Settings service after a correct password. The page remembers
   this instead of the password, so staying signed in never means keeping what
   someone typed — and changing the password revokes it server-side. */
let SESSION_TOKEN = null;

/* Sign in.

   With a Settings service configured, the password is checked there and the
   service hands back two things: the key the published files were encrypted
   with, and a session token. The password is never the encryption key, which
   is exactly what makes it changeable from this page — changing an encryption
   key would strand every already-published file.

   Without a service, the password IS the key (the original arrangement), and
   the page still works standalone — it just cannot save settings or change
   the password. */
async function unlock(password, remember) {
  const payload = await fetchPayload('index');
  let key = null;
  SESSION_TOKEN = null;

  if (SETTINGS_URL) {
    let auth = null;
    try {
      auth = await jsonp(
        `${SETTINGS_URL}?action=auth&password=${encodeURIComponent(password)}`);
    } catch (err) {
      auth = { ok: false, error: err.message, unreachable: true };
    }
    if (auth.ok) {
      key = await deriveKey(auth.dataKey, payload.kdf);
      SESSION_TOKEN = auth.token;
    } else if (auth.error && /wrong password|no password sent/i.test(auth.error)) {
      // The service is working and says no. Believe it.
      throw new AuthError('Wrong password.');
    }
    // Anything else — service unreachable, properties not configured yet, a
    // deployment mid-change — must not brick the page. Fall through and try
    // the password as the key, which is how the site works with no service at
    // all. Settings then stays read-only rather than the whole site being shut.
  }

  if (!key) key = await deriveKey(password, payload.kdf);

  MANIFEST = await decryptWith(key, payload);      // throws if the key is wrong
  KEY = key;
  if (remember) await rememberKey(key, SESSION_TOKEN);
  $('gate').hidden = true;
  $('app').hidden = false;
  await boot();
}

/* A refusal from the service is a wrong password, not a broken page — the
   login form should say so plainly rather than showing a decryption error. */
class AuthError extends Error {
  constructor(message) { super(message); this.name = 'AuthError'; }
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
    err.textContent = (ex.name === 'OperationError' || ex.name === 'AuthError')
      ? (ex.name === 'AuthError' ? ex.message : 'Wrong password.')
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

/* ── Settings ─────────────────────────────────────────────────────────────
   The Settings worksheet is the source of truth, and this panel edits it.
   Saving goes through the Apps Script Web App, because a static page holds no
   Google credentials. Two constraints shape that exchange, both established
   the hard way:

     - A Web App's /exec response carries no Access-Control-Allow-Origin, so a
       normal fetch that reads the response dies. Saves are sent no-cors:
       fire-and-forget, response unreadable.
     - Since the save's own answer cannot be read, the panel confirms by
       reading back over JSONP (a <script> tag), which CORS does not touch.

   So a save is: POST blind, then re-read and show what the sheet actually
   says. If the read-back disagrees, the user is told the save did not land
   rather than being shown an optimistic success. */

const SETTINGS_URL = (CFG.settingsWebApp || '').trim();
let SETTINGS_ROWS = null;
const pending = {};

function jsonp(url, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const name = `__jsonp_${Math.random().toString(36).slice(2)}`;
    const script = document.createElement('script');
    const done = (fn) => {
      delete window[name];
      script.remove();
      clearTimeout(timer);
      fn();
    };
    const timer = setTimeout(
      () => done(() => reject(new Error('the Settings service did not respond'))), timeoutMs);
    window[name] = (payload) => done(() => resolve(payload));
    script.onerror = () => done(() => reject(new Error('could not reach the Settings service')));
    script.src = `${url}${url.includes('?') ? '&' : '?'}callback=${name}`;
    document.body.append(script);
  });
}

async function readLiveSettings() {
  const payload = await jsonp(
    `${SETTINGS_URL}?token=${encodeURIComponent(SESSION_TOKEN || '')}`);
  if (!payload.ok) {
    // A revoked token means the password was changed elsewhere.
    if (payload.signedOut) throw new AuthError('this session is no longer valid');
    throw new Error(payload.error || 'the Settings service refused the request');
  }
  return payload.settings;
}

function markDirty() {
  const n = Object.keys(pending).length;
  $('settings-save').disabled = n === 0;
  $('settings-discard').disabled = n === 0;
  $('settings-status').textContent = n ? `${n} unsaved change${n > 1 ? 's' : ''}` : '';
}

function track(path, value, original, node) {
  if (String(value) === String(original)) delete pending[path];
  else pending[path] = String(value);
  node.classList.toggle('changed', path in pending);
  markDirty();
}

function settingControl(row) {
  const type = (row.Type || 'text').trim();
  const original = (row.Value || '').trim();
  const options = (row.Options || '').split('|').map((o) => o.trim()).filter(Boolean);
  const node = el('div', 'field');

  const lbl = el('div', 'lbl');
  lbl.append(el('div', 'mono', row.Setting));
  if (row.Description) lbl.append(el('div', 'help', row.Description));
  node.append(lbl);

  const ctrl = el('div', 'ctrl');
  let input;

  if (type === 'bool') {
    const wrap = el('label', 'switch');
    input = el('input');
    input.type = 'checkbox';
    input.checked = /^(true|yes|1|on)$/i.test(original);
    input.addEventListener('change',
      () => track(row.Setting, input.checked ? 'TRUE' : 'FALSE', original || 'FALSE', node));
    wrap.append(input, el('span', 'faint', 'on / off'));
    ctrl.append(wrap);
  } else if (type === 'select' && options.length) {
    input = el('select');
    options.forEach((o) => input.append(new Option(o, o)));
    if (!options.includes(original) && original) input.append(new Option(original, original));
    input.value = original;
    input.addEventListener('change', () => track(row.Setting, input.value, original, node));
    ctrl.append(input);
  } else if (type === 'multiselect' && options.length) {
    const chosen = new Set(original.split(',').map((v) => v.trim()).filter(Boolean));
    const box = el('div', 'checks');
    options.forEach((option) => {
      const tag = el('label', chosen.has(option) ? 'on' : '');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = chosen.has(option);
      cb.addEventListener('change', () => {
        cb.checked ? chosen.add(option) : chosen.delete(option);
        tag.classList.toggle('on', cb.checked);
        track(row.Setting,
              options.filter((o) => chosen.has(o)).join(', '), original, node);
      });
      tag.append(cb, document.createTextNode(option));
      box.append(tag);
    });
    ctrl.append(box);
  } else {
    input = el('input');
    input.type = (type === 'int' || type === 'float') ? 'number' : 'text';
    if (type === 'float') input.step = '0.1';
    input.value = original;
    input.addEventListener('input', () => track(row.Setting, input.value, original, node));
    ctrl.append(input);
  }

  if (row.Options) ctrl.append(el('div', 'help', `options: ${row.Options}`));
  node.append(ctrl);
  return node;
}

function editableNow() { return Boolean(SETTINGS_URL && SESSION_TOKEN); }

function renderSettings() {
  const host = $('settings-body');
  host.innerHTML = '';
  for (const key of Object.keys(pending)) delete pending[key];

  if (!SETTINGS_ROWS || !SETTINGS_ROWS.length) {
    host.append(el('p', 'muted',
      'No Settings worksheet was published. Run “Refresh this dashboard”.'));
    return;
  }

  // A URL alone is not enough — without a token the service will refuse
  // every write, and offering controls that cannot save is worse than
  // showing the values plainly.
  const editable = Boolean(SETTINGS_URL && SESSION_TOKEN);
  const groups = new Map();
  SETTINGS_ROWS.forEach((row) => {
    const group = row.Group || 'Other';
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(row);
  });
  groups.forEach((rows, group) => {
    const box = el('div', 'setting-group');
    box.append(el('h3', '', group));
    rows.forEach((row) => box.append(
      editable ? settingControl(row) : readOnlyRow(row)));
    host.append(box);
  });
  $('settings-savebar').hidden = !editable;
  $('password-card').hidden = !editable;
  markDirty();
}

function readOnlyRow(row) {
  const node = el('div', 'field');
  const lbl = el('div', 'lbl');
  lbl.append(el('div', 'mono', row.Setting));
  if (row.Description) lbl.append(el('div', 'help', row.Description));
  const ctrl = el('div', 'ctrl');
  const value = (row.Value || '').trim();
  const shown = el('div', 'mono', value || '(default)');
  if (!value) shown.classList.add('faint');
  ctrl.append(shown);
  if (row.Options) ctrl.append(el('div', 'help', `options: ${row.Options}`));
  node.append(lbl, ctrl);
  return node;
}

/* Changing the login password. It is only a Script Property on the service —
   nothing is encrypted with it — so this takes effect immediately and does not
   require anything to be republished. Every existing session is revoked, this
   one included, so the change is visibly real rather than silent. */
async function changePassword() {
  const current = $('pw-current').value;
  const next = $('pw-new').value;
  const again = $('pw-repeat').value;
  const status = $('pw-status');

  if (next !== again) { status.textContent = 'The new passwords do not match.'; return; }
  if (next.length < 8) { status.textContent = 'Use at least 8 characters.'; return; }

  $('pw-change').disabled = true;
  status.textContent = 'changing…';
  try {
    await fetch(SETTINGS_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({
        token: SESSION_TOKEN, action: 'changePassword',
        currentPassword: current, newPassword: next,
      }),
    });

    // The write's own answer is unreadable, so prove it by signing in with the
    // new password. Success also re-establishes a session, since the change
    // revoked the old one.
    const check = await jsonp(
      `${SETTINGS_URL}?action=auth&password=${encodeURIComponent(next)}`);
    if (check.ok) {
      SESSION_TOKEN = check.token;
      if (remainingDays()) await rememberKey(KEY, SESSION_TOKEN);
      ['pw-current', 'pw-new', 'pw-repeat'].forEach((id) => { $(id).value = ''; });
      status.textContent = '';
      banner($('settings-error'), 'ok',
        'Password changed. Everyone signed in elsewhere will have to sign in again ' +
        'with the new one.');
    } else {
      status.textContent = '';
      banner($('settings-error'), 'err',
        `The password was not changed: ${check.error || 'the service refused it'}.`);
    }
  } catch (err) {
    status.textContent = '';
    banner($('settings-error'), 'err', `Could not change the password: ${err.message}`);
  } finally {
    $('pw-change').disabled = false;
  }
}

/* ── Keywords ─────────────────────────────────────────────────────────────
   A plain list, so it is edited as one and saved as one: the service replaces
   the whole Search Term column. That makes add, edit, reorder and delete a
   single operation rather than a row-by-row diff that could half-apply. */

let KEYWORDS = null;        // as the sheet has them
let KEYWORD_DRAFT = null;   // as the page has them

function keywordsDirty() {
  return Boolean(KEYWORDS && KEYWORD_DRAFT) &&
         JSON.stringify(KEYWORDS) !== JSON.stringify(KEYWORD_DRAFT);
}

function markKeywordsDirty() {
  const dirty = keywordsDirty();
  $('keywords-save').disabled = !dirty;
  $('keywords-discard').disabled = !dirty;
  $('keywords-status').textContent = dirty
    ? `${KEYWORD_DRAFT.filter(Boolean).length} keyword(s), unsaved`
    : '';
}

function renderKeywords() {
  const host = $('keywords-list');
  host.innerHTML = '';
  const editable = editableNow();
  $('keywords-savebar').hidden = !editable;

  if (!KEYWORD_DRAFT || !KEYWORD_DRAFT.length) {
    host.append(el('p', 'muted', 'No keywords yet.'));
  }
  (KEYWORD_DRAFT || []).forEach((term, index) => {
    const row = el('div', 'kw-row');
    row.append(el('span', 'idx', String(index + 1)));
    if (editable) {
      const input = el('input');
      input.type = 'text';
      input.value = term;
      input.addEventListener('input', () => {
        KEYWORD_DRAFT[index] = input.value;
        row.classList.toggle('changed', input.value !== (KEYWORDS[index] || ''));
        markKeywordsDirty();
      });
      const remove = el('button', 'btn sm danger', 'Remove');
      remove.addEventListener('click', () => {
        KEYWORD_DRAFT.splice(index, 1);
        renderKeywords();
        markKeywordsDirty();
      });
      row.append(input, remove);
    } else {
      row.append(el('span', '', term));
    }
    host.append(row);
  });

  $('keywords-count').textContent =
    `${(KEYWORD_DRAFT || []).filter(Boolean).length} keywords`;
  $('keyword-new').disabled = !editable;
  $('keyword-add').disabled = !editable;
  markKeywordsDirty();
}

async function loadKeywords() {
  banner($('keywords-error'), '', '');
  try {
    if (editableNow()) {
      const payload = await jsonp(
        `${SETTINGS_URL}?action=keywords&token=${encodeURIComponent(SESSION_TOKEN)}`);
      if (!payload.ok) throw new Error(payload.error || 'the service refused the request');
      KEYWORDS = payload.keywords.keywords || [];
    } else {
      // No service: fall back to the published snapshot, read-only.
      if (!data.cache.Keywords) data.cache.Keywords = await fetchEncrypted('Keywords', KEY);
      const column = data.cache.Keywords.columns.find((c) => /search term/i.test(c))
                     || data.cache.Keywords.columns[0];
      KEYWORDS = data.cache.Keywords.rows
        .map((r) => (r[column] || '').trim()).filter(Boolean);
    }
    KEYWORD_DRAFT = KEYWORDS.slice();
    renderKeywords();
    if (!editableNow()) {
      banner($('keywords-error'), 'warn',
        'Read-only: the Settings service is not available for this session.');
    }
  } catch (err) {
    banner($('keywords-error'), 'err', `Could not load keywords: ${err.message}`);
  }
}

async function saveKeywords() {
  const list = KEYWORD_DRAFT.map((t) => t.trim()).filter(Boolean);
  if (!list.length) {
    banner($('keywords-error'), 'err',
      'Refusing to save an empty list — a scrape with no keywords finds nothing.');
    return;
  }
  $('keywords-save').disabled = true;
  $('keywords-status').textContent = 'saving…';
  banner($('keywords-error'), '', '');
  try {
    await fetch(SETTINGS_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ token: SESSION_TOKEN, action: 'saveKeywords', keywords: list }),
    });
    $('keywords-status').textContent = 'confirming…';
    const payload = await jsonp(
      `${SETTINGS_URL}?action=keywords&token=${encodeURIComponent(SESSION_TOKEN)}`);
    if (!payload.ok) throw new Error(payload.error || 'could not read back');
    KEYWORDS = payload.keywords.keywords || [];
    KEYWORD_DRAFT = KEYWORDS.slice();
    renderKeywords();
    const same = JSON.stringify(KEYWORDS) === JSON.stringify(list);
    banner($('keywords-error'), same ? 'ok' : 'err', same
      ? `Saved ${KEYWORDS.length} keywords. The next scrape will use them.`
      : 'The sheet does not match what was sent — check the Keywords tab.');
  } catch (err) {
    banner($('keywords-error'), 'err', `Could not confirm the save: ${err.message}`);
  } finally {
    $('keywords-status').textContent = '';
    markKeywordsDirty();
  }
}

async function saveSettings() {
  const changes = { ...pending };
  const count = Object.keys(changes).length;
  if (!count) return;

  const button = $('settings-save');
  button.disabled = true;
  $('settings-status').textContent = 'saving…';
  banner($('settings-error'), '', '');

  try {
    // Blind write — the response is unreadable cross-origin by design.
    await fetch(SETTINGS_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ token: SESSION_TOKEN, updates: changes }),
    });

    // Confirm against what the sheet now actually says.
    $('settings-status').textContent = 'confirming…';
    const live = await readLiveSettings();
    SETTINGS_ROWS = live.rows;

    const byPath = {};
    live.rows.forEach((row) => { byPath[row.Setting] = String(row.Value || '').trim(); });
    const missed = Object.entries(changes)
      .filter(([path, want]) => byPath[path] !== String(want).trim())
      .map(([path]) => path);

    renderSettings();
    if (missed.length) {
      banner($('settings-error'), 'err',
        `${count - missed.length} of ${count} saved. These did not stick: ${missed.join(', ')}.`);
    } else {
      banner($('settings-error'), 'ok',
        `Saved ${count} setting${count === 1 ? '' : 's'} to the sheet. ` +
        'The next run will use them.');
    }
  } catch (err) {
    banner($('settings-error'), 'err',
      `Could not confirm the save: ${err.message}. Check the Settings tab before retrying.`);
  } finally {
    markDirty();
  }
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
    if (btn.dataset.panel === 'panel-keywords') loadKeywords();
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

async function loadSettings() {
  banner($('settings-error'), '', '');
  const link = $('settings-edit');
  link.href = `https://docs.google.com/spreadsheets/d/${MANIFEST.spreadsheet_id || ''}`;
  link.hidden = !MANIFEST.spreadsheet_id;

  try {
    if (SETTINGS_URL && SESSION_TOKEN) {
      // Live from the sheet, so the panel shows the truth rather than whatever
      // the last publish froze.
      $('settings-body').innerHTML = '<p class="muted">reading the sheet…</p>';
      SETTINGS_ROWS = (await readLiveSettings()).rows;
    } else {
      if (!data.cache.Settings) data.cache.Settings = await fetchEncrypted('Settings', KEY);
      SETTINGS_ROWS = data.cache.Settings.rows;
    }
    renderSettings();
    if (!editableNow()) {
      banner($('settings-error'), 'warn',
        'Read-only: the Settings service is not available for this session, so this ' +
        'shows the last published snapshot. Edit in Google Sheets, or check the service ' +
        'is deployed with DASHBOARD_PASSWORD and DASHBOARD_DATA_KEY set.');
    }
  } catch (err) {
    // A live read failing must not leave an empty panel — fall back.
    try {
      if (!data.cache.Settings) data.cache.Settings = await fetchEncrypted('Settings', KEY);
      SETTINGS_ROWS = data.cache.Settings.rows;
      renderSettings();
      $('settings-savebar').hidden = true;
      banner($('settings-error'), 'warn',
        `Showing the last published snapshot — the live sheet could not be read (${err.message}).`);
    } catch (inner) {
      banner($('settings-error'), 'err', `Could not load Settings: ${inner.message}`);
    }
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

$('settings-save').addEventListener('click', saveSettings);
$('settings-discard').addEventListener('click', () => { renderSettings(); });

$('pw-change').addEventListener('click', changePassword);

$('keywords-save').addEventListener('click', saveKeywords);
$('keywords-discard').addEventListener('click', () => {
  KEYWORD_DRAFT = KEYWORDS.slice();
  renderKeywords();
});
$('keyword-add').addEventListener('click', () => {
  const box = $('keyword-new');
  const term = box.value.trim();
  if (!term) return;
  if (KEYWORD_DRAFT.some((t) => t.toLowerCase() === term.toLowerCase())) {
    banner($('keywords-error'), 'warn', `"${term}" is already in the list.`);
    return;
  }
  KEYWORD_DRAFT.push(term);
  box.value = '';
  renderKeywords();
});
$('keyword-new').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('keyword-add').click(); }
});
