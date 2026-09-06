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
const DB_NAME = 'dashboard-auth';
const STORE = 'keys';
/* One record holding {projectId: {name, key, token, expires}} — see
   "Staying signed in, per project" below. */
const SESSIONS_KEY = 'sessions';

async function deriveKey(password, kdf) {
  const material = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt: b64(kdf.salt), iterations: kdf.iterations, hash: kdf.hash },
    material,
    { name: 'AES-GCM', length: 256 },
    /* extractable */ false,          // see rememberSession()
    ['decrypt']);
}

/* ── Staying signed in, per project ───────────────────────────────────────
   The derived CryptoKey is stored, never the password. Marking it
   non-extractable means the browser will decrypt with it but will not hand its
   bytes back to any script, so a cached session cannot give up a password the
   viewer may well have reused somewhere else. IndexedDB is used because it is
   the only browser store that can hold a live CryptoKey; localStorage would
   force us to keep the password as text.

   Sessions are kept one per project, which is what makes switching instant:
   unlocking a second project adds to this rather than replacing it, and moving
   between projects you have already unlocked never asks for a password again.
   Each has its own expiry, so they lapse independently. */

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

/* Every remembered session, expired ones dropped. A CryptoKey survives being
   nested inside a plain object because IndexedDB stores by structured clone,
   which handles keys — so one record can hold them all. */
async function loadSessions() {
  try {
    const all = (await idbGet(SESSIONS_KEY)) || {};
    const now = Date.now();
    let changed = false;
    for (const id of Object.keys(all)) {
      if (!all[id] || !all[id].key || (all[id].expires || 0) < now) {
        delete all[id];
        changed = true;
      }
    }
    if (changed) await idbPut(SESSIONS_KEY, all);
    return all;
  } catch (e) {
    // Private browsing and blocked site data both land here. Staying signed in
    // is a convenience; losing it must not stop the page working.
    console.warn('could not read remembered sessions:', e.name);
    return {};
  }
}

async function rememberSession(project, name, key, token) {
  try {
    const all = await loadSessions();
    all[project] = { name, key, token: token || null,
                     expires: Date.now() + REMEMBER_DAYS * 864e5,
                     usedAt: Date.now() };
    await idbPut(SESSIONS_KEY, all);
  } catch (e) {
    console.warn('could not remember this session:', e.name);
  }
}

/* Bump a session to the front without re-deriving anything, so reopening the
   page returns to the project you were last actually looking at. */
async function touchSession(project) {
  try {
    const all = await loadSessions();
    if (all[project]) { all[project].usedAt = Date.now(); await idbPut(SESSIONS_KEY, all); }
  } catch { /* not worth failing a page load over */ }
}

async function forgetSession(project) {
  try {
    const all = await loadSessions();
    delete all[project];
    await idbPut(SESSIONS_KEY, all);
  } catch { /* nothing to clean up */ }
}

async function forgetAllSessions() {
  try { await idbPut(SESSIONS_KEY, {}); } catch { /* nothing to clean up */ }
}

function remainingDays() {
  const expires = (SESSION && SESSION.expires) || 0;
  return expires ? Math.max(0, Math.ceil((expires - Date.now()) / 864e5)) : 0;
}

/* Decrypt one published file with an already-derived key. Throws if the key is
   wrong, which the caller treats as a failed or stale login. */
async function decryptWith(key, payload) {
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b64(payload.cipher.iv) }, key, b64(payload.data));
  return JSON.parse(new TextDecoder().decode(plain));
}

/* Each project's files live in their own directory, encrypted under that
   project's own key, so a session for one decrypts nothing belonging to
   another. There is deliberately no index of the directories: the page is told
   which one it may read by the service that checked the password. */
async function fetchPayloadFor(project, name) {
  const res = await fetch(`data/${project}/${name}.enc.json`, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`could not load ${name} (HTTP ${res.status})`);
  return res.json();
}

async function fetchPayload(name) {
  if (!PROJECT) throw new Error('no project is open');
  return fetchPayloadFor(PROJECT.id, name);
}

async function fetchEncrypted(name, key) {
  return decryptWith(key, await fetchPayload(name));
}

/* ── Reading the sheet as it is now ───────────────────────────────────────
   The published files above are a snapshot: a scheduled job exported every
   tab, encrypted it, and committed it. That made the dashboard fast and it
   made it WRONG between runs — it showed whatever the last successful publish
   contained, and it could only stay current while a CI runner kept publishing.

   Now that the service can serve a tab directly, the page reads what the
   spreadsheet says at the moment you look. The snapshot is kept as a fallback
   for a project that still has published files and no reachable service, but
   live is preferred: stale data presented as current is worse than slow.

   The shapes below match export_snapshot.py exactly, so everything downstream
   — the table, the facets, the CSV export — cannot tell where a payload came
   from. */

const HYPERLINK = /^=HYPERLINK\(\s*"((?:[^"]|"")*)"\s*[,;]\s*"((?:[^"]|"")*)"\s*\)$/i;

/* A HYPERLINK formula split into its text and its URL, mirroring _unwrap in
   web/sheets_data.py. Most cells are neither, and pass straight through. */
function unwrapCell(value) {
  const match = HYPERLINK.exec(String(value == null ? '' : value).trim());
  if (!match) return [String(value == null ? '' : value), null];
  return [match[1].replace(/""/g, '"'), match[2].replace(/""/g, '"')];
}

/* Raw rows into the record shape the panel expects. Blank rows are dropped and
   _row keeps each record's real position in the sheet, which is what makes a
   row in the table findable in the spreadsheet itself. */
function shapeRows(raw, worksheet, capturedAt) {
  const header = (raw[0] || []).map((h) => String(h).trim());
  while (header.length && !header[header.length - 1]) header.pop();
  const columns = header.filter(Boolean);
  const rows = [];
  for (let i = 1; i < raw.length; i++) {
    const record = { _row: i + 1 };
    let blank = true;
    header.forEach((column, index) => {
      if (!column) return;
      const [text, url] = unwrapCell(raw[i][index]);
      record[column] = text;
      if (url) record[`${column}__url`] = url;
      if (text.trim()) blank = false;
    });
    if (!blank) rows.push(record);
  }
  return { worksheet, columns, rows, row_count: rows.length,
           captured_at: capturedAt || new Date().toISOString() };
}

async function liveManifest(token) {
  const view = await jsonp(
    `${SETTINGS_URL}?action=tabs&token=${encodeURIComponent(token)}`);
  /* A dead session and a deployment that cannot serve tabs both come back as
     a refusal, and they must NOT be treated alike: the first has to send the
     viewer back to the password, the second is merely a page newer than the
     script and must not stop anyone signing in. */
  if (view.signedOut) {
    const dead = new Error(view.error || 'that session is no longer valid');
    dead.signedOut = true;
    throw dead;
  }
  if (!view.ok) throw new Error(view.error || 'the service refused the request');
  /* A deployment older than this page answers an action it does not know by
     reading the Settings tab instead — a perfectly valid reply to a different
     question. Taken at face value it produces an empty dashboard rather than
     falling back to the published snapshot, so the reply has to be checked for
     what was actually asked for. */
  if (!Array.isArray(view.worksheets)) {
    throw new Error('the Settings service does not serve tabs yet');
  }
  return {
    captured_at: view.capturedAt,
    spreadsheet_id: view.spreadsheetId || '',
    worksheets: view.worksheets || [],
    live: true,
  };
}

async function liveWorksheet(name) {
  const view = await jsonp(`${SETTINGS_URL}?action=rows` +
    `&token=${encodeURIComponent(SESSION_TOKEN)}` +
    `&worksheet=${encodeURIComponent(name)}`);
  if (!view.ok) throw new Error(view.error || 'the service refused the request');
  if (!Array.isArray(view.rows)) {
    throw new Error(`the Settings service did not return rows for '${name}'`);
  }
  return shapeRows(view.rows, name, new Date().toISOString());
}

/* ── Signing in, and which project you are in ─────────────────────────────
   The password selects the project: the Settings service is asked which one it
   unlocks, so the landing page never has to list them and opening the URL
   discloses no project names. */

let KEY = null;          // the derived CryptoKey; the password is never kept
let MANIFEST = null;
let PROJECT = null;      // { id, name }
let SESSION = null;      // the remembered record for PROJECT, when there is one
/* Issued by the Settings service after a correct password. The page remembers
   this instead of the password, so staying signed in never means keeping what
   someone typed — and changing the password revokes it server-side. */
let SESSION_TOKEN = null;

/* A refusal from the service is a wrong password, not a broken page — the
   login form should say so plainly rather than showing a decryption error. */
class AuthError extends Error {
  constructor(message) { super(message); this.name = 'AuthError'; }
}

/* Sign in.

   The service checks the password and hands back three things: which project
   it belongs to, the key that project's published files were encrypted with,
   and a session token. The password is never the encryption key, which is
   exactly what makes it changeable from this page — changing an encryption key
   would strand every already-published file.

   Without a reachable service there is no sign-in at all: only it knows which
   project a password belongs to, and it is the only thing holding the data
   keys. That is deliberate — see the fallback note below. */
async function unlock(password, remember) {
  let key = null;
  let project = null;
  let token = null;

  if (SETTINGS_URL) {
    let auth = null;
    try {
      auth = await jsonp(
        `${SETTINGS_URL}?action=auth&password=${encodeURIComponent(password)}`);
    } catch (err) {
      auth = { ok: false, error: err.message, unreachable: true };
    }
    if (auth.ok) {
      project = { id: auth.project, name: auth.name || auth.project };
      token = auth.token;
      /* The key exists ONLY to decrypt a published snapshot, and its KDF
         parameters live in that snapshot — so deriving it needs one to exist.
         A project that has never been published has none, and reading the
         sheet live needs no key at all.

         This was a hard requirement, and it took sign-in down: publishing
         stopped writing snapshots in the same change that started reading
         tabs live, so the fetch 404'd and the password could not be used at
         all. The key is optional now, because it always was. */
      try {
        const payload = await fetchPayloadFor(project.id, 'index');
        key = await deriveKey(auth.dataKey, payload.kdf);
      } catch (noSnapshot) {
        key = null;
      }
    } else if (auth.error && /no project matched|no password sent/i.test(auth.error)) {
      // The service is working and says no. Believe it.
      throw new AuthError('No project matched that password.');
    }
    // Anything else — service unreachable, properties not configured yet, a
    // deployment mid-change — must not brick the page. Fall through.
  }

  // The token is what opening a project actually requires — not the key, which
  // only some of the data paths use.
  if (!token) {
    // There is deliberately no offline fallback.
    //
    // It used to try a project id stamped into config.js at publish time. That
    // put a real project id on a world-readable URL, which is the one thing
    // this design promises not to do — and it could only ever have worked for
    // a project whose data key happened to equal its password, which is true
    // of no project created since. A clear failure is worth more than a
    // fallback that leaks and does not work.
    throw new AuthError(
      'The sign-in service is unreachable, so this password cannot be checked. ' +
      'Try again in a moment.');
  }

  await enter(project, key, token, remember);
}

/* Where the tab index comes from, and what to do when it does not come.

   Live first: the snapshot is a published export that nothing writes any more,
   kept only for a project that still has one and cannot reach the service.
   Neither is required — see enter(). */
async function loadManifest(project, key, token) {
  try {
    if (!token) throw new Error('no session token');
    return await liveManifest(token);
  } catch (liveError) {
    if (liveError.signedOut) throw liveError;
    try {
      const manifest = await decryptWith(key, await fetchPayloadFor(project.id, 'index'));
      manifest.live = false;
      return manifest;
    } catch (snapshotError) {
      return { captured_at: '', spreadsheet_id: '', worksheets: [],
               live: false, unavailable: liveError.message };
    }
  }
}

/* Everything that has to be true once a project is open, in one place, so
   signing in and switching cannot drift apart.

   The order matters. The session is remembered before the switcher is drawn,
   or the project just opened would be missing from its own menu. And the app
   is revealed last, so it is never briefly visible showing the previous
   project's name. */
async function enter(project, key, token, remember = false) {
  /* WHAT SIGNING IN WAITS FOR: the password, and nothing else.

     This has been wrong twice, in the same place, for the same reason — the
     data and the door were tangled together. First the manifest was a
     REQUIREMENT: it proved the key, so a project with no published snapshot
     could not be opened at all, and sign-in died on a 404 the day publishing
     stopped writing them. Then it was merely AWAITED, which is barely better:
     reading the tab index of a real spreadsheet takes about nine seconds, so
     the button sat on "Unlocking…" for nine seconds with everything already
     in hand.

     auth has checked the password and handed back the token and key. That is
     the whole of signing in. The tab index is data, it is fetched in the
     background, and the Data panel fills itself in when it arrives. */
  PROJECT = project;
  KEY = key;
  SESSION_TOKEN = token || null;
  // A placeholder, replaced when the index arrives below.
  MANIFEST = { captured_at: '', spreadsheet_id: '', worksheets: [],
               live: false, loading: true };
  data.cache = {};                 // the previous project's tabs are not this one's
  SETTINGS_ROWS = null;
  KEYWORDS = null;
  KEYWORD_DRAFT = null;
  // Unsaved edits belong to the project they were typed in. Carrying them over
  // would write one project's settings into another's spreadsheet on the next
  // save — silently, since the panel would look the same either way.
  Object.keys(pending).forEach((key) => { delete pending[key]; });
  const savebar = $('settings-savebar');
  if (savebar) savebar.hidden = true;

  if (remember) await rememberSession(project.id, project.name, key, token);
  SESSION = (await loadSessions())[project.id] || null;

  await renderSwitcher();
  $('gate').hidden = true;
  $('app').hidden = false;
  await boot();

  /* Deliberately not awaited. A slow tab index must not hold the door shut,
     and every other panel reads the sheet through its own call. */
  const opened = project.id;
  loadManifest(project, key, token).then((manifest) => {
    // The viewer may have switched projects while this was in flight; a
    // manifest belongs to the project it was asked for.
    if (!PROJECT || PROJECT.id !== opened) return;
    MANIFEST = manifest;
    return boot();
  }).catch(async (err) => {
    if (!PROJECT || PROJECT.id !== opened) return;
    if (err && err.signedOut) {
      /* The one refusal that has to end the session. It is found late now
         rather than before the page opened, which is the right trade: a valid
         password must never wait on this, and a dead one is rare and obvious. */
      await forgetSession(opened);
      location.reload();
      return;
    }
    MANIFEST = { captured_at: '', spreadsheet_id: '', worksheets: [],
                 live: false, unavailable: err.message };
    await boot();
  });
}

/* Resume the project last looked at. A key that no longer decrypts means the
   data was republished under a new key, so that session is dropped and the
   viewer is asked again rather than shown a broken page. */
async function resume() {
  const all = await loadSessions();
  const ordered = Object.entries(all)
    .sort((a, b) => (b[1].usedAt || 0) - (a[1].usedAt || 0));
  for (const [id, rec] of ordered) {
    try {
      SESSION = rec;
      await enter({ id, name: rec.name || id }, rec.key, rec.token);
      await touchSession(id);
      return true;
    } catch (err) {
      // Only a key that no longer decrypts means the session is finished. Any
      // other failure is the network, and throwing the session away for that
      // would make one offline reload cost every remembered project its full
      // ten days.
      if (staleKey(err)) await forgetSession(id);
    }
  }
  SESSION = null;
  return false;
}

/* Whether a failure means the remembered key is finished, as opposed to the
   network being down. WebCrypto raises OperationError when AES-GCM cannot
   authenticate the ciphertext, which is exactly the "republished under a new
   key" case; a fetch that failed or 404'd is not that. */
function staleKey(err) {
  return Boolean(err) && err.name === 'OperationError';
}

async function switchTo(projectId) {
  const rec = (await loadSessions())[projectId];
  if (!rec) return openGate({ extra: true });
  closeMenu();
  try {
    SESSION = rec;
    await enter({ id: projectId, name: rec.name || projectId }, rec.key, rec.token);
    await touchSession(projectId);
  } catch (err) {
    if (!staleKey(err)) {
      // Offline, or the file is briefly missing mid-publish. The session is
      // still good, so keep it and say what actually went wrong.
      banner($('data-error'), 'err',
        `Could not open ${rec.name || projectId}: ${err.message}`);
      return;
    }
    // Its data was republished under a different key, or removed.
    await forgetSession(projectId);
    openGate({ extra: true, message:
      `${rec.name || projectId} needs its password again.` });
  }
}

/* ── The project switcher ─────────────────────────────────────────────────
   Only projects already unlocked on this device are listed. There is nothing
   to enumerate: the page has never been told what else exists. */

function closeMenu() {
  $('project-menu').hidden = true;
  $('project-btn').setAttribute('aria-expanded', 'false');
}

async function renderSwitcher() {
  $('project-name').textContent = (PROJECT && PROJECT.name) || 'Project';
  const menu = $('project-menu');
  menu.innerHTML = '';
  const all = await loadSessions();

  Object.entries(all)
    .sort((a, b) => (a[1].name || a[0]).localeCompare(b[1].name || b[0]))
    .forEach(([id, rec]) => {
      const item = el('button', 'menu-item');
      item.type = 'button';
      const current = PROJECT && id === PROJECT.id;
      item.appendChild(el('span', 'tick', current ? '✓' : ''));
      item.appendChild(el('span', '', rec.name || id));
      if (current) item.classList.add('current');
      item.addEventListener('click', () => { if (!current) switchTo(id); else closeMenu(); });
      menu.appendChild(item);
    });

  if (Object.keys(all).length) menu.appendChild(el('div', 'menu-sep'));

  const add = el('button', 'menu-item', '＋  Unlock another project');
  add.type = 'button';
  add.addEventListener('click', () => { closeMenu(); openGate({ extra: true }); });
  menu.appendChild(add);

  if (SETTINGS_URL) {
    const make = el('button', 'menu-item', '✚  New project…');
    make.type = 'button';
    make.addEventListener('click', () => { closeMenu(); openNewProject(); });
    menu.appendChild(make);

    const copy = el('button', 'menu-item', '⧉  Copy this project…');
    copy.type = 'button';
    copy.addEventListener('click', () => { closeMenu(); openNewProject({ copy: true }); });
    menu.appendChild(copy);

    const remove = el('button', 'menu-item danger', '🗑  Delete this project…');
    remove.type = 'button';
    remove.addEventListener('click', () => { closeMenu(); openDeleteProject(); });
    menu.appendChild(remove);
  }

  const out = el('button', 'menu-item danger', 'Sign out of this project');
  out.type = 'button';
  out.addEventListener('click', async () => {
    if (PROJECT) await forgetSession(PROJECT.id);
    location.reload();
  });
  menu.appendChild(out);
}

$('project-btn').addEventListener('click', () => {
  const menu = $('project-menu');
  const showing = menu.hidden;
  menu.hidden = !showing;
  $('project-btn').setAttribute('aria-expanded', String(showing));
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.switcher')) closeMenu();
});

/* ── The gate ─────────────────────────────────────────────────────────────
   Shown to sign in for the first time, and again to add a second project —
   in which case it can be cancelled back to the project already open. */

let GATE_IS_EXTRA = false;

function openGate({ extra = false, message = '' } = {}) {
  GATE_IS_EXTRA = extra;
  $('gate-title').textContent = extra ? 'Unlock another project' : 'Job Scraping Automation';
  $('gate-sub').textContent = extra
    ? 'Enter that project\'s password. The one you are in now stays unlocked.'
    : 'Enter your project password. It decrypts the data in your browser — nothing is sent anywhere.';
  $('gate-err').textContent = message;
  $('gate-cancel').hidden = !extra;
  $('gate-pw').value = '';
  $('app').hidden = extra ? false : true;
  $('gate').hidden = false;
  $('gate-pw').focus();
}

$('gate-cancel').addEventListener('click', () => {
  GATE_IS_EXTRA = false;
  $('gate').hidden = true;
  $('app').hidden = false;
});

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
      ? (ex.name === 'AuthError' ? ex.message : 'No project matched that password.')
      : `Could not unlock: ${ex.message}`;
    $('gate-pw').select();
  } finally {
    btn.disabled = false;
    btn.textContent = 'Unlock';
  }
});

$('btn-lock').addEventListener('click', async () => {
  await forgetAllSessions();
  location.reload();
});

/* ── Creating a project ───────────────────────────────────────────────────
   The service does the whole job: it creates the spreadsheet in the owner's
   Drive, gives it the tabs the automation expects, shares it with the service
   account, and registers it. A service account cannot do this itself — one on
   a consumer account has no Drive storage quota, so it cannot own a file at
   all — which is why this is a call to the Apps Script and not to Python.

   As with every other write here, the POST's own answer is unreadable (see the
   Settings section), so success is proved by signing in to the new project. */

/* Copying reuses this dialog. The two differ only in what the button does and
   which explanation is shown, so a second near-identical form would be a second
   place for them to drift apart. */
let NP_MODE = 'create';

function openNewProject({ copy = false } = {}) {
  NP_MODE = copy ? 'copy' : 'create';
  const dlg = $('new-project');
  ['np-name', 'np-pw', 'np-sheet', 'np-admin', 'np-email']
    .forEach((id) => { $(id).value = ''; });
  $('np-results').checked = false;
  $('np-err').textContent = '';
  $('np-done').hidden = true;
  $('np-go').disabled = false;

  const source = (PROJECT && PROJECT.name) || 'this project';
  dlg.querySelector('h2').textContent = copy ? `Copy “${source}”` : 'New project';
  dlg.querySelector('.muted').textContent = copy
    ? 'A copy of this project\'s spreadsheet becomes a separate project with its '
      + 'own password. Changing one never affects the other.'
    : 'A spreadsheet is created in your Drive, given the tabs the automation '
      + 'expects, and shared with the service account — nothing to set up by hand.';
  $('np-go').textContent = copy ? 'Copy project' : 'Create project';
  $('np-adopt').hidden = copy;          // a copy has its source already
  $('np-results-wrap').hidden = !copy;
  $('np-copy-note').hidden = !copy;
  // Only ask for an admin password when the service says it wants one.
  jsonp(`${SETTINGS_URL}?ping=1`)
    .then((info) => { $('np-admin-wrap').hidden = !info.adminPasswordConfigured; })
    .catch(() => { $('np-admin-wrap').hidden = true; });
  dlg.showModal();
}

async function createProject() {
  const name = $('np-name').value.trim();
  const password = $('np-pw').value;
  const spreadsheetId = $('np-sheet').value.trim();
  const ownerEmail = $('np-email').value.trim();
  const copying = NP_MODE === 'copy';
  const adminPassword = $('np-admin').value;
  const err = $('np-err');
  err.textContent = '';

  if (!name) { err.textContent = 'Give the project a name.'; return; }
  if (password.length < 8) {
    err.textContent = 'The password must be at least 8 characters.'; return;
  }

  const go = $('np-go');
  go.disabled = true;
  go.textContent = 'Creating…';
  try {
    // Ask FIRST whether this password already belongs to a project.
    //
    // Creation is confirmed below by signing in with the password, and the
    // service refuses a duplicate — so without this check a password that was
    // already in use would authenticate against the project that owns it and
    // be reported as "Created", naming someone else's project.
    let clash = null;
    try {
      clash = await jsonp(
        `${SETTINGS_URL}?action=auth&password=${encodeURIComponent(password)}`);
    } catch { /* unreachable now is handled by the confirmation below */ }
    if (clash && clash.ok) {
      err.textContent = 'That password already belongs to a project. ' +
                        'Choose a different one.';
      go.disabled = false;
      go.textContent = copying ? 'Copy project' : 'Create project';
      return;
    }

    await fetch(SETTINGS_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify(copying ? {
        action: 'copyProject', token: SESSION_TOKEN,
        name, password, ownerEmail, includeResults: $('np-results').checked,
      } : {
        action: 'createProject', token: SESSION_TOKEN,
        name, password, spreadsheetId, adminPassword, ownerEmail,
      }),
    });

    // Creating a spreadsheet and sharing it takes a few seconds, so the
    // confirmation is retried rather than asked once and given up on.
    let auth = null;
    for (let attempt = 0; attempt < 10 && !(auth && auth.ok); attempt++) {
      await new Promise((r) => setTimeout(r, attempt ? 2000 : 1500));
      try {
        auth = await jsonp(
          `${SETTINGS_URL}?action=auth&password=${encodeURIComponent(password)}`);
      } catch { /* keep waiting */ }
    }

    if (!auth || !auth.ok) {
      err.textContent = copying
        ? 'The project was not copied. The most likely reason is a password ' +
          'another project already uses.'
        : 'The project was not created. The most likely reasons are a password ' +
          'another project already uses, or an admin password being required.';
      go.disabled = false;
      go.textContent = 'Create project';
      return;
    }

    // Deliberately not remembered as a session yet: there is nothing published
    // to derive a key from until the first run, so a stored session would be
    // an entry that cannot open anything.
    // The sheet id comes back through the sign-in, not the create: a no-cors
    // POST's own answer cannot be read (see the Settings section).
    let sheetUrl = '';
    try {
      const info = await jsonp(
        `${SETTINGS_URL}?action=project&token=${encodeURIComponent(auth.token)}`);
      if (info.ok && info.spreadsheetId) {
        sheetUrl = `https://docs.google.com/spreadsheets/d/${info.spreadsheetId}`;
      }
    } catch { /* the link is a convenience, not the result */ }

    const done = $('np-done');
    done.hidden = false;
    done.textContent = `Created "${auth.name || name}". `;
    if (sheetUrl) {
      const link = el('a', '', 'Open its Google Sheet');
      link.href = sheetUrl;
      link.target = '_blank';
      link.rel = 'noopener';
      done.append(link);
      done.append(document.createTextNode(
        ownerEmail ? ` — shared with ${ownerEmail}.` : '.'));
    }
    done.append(el('div', 'hint',
      'Run the pipeline for this project, then unlock it here with its password.'));
    go.textContent = copying ? 'Copied' : 'Created';
  } catch (ex) {
    err.textContent = `Could not ${copying ? 'copy' : 'create'} the project: ${ex.message}`;
    go.disabled = false;
    go.textContent = copying ? 'Copy project' : 'Create project';
  }
}

/* ── Deleting a project ───────────────────────────────────────────────────
   Three separate confirmations, because a session alone proves only that the
   browser was left open: the name typed out proves the person knows WHICH
   project this is, and the password proves it is theirs to delete. The
   spreadsheet is left alone unless they ask, since it is often the only copy
   of work that took a long time to gather. */

function openDeleteProject() {
  if (!PROJECT) return;
  $('dp-expected').textContent = PROJECT.name;
  $('dp-name').value = '';
  $('dp-pw').value = '';
  $('dp-trash').checked = false;
  $('dp-err').textContent = '';
  $('dp-go').disabled = true;
  $('dp-go').textContent = 'Delete project';
  $('dp-sheet-note').textContent = 'The Google Sheet is left untouched in Drive.';
  $('delete-project').showModal();
  $('dp-name').focus();
}

/* The button stays dead until the name matches — the confirmation should be
   visible in the UI, not only enforced by the server. */
function refreshDeleteButton() {
  const typed = $('dp-name').value.trim().toLowerCase();
  const expected = ((PROJECT && PROJECT.name) || '').trim().toLowerCase();
  $('dp-go').disabled = !(typed && typed === expected);
}
$('dp-name').addEventListener('input', refreshDeleteButton);
$('dp-trash').addEventListener('change', () => {
  $('dp-sheet-note').textContent = $('dp-trash').checked
    ? "The Google Sheet goes to Drive's bin, recoverable for 30 days."
    : 'The Google Sheet is left untouched in Drive.';
});

async function deleteProject() {
  const err = $('dp-err');
  const go = $('dp-go');
  const password = $('dp-pw').value;
  err.textContent = '';
  if (!password) { err.textContent = 'Enter this project\'s password.'; return; }

  const doomed = PROJECT.id;
  go.disabled = true;
  go.textContent = 'Deleting…';
  try {
    await fetch(SETTINGS_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({
        action: 'deleteProject', token: SESSION_TOKEN, password,
        confirm: true, confirmName: $('dp-name').value.trim(),
        trashSheet: $('dp-trash').checked,
      }),
    });

    // The POST's answer is unreadable, so it is proved by the password no
    // longer opening anything.
    let gone = false;
    for (let attempt = 0; attempt < 6 && !gone; attempt++) {
      await new Promise((r) => setTimeout(r, attempt ? 1500 : 1200));
      try {
        const check = await jsonp(
          `${SETTINGS_URL}?action=auth&password=${encodeURIComponent(password)}`);
        gone = !check.ok;
      } catch { /* keep waiting */ }
    }

    if (!gone) {
      err.textContent = 'The project was not deleted — the password still opens it. ' +
        'Check the name is typed exactly.';
      go.disabled = false;
      go.textContent = 'Delete project';
      return;
    }

    await forgetSession(doomed);
    $('delete-project').close();
    location.reload();
  } catch (ex) {
    err.textContent = `Could not delete the project: ${ex.message}`;
    go.disabled = false;
    go.textContent = 'Delete project';
  }
}

$('dp-go').addEventListener('click', deleteProject);
$('dp-cancel').addEventListener('click', () => $('delete-project').close());

$('np-go').addEventListener('click', createProject);
$('np-cancel').addEventListener('click', () => $('new-project').close());

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
      // Only ever http(s). The target comes from a HYPERLINK formula in the
      // sheet, which is data — a javascript: URL there would otherwise run in
      // the dashboard the moment someone clicked the cell.
      const candidate = row[`${col}__url`] || value;
      const url = /^https?:\/\//i.test(String(candidate || '').trim())
        ? String(candidate).trim() : null;
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
  $('data-count').textContent = MANIFEST && MANIFEST.live ? 'reading…' : 'decrypting…';
  try {
    if (!data.cache[worksheet]) {
      data.cache[worksheet] = MANIFEST.live
        ? await liveWorksheet(worksheet)
        : await fetchEncrypted(worksheet, KEY);
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

/* ── Runs ─────────────────────────────────────────────────────────────────
   Pressing Run here starts the work on the operator's OWN machine, not on
   anyone's servers.

   This page cannot reach that machine — it is behind a router, asleep half the
   time, and on a different address every week — and the machine cannot be
   reached from the internet either. So neither calls the other. Both talk to
   the project's sheet: this appends a row saying what is wanted, and the agent
   running there polls for one and claims it. Both ends only make outbound
   requests, which is why it works from any network.

   That also means Run is a REQUEST, never a guarantee. If no machine is
   listening the row simply waits, so the status line above the cards says
   whether one is there — silence that looks like success is the failure mode
   worth designing against. */
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
  /* No "Refresh this dashboard" any more. It existed to re-export and
     republish the encrypted snapshot, and the panel now reads the sheet
     directly — so there is nothing to refresh, and offering it would suggest
     the data here can be out of date. publish-only still exists as a command
     for anyone who wants an offline copy. */
];

/* What the last poll said: which modes are busy, and whether a machine is on. */
let RUN_STATE = { runs: [], agent: { online: false, everSeen: false } };
let RUNS_TIMER = null;

function busyMode(mode) {
  return RUN_STATE.runs.find(
    (r) => r.mode === mode && (r.status === 'queued' || r.status === 'running'
                               || r.status === 'cancelling'));
}

function renderRunActions() {
  const grid = $('run-actions');
  grid.innerHTML = '';
  AUTOMATIONS.forEach(([label, blurb, choice, primary]) => {
    const card = el('div', `task${primary ? ' primary' : ''}`);
    const h = el('h3', '', label);
    if (primary) h.append(el('span', 'pill info', 'main'));
    card.append(h, el('p', '', blurb));
    const row = el('div', 'run-row');
    const busy = busyMode(choice);
    const btn = el('button', 'btn primary', busy ? busy.status : 'Run here');
    btn.disabled = Boolean(busy);
    /* Deliberately still enabled with no agent online. The run waits in the
       queue and starts the moment the machine comes back, which is more useful
       than refusing — but the status line says so, so it is never a surprise. */
    btn.title = RUN_STATE.agent.online
      ? `Runs on ${RUN_STATE.agent.agent || 'your machine'}`
      : 'No machine is listening — this will wait in the queue';
    btn.addEventListener('click', () => requestRun(choice, btn));
    row.append(btn);
    card.append(row);
    grid.append(card);
  });
}

function renderAgentStatus() {
  const box = $('agent-status');
  const a = RUN_STATE.agent || {};
  box.innerHTML = '';
  const dot = el('span', `dot ${a.online ? 'ok' : (a.everSeen ? 'warn' : 'off')}`);
  let text;
  if (a.online) {
    text = `${a.agent || 'A machine'} is listening — last seen ${a.secondsAgo}s ago.`;
  } else if (a.everSeen) {
    text = `${a.agent || 'The machine'} is not answering (last seen ` +
           `${fmtAgo(a.secondsAgo)}). Anything you start will wait until it is back.`;
  } else {
    text = 'No machine has connected to this project yet. Start the agent on ' +
           'the computer that should do the work — see “Run locally”.';
  }
  box.append(dot, el('span', '', text));
}

function fmtAgo(seconds) {
  if (!Number.isFinite(seconds)) return 'a while ago';
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

/* A write's reply cannot be read (no CORS header on /exec), so this posts
   blind and then re-reads the queue to prove it landed. */
async function requestRun(mode, btn) {
  const previous = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Queueing…';
  banner($('runs-error'), '', '');
  try {
    await fetch(SETTINGS_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ action: 'requestRun', token: SESSION_TOKEN, mode,
                             requestedBy: 'dashboard' }),
    });
    let landed = false;
    for (let attempt = 0; attempt < 6 && !landed; attempt++) {
      await new Promise((r) => setTimeout(r, attempt ? 1200 : 900));
      await loadRuns({ quiet: true });
      landed = Boolean(busyMode(mode));
    }
    if (!landed) {
      banner($('runs-error'), 'err',
        'The run was not queued. Your session may have expired — sign out and back in.');
      btn.disabled = false;
      btn.textContent = previous;
    }
  } catch (err) {
    banner($('runs-error'), 'err', `Could not queue that run: ${err.message}`);
    btn.disabled = false;
    btn.textContent = previous;
  }
}

async function cancelRun(id) {
  try {
    await fetch(SETTINGS_URL, {
      method: 'POST',
      mode: 'no-cors',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ action: 'cancelRun', token: SESSION_TOKEN, id }),
    });
    await new Promise((r) => setTimeout(r, 1200));
    await loadRuns({ quiet: true });
  } catch (err) {
    banner($('runs-error'), 'err', `Could not cancel: ${err.message}`);
  }
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

/* Apps Script can take a long time to answer — it opens a spreadsheet to do it
   — so this waits a while before giving up. */
function jsonp(url, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    const name = `__jsonp_${Math.random().toString(36).slice(2)}`;
    const script = document.createElement('script');
    let settled = false;

    /* Removing the script tag does NOT cancel the request. A slow reply still
       arrives and still runs `__jsonp_xxx({…})`, so deleting the name on
       timeout turned every slow response into an uncaught ReferenceError with
       no obvious cause. A no-op is left in its place instead, and cleaned up
       later once nothing can still be in flight. */
    const retire = () => {
      window[name] = () => {};
      setTimeout(() => { delete window[name]; }, 120000);
    };

    const done = (fn) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      script.remove();
      retire();
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
    // It must be THIS project that answers. A password the service refused —
    // because another project already uses it — would still authenticate
    // successfully against that other project, and taking that as proof would
    // report a change that never happened and store someone else's session
    // token under this project.
    if (check.ok && PROJECT && check.project === PROJECT.id) {
      SESSION_TOKEN = check.token;
      if (remainingDays() && PROJECT) {
        await rememberSession(PROJECT.id, PROJECT.name, KEY, SESSION_TOKEN);
      }
      ['pw-current', 'pw-new', 'pw-repeat'].forEach((id) => { $(id).value = ''; });
      status.textContent = '';
      banner($('settings-error'), 'ok',
        'Password changed. Everyone signed in elsewhere will have to sign in again ' +
        'with the new one.');
    } else if (check.ok) {
      status.textContent = '';
      banner($('settings-error'), 'err',
        'The password was not changed — it already belongs to a different ' +
        'project. Choose another one.');
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

const RUN_PILL = {
  queued: 'neutral', running: 'warn', cancelling: 'warn',
  done: 'ok', failed: 'err', cancelled: 'neutral', lost: 'err',
};

async function loadRuns(opts = {}) {
  if (!opts.quiet) banner($('runs-error'), '', '');
  try {
    const view = await jsonp(
      `${SETTINGS_URL}?action=runs&token=${encodeURIComponent(SESSION_TOKEN)}&limit=25`);
    if (!view.ok) throw new Error(view.error || 'the service refused the request');
    RUN_STATE = { runs: view.runs || [], agent: view.agent || {} };
    renderAgentStatus();
    renderRunActions();

    const body = $('runs-body');
    body.innerHTML = '';
    if (!RUN_STATE.runs.length) {
      const tr = el('tr'), td = el('td', 'muted', 'Nothing has been run yet.');
      td.colSpan = 6; tr.append(td); body.append(tr);
    }
    RUN_STATE.runs.forEach((r) => {
      const tr = el('tr');
      tr.append(el('td', 'faint', r.requested_at
        ? new Date(r.requested_at).toLocaleString() : '—'));
      tr.append(el('td', '', labelFor(r.mode)));
      const st = el('td');
      st.append(el('span', `pill ${RUN_PILL[r.status] || 'neutral'}`, r.status));
      tr.append(st);
      tr.append(el('td', 'faint', fmtDuration(r.started_at,
        r.finished_at || (r.status === 'running' ? new Date().toISOString() : ''))));
      tr.append(el('td', 'faint', r.claimed_by || '—'));

      const actions = el('td');
      if (r.status === 'queued' || r.status === 'running') {
        const stop = el('button', 'btn sm', 'Cancel');
        stop.addEventListener('click', () => cancelRun(r.id));
        actions.append(stop);
      } else if (r.summary) {
        const show = el('button', 'btn sm ghost', 'Detail');
        show.addEventListener('click', () => showRunDetail(r));
        actions.append(show);
      }
      tr.append(actions);
      body.append(tr);
    });
    $('runs-updated').textContent = `updated ${new Date().toLocaleTimeString()}`;
    scheduleRunsRefresh();
  } catch (err) {
    if (!opts.quiet) {
      banner($('runs-error'), 'err', `Could not read the queue: ${err.message}`);
    }
  }
}

function labelFor(mode) {
  const found = AUTOMATIONS.find(([, , choice]) => choice === mode);
  return found ? found[0] : mode;
}

function showRunDetail(run) {
  const box = $('run-detail');
  box.hidden = false;
  box.innerHTML = '';
  box.append(el('h4', '', `${labelFor(run.mode)} — ${run.status}`));
  box.append(el('pre', '', run.summary || 'No output was reported.'));
  box.append(el('p', 'faint', run.claimed_by
    ? `The full log is on ${run.claimed_by}, in logs/agent/${run.id}.log`
    : 'Never started, so there is no log.'));
}

/* Poll only while something is actually happening, and only while this panel
   is on screen. An idle dashboard left open all day should cost nothing. */
function scheduleRunsRefresh() {
  clearTimeout(RUNS_TIMER);
  const active = RUN_STATE.runs.some(
    (r) => r.status === 'queued' || r.status === 'running' || r.status === 'cancelling');
  const visible = !$('panel-runs').hidden;
  if (!active || !visible) return;
  RUNS_TIMER = setTimeout(() => loadRuns({ quiet: true }), 5000);
}

$('btn-refresh-runs').addEventListener('click', loadRuns);

/* ── Running it locally ───────────────────────────────────────────────────
   The install command is built from the repository this page was published
   from, so a fork or a renamed branch cannot leave a stale command behind for
   someone to paste. */

function setupCommand() {
  const repo = CFG.repo || 'aakashchamola/automated-scraping';
  const branch = CFG.branch || 'main';
  return `curl -fsSL https://raw.githubusercontent.com/${repo}/${branch}/install.sh | bash`;
}

function renderSetup() {
  const command = setupCommand();
  $('setup-cmd').querySelector('code').textContent = command;
  const repo = CFG.repo || 'aakashchamola/automated-scraping';
  const branch = CFG.branch || 'main';
  $('setup-read').href =
    `https://github.com/${repo}/blob/${branch}/install.sh`;
  // The real URL, so nobody has to be told it separately.
  const exec = $('setup-exec');
  if (exec) {
    exec.textContent = SETTINGS_URL ||
      'ask whoever runs the project for the /exec URL';
  }
}

$('setup-copy').addEventListener('click', async () => {
  const button = $('setup-copy');
  try {
    await navigator.clipboard.writeText(setupCommand());
    button.textContent = 'Copied';
  } catch {
    // Clipboard access is refused in some contexts; selecting it is still useful.
    const range = document.createRange();
    range.selectNodeContents($('setup-cmd'));
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    button.textContent = 'Selected — press ⌘/Ctrl+C';
  }
  setTimeout(() => { button.textContent = 'Copy'; }, 2500);
});

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
    if (btn.dataset.panel === 'panel-setup') renderSetup();
  });
});

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
        'is deployed with CONTROL_SHEET_ID set in its Script Properties.');
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
  if (MANIFEST.loading) {
    chip.textContent = 'reading the sheet…';
    chip.className = 'pill neutral';
    chip.title = 'Fetching the list of tabs. Everything else already works.';
  } else if (MANIFEST.unavailable) {
    chip.textContent = 'no data yet';
    chip.className = 'pill warn';
    chip.title = MANIFEST.unavailable;
  } else if (MANIFEST.live) {
    // Read from the spreadsheet just now, so age is not a thing that can go
    // wrong — saying "data from <a time>" would imply it might be old.
    chip.textContent = 'live from the sheet';
    chip.className = 'pill ok';
    chip.title = 'Read from the spreadsheet when this page loaded. ' +
                 'Reload, or reopen a tab, to see newer rows.';
  } else {
    chip.textContent = `data from ${captured.toLocaleString()}`;
    chip.className = `pill ${ageHours > 24 * 8 ? 'stale' : 'neutral'}`;
    chip.title = 'A published snapshot — the service was unreachable, so this ' +
                 'is the last export rather than what the sheet says now.';
  }

  const days = remainingDays();
  $('btn-lock').title = days
    ? `Signed in for ${days} more day${days === 1 ? '' : 's'}. Lock to sign out now.`
    : 'Sign out';

  renderRunActions();
  if (usable.length) await loadSheet(usable[0].name);
  else if (MANIFEST.loading) {
    // Not an error, and not empty — just not here yet. Saying "no worksheets"
    // now would be wrong in a second's time.
    $('data-count').textContent = 'reading the sheet…';
  } else if (MANIFEST.unavailable) {
    // Everything else on the page works; say what is missing and how to fix it
    // rather than leaving an empty table with no explanation.
    banner($('data-error'), 'warn',
      'Signed in, but this page could not read the sheet: ' + MANIFEST.unavailable +
      ' Settings, Keywords and Runs all still work.');
  } else banner($('data-error'), 'warn', MANIFEST.live
    ? 'This project\'s spreadsheet has no tabs with any rows in them yet.'
    : 'The last run published no readable worksheets.');
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
