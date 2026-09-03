/**
 * test_settings.js — exercises Settings.gs outside Apps Script.
 *
 *     node apps-script/test_settings.js
 *
 * Settings.gs can only be tried for real by pasting it into the Apps Script
 * editor and deploying, which is a slow way to find a typo. This runs the
 * actual file against a shim of the Google services, so routing, auth, project
 * isolation and provisioning are all checked in a second.
 *
 * The shim reproduces the two behaviours that have actually caused bugs:
 * computeDigest returning SIGNED bytes, and getRange() being 1-indexed.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const vm = require('vm');

/* ── Fake Google services ──────────────────────────────────────────────── */

class FakeSheet {
  constructor(name, rows) { this.name = name; this.rows = rows || []; this.frozen = 0; }
  _pad(r, c) {
    while (this.rows.length < r) this.rows.push([]);
    for (const row of this.rows) while (row.length < c) row.push('');
  }
  getName() { return this.name; }
  getLastRow() { return this.rows.filter(r => r.some(c => String(c).trim())).length; }
  getLastColumn() { return Math.max(0, ...this.rows.map(r => r.length)); }
  getMaxRows() { return Math.max(this.rows.length, 1); }
  getMaxColumns() { return Math.max(this.getLastColumn(), 1); }
  setFrozenRows(n) { this.frozen = n; }
  insertRowsAfter(_after, n) { for (let i = 0; i < n; i++) this.rows.push([]); }
  insertColumnsAfter(_after, n) { for (const row of this.rows) for (let i = 0; i < n; i++) row.push(''); }
  clear() { this.rows = []; }
  appendRow(values) { this.rows.push(values.slice()); }
  deleteRows(start, howMany) { this.rows.splice(start - 1, howMany); }
  deleteRow(n) { this.rows.splice(n - 1, 1); }
  getDataRange() {
    const width = this.getLastColumn();
    return { getValues: () => this.rows.map(r => { const c = r.slice(); while (c.length < width) c.push(''); return c; }) };
  }
  getRange(row, col, numRows, numCols) {
    const sheet = this;
    numRows = numRows || 1; numCols = numCols || 1;
    return {
      getValues() {
        sheet._pad(row + numRows - 1, col + numCols - 1);
        const out = [];
        for (let r = 0; r < numRows; r++) out.push(sheet.rows[row - 1 + r].slice(col - 1, col - 1 + numCols));
        return out;
      },
      setValues(values) {
        sheet._pad(row + numRows - 1, col + numCols - 1);
        values.forEach((line, r) => line.forEach((v, c) => { sheet.rows[row - 1 + r][col - 1 + c] = v; }));
      },
      setValue(v) { sheet._pad(row, col); sheet.rows[row - 1][col - 1] = v; },
    };
  }
}

class FakeSpreadsheet {
  constructor(id, sheets) { this.id = id; this.sheets = sheets || []; }
  getId() { return this.id; }
  getUrl() { return 'https://docs.google.com/spreadsheets/d/' + this.id; }
  getSheets() { return this.sheets; }
  getSheetByName(n) { return this.sheets.find(s => s.name === n) || null; }
  insertSheet(n) { const s = new FakeSheet(n, []); this.sheets.push(s); return s; }
  deleteSheet(s) { this.sheets = this.sheets.filter(x => x !== s); }
}

function buildSandbox(world) {
  // Folders record what is added and removed, so a move can be asserted rather
  // than assumed. A folder listed in world.readonlyParents refuses removeFile,
  // which is what Drive does when the file lives in someone else's Drive.
  const makeFolder = (id) => ({
    getId: () => id,
    getName: () => (id === 'root' ? 'My Drive' : 'Projects (' + id + ')'),
    getEditors: () => (world.folderEditors[id] || []).map(e => ({ getEmail: () => e })),
    getViewers: () => (world.folderViewers[id] || []).map(e => ({ getEmail: () => e })),
    addFile(f) {
      const fid = f.getId();
      world.filed.push([id, fid]);
      world.parents[fid] = [...new Set([...(world.parents[fid] || []), id])];
    },
    removeFile(f) {
      if (world.readonlyParents.includes(id)) {
        throw new Error("cannot remove from another owner's Drive");
      }
      const fid = f.getId();
      world.parents[fid] = (world.parents[fid] || []).filter(p => p !== id);
    },
  });

  const Utilities = {
    DigestAlgorithm: { SHA_256: 'sha256' },
    Charset: { UTF_8: 'utf8' },
    computeDigest(alg, text) {
      const buf = crypto.createHash(alg).update(text, 'utf8').digest();
      return Array.from(buf).map(b => (b > 127 ? b - 256 : b));   // signed, like Java
    },
    getUuid: () => crypto.randomUUID(),
    base64EncodeWebSafe: s => Buffer.from(s).toString('base64url'),
  };

  const sandbox = {
    Utilities,
    console,
    Logger: { log() {} },
    Session: {
      getEffectiveUser: () => ({
        getEmail() {
          // Apps Script throws here unless the userinfo.email scope was
          // granted, which it is not by default. A live deployment failed on
          // exactly this, so the shim reproduces it.
          if (world.denyUserinfo) {
            throw new Error('You do not have permission to call ' +
              'Session.getEffectiveUser. Required permissions: ' +
              'https://www.googleapis.com/auth/userinfo.email');
          }
          return 'owner@example.com';
        },
      }),
    },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: k => (k in world.props ? world.props[k] : null),
        setProperty: (k, v) => { world.props[k] = String(v); },
      }),
    },
    LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
    SpreadsheetApp: {
      openById(id) {
        if (!world.sheets[id]) throw new Error('no such spreadsheet: ' + id);
        return world.sheets[id];
      },
      create(name) {
        const id = 'new-' + (++world.created);
        world.sheets[id] = new FakeSpreadsheet(id, [new FakeSheet('Sheet1', [])]);
        world.names[id] = name;
        return world.sheets[id];
      },
    },
    DriveApp: {
      getFileById: id => ({
        addEditor(email) { (world.editors[id] = world.editors[id] || []).push(email); },
        removeEditor(email) {
          world.editors[id] = (world.editors[id] || []).filter(e => e !== email);
        },
        getEditors() {
          return (world.editors[id] || []).map(e => ({ getEmail: () => e }));
        },
        getViewers() {
          return (world.viewers[id] || []).map(e => ({ getEmail: () => e }));
        },
        removeViewer(email) {
          world.viewers[id] = (world.viewers[id] || []).filter(e => e !== email);
        },
        setTrashed(flag) {
          if (world.untrashable.includes(id)) throw new Error('cannot trash it');
          world.trashed[id] = flag;
        },
        getUrl() { return 'https://docs.google.com/spreadsheets/d/' + id; },
        makeCopy(name, folder) {
          const copyId = 'copy-' + (++world.created);
          const source = world.sheets[id];
          world.sheets[copyId] = new FakeSpreadsheet(copyId,
            source.getSheets().map(s => new FakeSheet(s.name, s.rows.map(r => r.slice()))));
          world.names[copyId] = name;
          // A Drive copy inherits the source's sharing.
          world.editors[copyId] = (world.editors[id] || []).slice();
          world.viewers[copyId] = (world.viewers[id] || []).slice();
          if (folder) folder.addFile({ getId: () => copyId });
          return sandbox.DriveApp.getFileById(copyId);
        },
        getId() { return id; },
        getParents() {
          const list = (world.parents[id] || []).map(pid => makeFolder(pid));
          let i = 0;
          return { hasNext: () => i < list.length, next: () => list[i++] };
        },
      }),
      getFolderById: id => {
        if (!world.folders.includes(id)) throw new Error('no such folder: ' + id);
        return makeFolder(id);
      },
      getRootFolder: () => makeFolder('root'),
    },
    // A time-driven trigger has nobody to answer, so what happened goes to the
    // execution log.
    Logger: { log(message) { world.logged = (world.logged || []).concat(message); } },
    ContentService: {
      MimeType: { JAVASCRIPT: 'JAVASCRIPT', JSON: 'JSON' },
      // The mime type is kept, not discarded: whether a reply is javascript or
      // JSON is part of the contract with the two very different callers.
      createTextOutput: text => ({
        text, mime: '',
        setMimeType(type) { this.mime = type; return this; },
        getContent() { return this.text; },
      }),
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'Settings.gs'), 'utf8'), sandbox);
  return sandbox;
}

/* ── A world with two projects ─────────────────────────────────────────── */

const CONTROL_HEADER = ['id', 'name', 'spreadsheet_id', 'status', 'data_key',
                        'pw_salt', 'pw_hash', 'created_at', 'notes'];

function makeWorld() {
  const world = { props: {}, sheets: {}, editors: {}, folders: ['folder-1'],
                  filed: [], names: {}, created: 0,
                  parents: {}, readonlyParents: [],
                  viewers: {}, trashed: {}, untrashable: [],
                  folderEditors: {}, folderViewers: {} };

  const project = (id, jobs) => new FakeSpreadsheet(id, [
    new FakeSheet('Settings', [
      ['Group', 'Setting', 'Value', 'Type', 'Options', 'Description'],
      ['Scraping', 'scraping.max_pages', '3', 'int', '1 - 10', 'pages per search'],
      ['Scraping', 'scraping.platforms', 'linkedin', 'multiselect', '', 'where to look'],
      ['Sheets', 'google_sheets.jobs_worksheet', jobs, 'text', '', 'target tab'],
    ]),
    new FakeSheet('Keywords', [['Search Term', 'LinkedIn'], ['data analyst', 'y'], ['ml engineer', 'n']]),
    new FakeSheet('Jobs', [['Company', 'Role']]),
  ]);

  world.sheets['ctrl'] = new FakeSpreadsheet('ctrl', [new FakeSheet('Projects', [CONTROL_HEADER.slice()])]);
  world.sheets['sheet-a'] = project('sheet-a', 'Jobs_Test');
  world.sheets['sheet-b'] = project('sheet-b', 'Jobs');

  world.props.CONTROL_SHEET_ID = 'ctrl';
  world.props.SERVICE_ACCOUNT = 'bot@example.iam.gserviceaccount.com';
  return world;
}

function _projectCount(world) {
  return world.sheets['ctrl'].getSheetByName('Projects').rows.length - 1;
}

function seedProjects(sandbox, world) {
  const control = world.sheets['ctrl'].getSheetByName('Projects');
  [['alpha', 'Alpha Ltd', 'sheet-a', 'pw-alpha-secret'],
   ['beta', 'Beta Corp', 'sheet-b', 'pw-beta-secret'],
   ['gone', 'Archived',  'sheet-b', 'pw-gone-secret']].forEach(([id, name, sid, pw], i) => {
    const salt = 'salt-' + id;
    control.appendRow([id, name, sid, i === 2 ? 'archived' : 'active',
                       'key-' + id, salt, sandbox._hashPassword(pw, salt),
                       '2026-01-01T00:00:00Z', '']);
  });
}

/* ── Assertions ────────────────────────────────────────────────────────── */

let passed = 0, failed = 0;
function check(label, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + label); }
  else { failed++; console.log('  FAIL ' + label + (detail ? '\n       ' + detail : '')); }
}
function get(sandbox, params) {
  const out = sandbox.doGet({ parameter: params }).getContent();
  return JSON.parse(out.replace(/^[A-Za-z_$][\w$]*\(/, '').replace(/\);$/, ''));
}
function post(sandbox, body) {
  return JSON.parse(sandbox.doPost({ postData: { contents: JSON.stringify(body) } }).getContent());
}

/* ── Tests ─────────────────────────────────────────────────────────────── */

console.log('\nSettings.gs\n');

{
  const world = makeWorld(); const s = buildSandbox(world);
  console.log('ping');
  const before = get(s, { ping: '1' });
  check('reports itself standalone, version 4', before.standalone === true && before.version === 4);
  check('sees no projects in an empty registry', before.activeProjects === 0);
  seedProjects(s, world);
  const after = get(s, { ping: '1' });
  check('counts only active projects', after.activeProjects === 2, 'got ' + after.activeProjects);
  check('never names them', !JSON.stringify(after).includes('Alpha'));
  check('confirms the control sheet is readable', after.controlSheetReadable === true);
  check('confirms the service account is set', after.serviceAccountConfigured === true);

  // Diagnostics must never be able to fail the whole ping.
  world.denyUserinfo = true;
  const denied = get(s, { ping: '1' });
  check('a refused userinfo scope does not break the ping',
        denied.ok === true && denied.activeProjects === 2, JSON.stringify(denied));
  check('and the field is simply blank', denied.runsAs === '');
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nsign in');
  const bad = get(s, { action: 'auth', password: 'nope' });
  check('a wrong password matches nothing', bad.ok === false);
  check('and does not disclose any project', !JSON.stringify(bad).includes('alpha'));
  const none = get(s, { action: 'auth' });
  check('no password at all is refused', none.ok === false && /no password/.test(none.error));
  const ok = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  check('the right password selects its project', ok.ok === true && ok.project === 'alpha');
  check('and returns that project\'s data key', ok.dataKey === 'key-alpha');
  check('and a token', typeof ok.token === 'string' && ok.token.length > 30);
  const archived = get(s, { action: 'auth', password: 'pw-gone-secret' });
  check('an archived project cannot be signed into', archived.ok === false);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nproject isolation');
  const alpha = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const beta = get(s, { action: 'auth', password: 'pw-beta-secret' });
  const aSettings = get(s, { token: alpha.token });
  const bSettings = get(s, { token: beta.token });
  const valueOf = (r, key) => r.settings.rows.find(x => x.Setting === key).Value;
  check('each token reads its own sheet',
        valueOf(aSettings, 'google_sheets.jobs_worksheet') === 'Jobs_Test' &&
        valueOf(bSettings, 'google_sheets.jobs_worksheet') === 'Jobs');
  const crossed = get(s, { token: alpha.token, project: 'beta' });
  check('a token cannot be pointed at another project', crossed.ok === false && crossed.signedOut === true);
  const forged = get(s, { token: 'x'.repeat(64) });
  check('a forged token is refused', forged.ok === false);

  post(s, { token: alpha.token, updates: { 'scraping.max_pages': '9' } });
  check('a write lands in the right project',
        world.sheets['sheet-a'].getSheetByName('Settings').rows[1][2] === '9');
  check('and leaves the other project alone',
        world.sheets['sheet-b'].getSheetByName('Settings').rows[1][2] === '3');
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nsettings and keywords');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const unknown = post(s, { token: auth.token, updates: { 'not.a.setting': '1' } });
  check('an unknown setting is reported, not invented',
        unknown.unknown.length === 1 && unknown.applied.length === 0);
  const same = post(s, { token: auth.token, updates: { 'scraping.max_pages': '3' } });
  check('an unchanged value is not rewritten', same.unchanged.length === 1);

  const kw = get(s, { action: 'keywords', token: auth.token });
  check('keywords are read from the Search Term column',
        kw.keywords.keywords.join() === 'data analyst,ml engineer');
  post(s, { action: 'saveKeywords', token: auth.token,
            keywords: ['python dev', 'python dev', ' data analyst '] });
  const sheet = world.sheets['sheet-a'].getSheetByName('Keywords');
  check('saving de-duplicates and trims', sheet.rows[1][0] === 'python dev' && sheet.rows[2][0] === 'data analyst');
  check('and never disturbs the neighbouring column', sheet.rows[1][1] === 'y');
  const emptied = post(s, { action: 'saveKeywords', token: auth.token, keywords: [] });
  check('an empty list is refused', emptied.ok === false && /empty/.test(emptied.error));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\ncreating a project');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const made = post(s, { action: 'createProject', token: auth.token,
                         name: 'Gamma Industries', password: 'gamma-secret-1' });
  check('a sheet is created', made.ok === true && made.createdSheet === true, JSON.stringify(made));
  check('the id is slugified', made.project === 'gamma-industries');
  check('the service account is given editor rights',
        (world.editors[made.spreadsheetId] || []).includes('bot@example.iam.gserviceaccount.com'));
  const tabs = world.sheets[made.spreadsheetId].getSheets().map(x => x.name);
  check('every template tab exists', ['Jobs', 'Company', 'Companies', 'Keywords', 'Settings']
        .every(t => tabs.includes(t)), tabs.join());
  check('the stray Sheet1 is removed', !tabs.includes('Sheet1'));
  const signedIn = get(s, { action: 'auth', password: 'gamma-secret-1' });
  check('the new project can be signed into immediately',
        signedIn.ok === true && signedIn.project === 'gamma-industries');

  const dup = post(s, { action: 'createProject', token: auth.token,
                        name: 'Gamma Industries', password: 'another-secret-9' });
  check('a duplicate name gets a distinct id', dup.project === 'gamma-industries-2');
  const clash = post(s, { action: 'createProject', token: auth.token,
                          name: 'Delta', password: 'gamma-secret-1' });
  check('a reused password is refused', clash.ok === false && /already uses/.test(clash.error));
  const short = post(s, { action: 'createProject', token: auth.token, name: 'Eps', password: 'short' });
  check('a short password is refused', short.ok === false);
  const anon = post(s, { action: 'createProject', name: 'Nope', password: 'whatever-1' });
  check('an unauthenticated caller cannot create', anon.ok === false);

  world.props.ADMIN_PASSWORD = 'admin-only-pw';
  const blocked = post(s, { action: 'createProject', token: auth.token,
                            name: 'Zeta', password: 'zeta-secret-1' });
  check('with ADMIN_PASSWORD set a project password is not enough', blocked.ok === false);
  const allowed = post(s, { action: 'createProject', adminPassword: 'admin-only-pw',
                            name: 'Zeta', password: 'zeta-secret-1' });
  check('and the admin password is', allowed.ok === true);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nfiling new sheets into a projects folder');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });

  const loose = post(s, { action: 'createProject', token: auth.token,
                          name: 'Unfiled', password: 'unfiled-secret-1' });
  check('with no folder configured a sheet is still created',
        loose.ok === true && loose.filedIn === '', JSON.stringify(loose.filedIn));

  world.props.PROJECTS_FOLDER_ID = 'folder-1';
  const filed = post(s, { action: 'createProject', token: auth.token,
                          name: 'Filed', password: 'filed-secret-1' });
  check('with a folder configured the sheet is moved into it',
        filed.ok === true && /^Projects \(folder-1\)$/.test(filed.filedIn), filed.filedIn);
  check('and it is the new sheet that was filed',
        world.filed.some(([f, id]) => f === 'folder-1' && id === filed.spreadsheetId),
        JSON.stringify(world.filed));

  world.props.PROJECTS_FOLDER_ID = 'no-such-folder';
  const broken = post(s, { action: 'createProject', token: auth.token,
                           name: 'Broken', password: 'broken-secret-1' });
  // Silently leaving it in My Drive is how a misconfigured folder goes unnoticed.
  check('a bad folder id is reported, not swallowed',
        broken.ok === true && /could not file/.test(broken.filedIn), broken.filedIn);
  const ping = get(s, { ping: '1' });
  check('and the ping says the folder is unreachable',
        ping.projectsFolder.configured === true && ping.projectsFolder.reachable === false,
        JSON.stringify(ping.projectsFolder));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\ntidying what already exists into the folder');
  world.props.PROJECTS_FOLDER_ID = 'folder-1';
  world.parents['ctrl'] = ['root'];
  world.parents['sheet-a'] = ['root'];
  world.parents['sheet-b'] = ['someone-elses-drive'];
  world.folders.push('someone-elses-drive');
  world.readonlyParents.push('someone-elses-drive');

  const denied = post(s, { action: 'organiseFiles' });
  check('it cannot be run without authorisation', denied.ok === false);

  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const out = post(s, { action: 'organiseFiles', token: auth.token });
  check('it reports the folder it filed into', out.ok === true && /folder-1/.test(out.folder),
        JSON.stringify(out));
  check('the control sheet moves', /control sheet: moved/.test(out.results.join('|')),
        out.results.join(' | '));
  check('and it really left My Drive', !(world.parents['ctrl'] || []).includes('root'),
        JSON.stringify(world.parents['ctrl']));
  check('a sheet you own moves', /project 'alpha': moved/.test(out.results.join('|')),
        out.results.join(' | '));

  // A client's spreadsheet cannot be taken out of their Drive. Saying "moved"
  // would be a lie, and failing the whole run for it would be unhelpful.
  const stuck = out.results.find(r => /project 'beta'/.test(r));
  check("someone else's sheet is reported honestly, not claimed as moved",
        /stays in its owner's Drive/.test(stuck || ''), stuck);
  check('but it is reachable from the folder anyway',
        (world.parents['sheet-b'] || []).includes('folder-1'),
        JSON.stringify(world.parents['sheet-b']));

  const again = post(s, { action: 'organiseFiles', token: auth.token });
  check('running it twice is safe',
        again.results.filter(r => /already there/.test(r)).length >= 2,
        again.results.join(' | '));

  delete world.props.PROJECTS_FOLDER_ID;
  const unset = post(s, { action: 'organiseFiles', token: auth.token });
  check('with no folder configured it says so',
        unset.ok === false && /PROJECTS_FOLDER_ID/.test(unset.error), unset.error);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nthe rest of the pipeline, without the service-account key');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });

  // Seeded here rather than in makeWorld: the rest of the suite asserts on an
  // empty jobs tab, and rows in it would quietly change what those tests mean.
  const seedTab = w => w.sheets['sheet-a'].sheets.push(new FakeSheet('Jobs_Test', [
    ['Company', 'Role', 'Job Link', 'Job Status'],
    ['Acme', 'Analyst', 'https://example.com/job/1', 'Live'],
    ['Globex', 'Chemist', 'https://example.com/job/2', 'Dead'],
    ['Initech', 'Biologist', 'https://example.com/job/3', ''],
  ]));
  seedTab(world);
  const tabs = () => world.sheets['sheet-a'].getSheetByName('Jobs_Test').rows;

  // Reading a whole tab. Withholding this is what kept the validator, the
  // enricher and the career-page pass tied to the key.
  const jobs = get(s, { action: 'rows', worksheet: 'Jobs_Test', token: auth.token });
  check('any tab of the project can be read',
        jobs.ok === true && jobs.rows.length === 4 &&
        jobs.rows[0][3] === 'Job Status', JSON.stringify(jobs.rows[0]));
  check('every cell arrives as a string',
        jobs.rows.every(r => r.every(c => typeof c === 'string')));
  const absent = get(s, { action: 'rows', worksheet: 'Not_A_Tab', token: auth.token });
  check('a tab that does not exist reads as empty',
        absent.ok === true && absent.rows.length === 0);
  check('and reading it did not create it',
        world.sheets['sheet-a'].getSheetByName('Not_A_Tab') === null);
  const noTab = get(s, { action: 'rows', token: auth.token });
  check('a request with no worksheet is refused, not answered about a guess',
        noTab.ok === false && /worksheet name is required/.test(noTab.error), noTab.error);

  // Another project's tab, with this project's session.
  const other = get(s, { action: 'rows', worksheet: 'Jobs_Test',
                         token: auth.token, project: 'beta' });
  check('a session cannot read another project\'s tabs',
        other.ok === false && other.signedOut === true, JSON.stringify(other));

  // ensureColumn
  const found = post(s, { action: 'ensureColumn', token: auth.token,
                          worksheet: 'Jobs_Test', header: 'Job Status' });
  check('an existing column is found, not added again',
        found.position === 4 && found.added === false, JSON.stringify(found));
  const added = post(s, { action: 'ensureColumn', token: auth.token,
                          worksheet: 'Jobs_Test', header: 'Notes' });
  check('a missing column is added at the end',
        added.position === 5 && added.added === true && tabs()[0][4] === 'Notes',
        JSON.stringify(added));

  // writeColumn
  const wrote = post(s, { action: 'writeColumn', token: auth.token,
                          worksheet: 'Jobs_Test', col: 4, startRow: 2,
                          values: [['A'], ['B'], ['C']] });
  check('a column is written below the header',
        wrote.written === 3 && tabs()[1][3] === 'A' && tabs()[3][3] === 'C',
        JSON.stringify(tabs().map(r => r[3])));
  check('and the header survives', tabs()[0][3] === 'Job Status');
  const flat = post(s, { action: 'writeColumn', token: auth.token,
                         worksheet: 'Jobs_Test', col: 4, startRow: 2,
                         values: ['X', 'Y', 'Z'] });
  check('a flat list of values is accepted too',
        flat.written === 3 && tabs()[1][3] === 'X');
  const past = post(s, { action: 'writeColumn', token: auth.token,
                         worksheet: 'Jobs_Test', col: 4, startRow: 10,
                         values: [['deep']] });
  check('writing past the end of the sheet grows it',
        past.written === 1 && tabs()[9][3] === 'deep', String(tabs().length));
  const badCol = post(s, { action: 'writeColumn', token: auth.token,
                           worksheet: 'Jobs_Test', col: 0, values: [['x']] });
  check('a column number below 1 is refused',
        badCol.ok === false && /1-based/.test(badCol.error), badCol.error);

  // deleteRows
  const before = tabs().length;
  const gone = post(s, { action: 'deleteRows', token: auth.token,
                         worksheet: 'Jobs_Test', rows: [2, 3] });
  check('rows are deleted', gone.deleted === 2 && tabs().length === before - 2,
        JSON.stringify(gone));
  const header = post(s, { action: 'deleteRows', token: auth.token,
                           worksheet: 'Jobs_Test', rows: [1] });
  check('the header row is never deleted', header.deleted === 0 &&
        tabs()[0][0] === 'Company', JSON.stringify(tabs()[0]));

  // Ascending input must still delete the right rows: row 2 going renumbers
  // row 3, so the sort has to happen on this side whatever the caller sent.
  const w2 = makeWorld(); const s2 = buildSandbox(w2); seedProjects(s2, w2);
  seedTab(w2);
  const a2 = get(s2, { action: 'auth', password: 'pw-alpha-secret' });
  post(s2, { action: 'deleteRows', token: a2.token, worksheet: 'Jobs_Test',
             rows: [2, 3] });
  const left = w2.sheets['sheet-a'].getSheetByName('Jobs_Test').rows;
  check('ascending row numbers still delete the intended rows',
        left.length === 2 && left[1][0] === 'Initech',
        JSON.stringify(left.map(r => r[0])));

  // replaceTab
  const replaced = post(s, { action: 'replaceTab', token: auth.token,
                             worksheet: 'Report', rows: [['A', 'B'], ['1', '2']] });
  check('a tab can be replaced wholesale', replaced.rows === 2 &&
        replaced.columns === 2, JSON.stringify(replaced));
  check('and it was created to receive it',
        world.sheets['sheet-a'].getSheetByName('Report') !== null);
  const ragged = post(s, { action: 'replaceTab', token: auth.token,
                           worksheet: 'Report', rows: [['A', 'B', 'C'], ['1']] });
  check('a ragged table is padded rather than rejected',
        ragged.columns === 3 &&
        world.sheets['sheet-a'].getSheetByName('Report').rows[1].length === 3,
        JSON.stringify(ragged));
  const nothing = post(s, { action: 'replaceTab', token: auth.token,
                            worksheet: 'Report', rows: [] });
  check('replacing a tab with nothing is refused',
        nothing.ok === false && /refusing/.test(nothing.error), nothing.error);
  check('so the tab still has its rows',
        world.sheets['sheet-a'].getSheetByName('Report').rows.length === 2);

  // None of it works without a password.
  ['ensureColumn', 'writeColumn', 'deleteRows', 'replaceTab'].forEach(action => {
    const out = post(s, { action: action, worksheet: 'Jobs_Test', col: 1,
                          header: 'x', rows: [2], values: [['x']] });
    check(action + ' needs a password', out.ok === false && out.signedOut === true);
  });
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nan action this deployment does not know is refused, not guessed at');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });

  // A read used to fall through to the Settings tab: a valid-looking answer to
  // a different question. The caller saw an empty jobs tab and did nothing.
  const read = get(s, { action: 'somethingNew', token: auth.token });
  check('an unknown read is refused',
        read.ok === false && read.unknownAction === 'somethingNew', JSON.stringify(read));
  check('and it says what to do about it',
        /deploy a new version/.test(read.error), read.error);
  const settings = get(s, { action: 'settings', token: auth.token });
  check('while a real settings read still works', settings.ok === true &&
        settings.settings.rows.length > 0);

  // A write fell into the settings updater with nothing to update, and
  // answered ok — a column of statuses discarded and reported as written.
  const write = post(s, { action: 'somethingNew', token: auth.token, values: [['x']] });
  check('an unknown write is refused rather than answered ok',
        write.ok === false && /does not know the action/.test(write.error), write.error);

  const save = post(s, { token: auth.token,
                         updates: { 'scraping.max_pages': '7' } });
  check('while the dashboard\'s own settings save is untouched',
        save.ok === true && save.applied.length === 1, JSON.stringify(save));
  const named = post(s, { action: 'saveSettings', token: auth.token,
                          updates: { 'scraping.max_pages': '9' } });
  check('named explicitly, it works too', named.ok === true &&
        named.applied.length === 1, JSON.stringify(named));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nthe run queue');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const runsTab = () => world.sheets['sheet-a'].getSheetByName('Runs');

  const view0 = get(s, { action: 'runs', token: auth.token });
  check('a project with no history has an empty queue',
        view0.ok === true && view0.runs.length === 0, JSON.stringify(view0.runs));
  check('and says no machine has ever connected',
        view0.agent.online === false && view0.agent.everSeen === false,
        JSON.stringify(view0.agent));
  check('the Runs tab is created on demand', runsTab() !== null);

  const bad = post(s, { action: 'requestRun', token: auth.token, mode: 'rm -rf' });
  check('a mode the project does not know is refused',
        bad.ok === false && /not a run mode/.test(bad.error), bad.error);

  const asked = post(s, { action: 'requestRun', token: auth.token,
                          mode: 'scrape-only', requestedBy: 'dashboard' });
  check('a run can be queued', asked.ok === true && asked.queued === true &&
        asked.run.status === 'queued', JSON.stringify(asked));
  check('and it says plainly that nothing is listening yet',
        asked.agentOnline === false && /no machine has ever connected/.test(asked.message),
        asked.message);

  const twice = post(s, { action: 'requestRun', token: auth.token, mode: 'scrape-only' });
  check('queueing the same mode twice does not double it',
        twice.already === true && runsTab().rows.length === 2, twice.message);

  // The agent's half.
  const claimed = post(s, { action: 'claimRun', token: auth.token, agent: 'laptop' });
  check('an agent claims the queued run',
        claimed.run && claimed.run.id === asked.run.id &&
        claimed.run.status === 'running', JSON.stringify(claimed.run));
  check('and is recorded as the machine that has it',
        claimed.run.claimed_by === 'laptop' && claimed.run.started_at !== '');

  const empty = post(s, { action: 'claimRun', token: auth.token, agent: 'laptop' });
  check('a second claim finds nothing left', empty.run === null);

  // The race that matters: the same row must never go to two machines.
  const w2 = makeWorld(); const s2 = buildSandbox(w2); seedProjects(s2, w2);
  const a2 = get(s2, { action: 'auth', password: 'pw-alpha-secret' });
  post(s2, { action: 'requestRun', token: a2.token, mode: 'full' });
  const first = post(s2, { action: 'claimRun', token: a2.token, agent: 'one' });
  const second = post(s2, { action: 'claimRun', token: a2.token, agent: 'two' });
  check('two machines cannot claim the same run',
        first.run !== null && second.run === null,
        JSON.stringify([first.run && first.run.claimed_by, second.run]));

  // Polling is the heartbeat, so the dashboard knows someone is there.
  const view1 = get(s, { action: 'runs', token: auth.token });
  check('polling marks the machine online',
        view1.agent.online === true && view1.agent.agent === 'laptop',
        JSON.stringify(view1.agent));
  check('and the run shows as running',
        view1.runs[0].status === 'running', JSON.stringify(view1.runs[0]));

  const progress = post(s, { action: 'updateRun', token: auth.token,
                             id: asked.run.id, status: 'running',
                             summary: 'running for 20s' });
  check('progress can be reported without finishing',
        progress.ok === true && progress.cancelRequested === false);

  const finished = post(s, { action: 'updateRun', token: auth.token,
                             id: asked.run.id, status: 'done', exitCode: 0,
                             summary: 'finished in 61s' });
  check('and the run can be finished', finished.ok === true);
  const view2 = get(s, { action: 'runs', token: auth.token });
  check('which is what the dashboard then shows',
        view2.runs[0].status === 'done' && view2.runs[0].exit_code === '0' &&
        view2.runs[0].finished_at !== '', JSON.stringify(view2.runs[0]));

  const ghost = post(s, { action: 'updateRun', token: auth.token, id: 'nope',
                          status: 'done' });
  check('an update for a run that does not exist is refused',
        ghost.ok === false && /no run/.test(ghost.error), ghost.error);
  const badStatus = post(s, { action: 'updateRun', token: auth.token,
                              id: asked.run.id, status: 'whatever' });
  check('and so is a status that is not a status',
        badStatus.ok === false && /not a run status/.test(badStatus.error));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\ncancelling, and machines that vanish');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });

  const queued = post(s, { action: 'requestRun', token: auth.token, mode: 'full' });
  const dropped = post(s, { action: 'cancelRun', token: auth.token, id: queued.run.id });
  check('a run that has not started is simply dropped',
        dropped.wasRunning === false, JSON.stringify(dropped));
  const afterDrop = get(s, { action: 'runs', token: auth.token });
  check('and no agent can claim it afterwards',
        afterDrop.runs[0].status === 'cancelled' &&
        post(s, { action: 'claimRun', token: auth.token, agent: 'x' }).run === null);

  // A running one is on somebody's laptop and cannot be killed from here.
  const live = post(s, { action: 'requestRun', token: auth.token, mode: 'scrape-only' });
  post(s, { action: 'claimRun', token: auth.token, agent: 'laptop' });
  const stopping = post(s, { action: 'cancelRun', token: auth.token, id: live.run.id });
  check('a running one is asked to stop rather than killed',
        stopping.wasRunning === true && /finish the step/.test(stopping.message),
        stopping.message);
  const told = post(s, { action: 'updateRun', token: auth.token, id: live.run.id,
                         status: 'running', summary: 'still going' });
  check('and the machine is told at its next progress report',
        told.cancelRequested === true, JSON.stringify(told));

  // A laptop that was closed mid-run leaves a row nothing will ever finish.
  const w2 = makeWorld(); const s2 = buildSandbox(w2); seedProjects(s2, w2);
  const a2 = get(s2, { action: 'auth', password: 'pw-alpha-secret' });
  post(s2, { action: 'requestRun', token: a2.token, mode: 'full' });
  post(s2, { action: 'claimRun', token: a2.token, agent: 'doomed' });
  post(s2, { action: 'requestRun', token: a2.token, mode: 'scrape-only' });

  // Backdate the claim and silence the heartbeat: the machine is gone.
  const sheet = w2.sheets['sheet-a'].getSheetByName('Runs');
  sheet.rows[1][6] = '2020-01-01T00:00:00Z';          // started_at
  delete w2.props['AGENT:alpha'];

  const view = get(s2, { action: 'runs', token: a2.token });
  const lost = view.runs.filter(r => r.status === 'lost');
  check('an abandoned run is marked lost, not left running',
        lost.length === 1 && /stopped reporting/.test(lost[0].summary),
        JSON.stringify(view.runs.map(r => r.status)));
  check('which unblocks what was queued behind it',
        post(s2, { action: 'claimRun', token: a2.token, agent: 'new' }).run.mode
          === 'scrape-only');

  // But a long run whose machine is still polling must be left alone.
  const w3 = makeWorld(); const s3 = buildSandbox(w3); seedProjects(s3, w3);
  const a3 = get(s3, { action: 'auth', password: 'pw-alpha-secret' });
  post(s3, { action: 'requestRun', token: a3.token, mode: 'full' });
  post(s3, { action: 'claimRun', token: a3.token, agent: 'slow' });
  w3.sheets['sheet-a'].getSheetByName('Runs').rows[1][6] = '2020-01-01T00:00:00Z';
  const still = get(s3, { action: 'runs', token: a3.token });
  check('a long run with a live agent is not reaped',
        still.runs[0].status === 'running', JSON.stringify(still.runs[0]));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nthe queue is a project\'s own, like everything else');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  post(s, { action: 'requestRun', token: auth.token, mode: 'full' });

  const other = get(s, { action: 'auth', password: 'pw-beta-secret' });
  const view = get(s, { action: 'runs', token: other.token });
  check("another project's queue is its own, and empty",
        view.ok === true && view.runs.length === 0, JSON.stringify(view.runs));
  check('and its agent status is separate too', view.agent.everSeen === false);

  const noPassword = post(s, { action: 'requestRun', mode: 'full' });
  check('queueing a run needs a password',
        noPassword.ok === false && noPassword.signedOut === true);
  const noPasswordClaim = post(s, { action: 'claimRun', agent: 'anyone' });
  check('and so does claiming one',
        noPasswordClaim.ok === false && noPasswordClaim.signedOut === true);

  // A run row is written from what someone typed into the dashboard.
  const sneaky = post(s, { action: 'requestRun', token: auth.token,
                           mode: 'scrape-only', requestedBy: '=IMPORTRANGE("x","y")' });
  check('a formula in the requester name is written as text',
        world.sheets['sheet-a'].getSheetByName('Runs')
          .rows[2][4].charAt(0) === "'",
        world.sheets['sheet-a'].getSheetByName('Runs').rows[2][4]);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nthe schedule, which replaced the CI cron');
  const settingsOf = id => world.sheets[id].getSheetByName('Settings');

  const quiet = s.scheduledRun();
  check('a project that has not opted in is left alone',
        /alpha: not scheduled/.test(quiet), quiet);
  check('and nothing was queued',
        world.sheets['sheet-a'].getSheetByName('Runs') === null);

  settingsOf('sheet-a').appendRow(['Schedule', 'schedule.enabled', 'true', 'bool', '', '']);
  const ran = s.scheduledRun();
  check('a project that opted in gets a run queued',
        /alpha: queued full/.test(ran), ran);
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const view = get(s, { action: 'runs', token: auth.token });
  check('and it is an ordinary queued run',
        view.runs[0].status === 'queued' && view.runs[0].mode === 'full',
        JSON.stringify(view.runs[0]));
  check('marked as coming from the schedule, not a person',
        view.runs[0].requested_by === 'schedule', view.runs[0].requested_by);

  const again = s.scheduledRun();
  check('a second firing does not stack a duplicate',
        /skipped/.test(again) &&
        get(s, { action: 'runs', token: auth.token }).runs.length === 1, again);

  settingsOf('sheet-a').appendRow(['Schedule', 'schedule.mode', 'validate-only', 'text', '', '']);
  post(s, { action: 'updateRun', token: auth.token, id: view.runs[0].id, status: 'done' });
  s.scheduledRun();
  const after = get(s, { action: 'runs', token: auth.token });
  check('the mode is the project\'s own choice',
        after.runs[0].mode === 'validate-only', after.runs[0].mode);

  // One trigger serves every project, so one broken project must not silence
  // the rest — and a trigger has nobody to show an error to.
  settingsOf('sheet-b').appendRow(['Schedule', 'schedule.enabled', 'true', 'bool', '', '']);
  const w2 = makeWorld(); const s2 = buildSandbox(w2); seedProjects(s2, w2);
  w2.sheets['sheet-a'].getSheetByName('Settings')
    .appendRow(['Schedule', 'schedule.enabled', 'true', 'bool', '', '']);
  w2.sheets['sheet-b'].getSheetByName('Settings')
    .appendRow(['Schedule', 'schedule.enabled', 'true', 'bool', '', '']);
  // beta's spreadsheet disappears — shared, moved to a bin, whatever.
  const rescue = w2.sheets['sheet-b'];
  delete w2.sheets['sheet-b'];
  const mixed = s2.scheduledRun();
  w2.sheets['sheet-b'] = rescue;
  check('a project that fails does not stop the others',
        /alpha: queued full/.test(mixed) && /beta: FAILED/.test(mixed), mixed);
  check('and what happened is written where it can be found',
        (w2.logged || []).join('').indexOf('beta: FAILED') !== -1);

  // An archived project is not a project any more.
  const w3 = makeWorld(); const s3 = buildSandbox(w3); seedProjects(s3, w3);
  w3.sheets['sheet-b'].getSheetByName('Settings')
    .appendRow(['Schedule', 'schedule.enabled', 'true', 'bool', '', '']);
  const archived = s3.scheduledRun();
  check('an archived project is never scheduled',
        archived.indexOf('gone:') === -1, archived);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nthe reply is wrapped only for whoever asked for a wrapper');

  // The dashboard is a page with no CORS header to work with, so it can only
  // read this as a <script> — it always sends a callback and must get JSONP.
  const wrapped = s.doGet({ parameter: { ping: '1', callback: 'cb17' } });
  check('a requested callback wraps the reply',
        /^cb17\(\{.*\}\);$/.test(wrapped.getContent()),
        wrapped.getContent().slice(0, 40));
  check('and is served as javascript', wrapped.mime === 'JAVASCRIPT', wrapped.mime);

  // The pipeline is a program. It fed `x({...});` to a JSON parser and died on
  // the first character, which broke every credential-free run.
  const plain = s.doGet({ parameter: { ping: '1' } });
  check('no callback means plain JSON, not a wrapper',
        plain.getContent().charAt(0) === '{', plain.getContent().slice(0, 40));
  check('and is served as JSON', plain.mime === 'JSON', plain.mime);
  check('which parses without unwrapping anything',
        JSON.parse(plain.getContent()).version === 4);

  // A callback name is interpolated into javascript, so it may only ever be a
  // bare identifier.
  const nasty = s.doGet({ parameter: { ping: '1', callback: 'evil();x' } });
  check('a callback that is not an identifier falls back to a safe name',
        /^callback\(/.test(nasty.getContent()), nasty.getContent().slice(0, 40));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nthe ping reports the projects folder without leaking addresses');
  world.props.PROJECTS_FOLDER_ID = 'folder-1';

  const priv = get(s, { ping: '1' });
  check('a folder shared with nobody counts zero',
        priv.projectsFolder.sharedWith === 0, JSON.stringify(priv.projectsFolder));
  check('and is named, so a wrong folder id is visible',
        priv.projectsFolder.name === 'Projects (folder-1)' &&
        priv.projectsFolder.reachable === true,
        JSON.stringify(priv.projectsFolder));

  // The team folder: shared on purpose, so every sheet in it is shared too.
  // Reported as a count, because a jump in it is worth noticing.
  world.folderEditors['folder-1'] = ['one@example.com', 'two@example.com'];
  world.folderViewers['folder-1'] = ['three@example.com'];
  const shared = get(s, { ping: '1' });
  check('viewers and editors are both counted',
        shared.projectsFolder.sharedWith === 3,
        String(shared.projectsFolder.sharedWith));
  // The ping needs no password, so it must not hand out addresses.
  check('but never the folder\'s addresses',
        !JSON.stringify(shared.projectsFolder).includes('@'),
        JSON.stringify(shared.projectsFolder));
  check('and the owner\'s own address is masked',
        shared.runsAs === 'o***r@example.com', shared.runsAs);

  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const made = post(s, { action: 'createProject', token: auth.token, name: 'Filed',
                         password: 'exposed-secret-1' });
  check('filing a new sheet there just names the folder',
        made.filedIn === 'Projects (folder-1)', made.filedIn);
  check('and the project is created', made.ok === true);

  const unreachable = makeWorld(); const s2 = buildSandbox(unreachable);
  seedProjects(s2, unreachable);
  unreachable.props.PROJECTS_FOLDER_ID = 'no-such-folder';
  const broken = get(s2, { ping: '1' });
  check('an unreachable folder is reported, not silently ignored',
        broken.projectsFolder.configured === true &&
        broken.projectsFolder.reachable === false &&
        Boolean(broken.projectsFolder.error),
        JSON.stringify(broken.projectsFolder));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nthe creator gets their own sheet, and only theirs');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });

  const made = post(s, { action: 'createProject', token: auth.token, name: 'Theirs',
                         password: 'theirs-secret-1', ownerEmail: 'client@example.com' });
  check('the creator is granted editor on their sheet',
        (world.editors[made.spreadsheetId] || []).includes('client@example.com'),
        JSON.stringify(world.editors[made.spreadsheetId]));
  check('and it is reported back', made.grantedTo === 'client@example.com', made.grantedTo);

  // The whole point: a per-file grant, never a shared folder that every
  // creator can read, which would defeat the password separating projects.
  const others = Object.keys(world.editors)
    .filter(id => id !== made.spreadsheetId)
    .filter(id => (world.editors[id] || []).includes('client@example.com'));
  check('they are granted nothing on any other project', others.length === 0,
        JSON.stringify(others));

  const anon = post(s, { action: 'createProject', token: auth.token, name: 'NoEmail',
                         password: 'noemail-secret-1' });
  check('the email is optional', anon.ok === true && anon.grantedTo === '');

  const bad = post(s, { action: 'createProject', token: auth.token, name: 'BadEmail',
                        password: 'bademail-secret-1', ownerEmail: 'not-an-email' });
  check('a malformed address is refused rather than silently skipped',
        bad.ok === false && /email address/.test(bad.error), bad.error);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nadopting an existing sheet is privileged');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  world.sheets['adopt-me'] = new FakeSpreadsheet('adopt-me', [new FakeSheet('Jobs', [['Company']])]);

  // Creating a sheet makes an empty one. Adopting names a sheet that already
  // exists, and this script can open any sheet its owner can — so a tenant
  // holding one project's password must not be able to point a project of
  // their own at somebody else's data.
  const unprivileged = post(s, { action: 'createProject', token: auth.token, name: 'Adopted',
                                 password: 'adopted-secret', spreadsheetId: 'adopt-me' });
  check('a project password alone cannot adopt a sheet',
        unprivileged.ok === false && /ADMIN_PASSWORD/.test(unprivileged.error), unprivileged.error);
  check('and nothing was registered', _projectCount(world) === 3, String(_projectCount(world)));

  const stolen = post(s, { action: 'createProject', token: auth.token, name: 'Steal',
                           password: 'steal-secret-1', spreadsheetId: 'sheet-b' });
  check('another project\'s spreadsheet cannot be claimed', stolen.ok === false);

  world.props.ADMIN_PASSWORD = 'admin-only-pw';
  const wrongAdmin = post(s, { action: 'createProject', adminPassword: 'nope', name: 'Adopted',
                               password: 'adopted-secret', spreadsheetId: 'adopt-me' });
  check('a wrong admin password cannot adopt', wrongAdmin.ok === false);

  const control = post(s, { action: 'createProject', adminPassword: 'admin-only-pw', name: 'Registry',
                            password: 'registry-secret', spreadsheetId: 'ctrl' });
  check('the control spreadsheet cannot be registered as a project',
        control.ok === false && /control spreadsheet/.test(control.error), control.error);

  const taken = post(s, { action: 'createProject', adminPassword: 'admin-only-pw', name: 'Dup',
                          password: 'dup-secret-1', spreadsheetId: 'sheet-b' });
  check('a spreadsheet already in use cannot be adopted twice',
        taken.ok === false && /already uses that spreadsheet/.test(taken.error), taken.error);

  const made = post(s, { action: 'createProject', adminPassword: 'admin-only-pw', name: 'Adopted',
                         password: 'adopted-secret', spreadsheetId: 'adopt-me' });
  check('with the admin password it works', made.ok === true && made.createdSheet === false,
        JSON.stringify(made));
  const tabs = world.sheets['adopt-me'].getSheets().map(x => x.name);
  check('missing tabs are added', tabs.includes('Settings') && tabs.includes('Keywords'));
  check('the existing tab keeps its header',
        world.sheets['adopt-me'].getSheetByName('Jobs').rows[0][0] === 'Company');

  const missing = post(s, { action: 'createProject', adminPassword: 'admin-only-pw', name: 'Ghost',
                            password: 'ghost-secret', spreadsheetId: 'does-not-exist' });
  check('an unreachable sheet is refused rather than registered', missing.ok === false);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nnames are written as text, not formulas');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  // appendRow writes as if typed, and a formula in the control sheet would run
  // as its owner — with IMPORTRANGE and friends available.
  const made = post(s, { action: 'createProject', token: auth.token,
                         password: 'formula-secret-1',
                         name: '=IMPORTRANGE("other","A1")' });
  check('a project is still created', made.ok === true, JSON.stringify(made));
  const row = world.sheets['ctrl'].getSheetByName('Projects').rows.slice(-1)[0];
  check('but the name is neutralised with a leading apostrophe',
        String(row[1]).charAt(0) === "'", JSON.stringify(row[1]));
  const notes = post(s, { action: 'createProject', token: auth.token, name: 'Notes',
                          password: 'notes-secret-1', notes: '+1+1' });
  const notesRow = world.sheets['ctrl'].getSheetByName('Projects').rows.slice(-1)[0];
  check('and so are notes', String(notesRow[8]).charAt(0) === "'", JSON.stringify(notesRow[8]));
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\narchiving ends the sessions too');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  check('the session works while active', get(s, { token: auth.token }).ok === true);
  const control = world.sheets['ctrl'].getSheetByName('Projects');
  const row = control.rows.findIndex(r => r[0] === 'alpha');
  control.rows[row][3] = 'archived';
  check('and stops the moment the project is archived',
        get(s, { token: auth.token }).ok === false);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nhanding the pipeline its inputs');
  const sheet = world.sheets['sheet-a'];
  sheet.getSheetByName('Jobs').name = 'Jobs';
  sheet.sheets.push(new FakeSheet('Jobs_Test', [
    ['Company', 'Role', 'Job Link'],
    ['Acme', 'Microbiologist', 'https://example.com/job/1'],
    ['Beta', 'Analyst', 'https://example.com/job/2'],
  ]));
  sheet.sheets.push(new FakeSheet('Company', [
    ['Company', 'Linkedin-Url'],
    ['Acme', 'https://linkedin.com/company/acme'],
    ['NoUrl', ''],
  ]));

  const anon = get(s, { action: 'inputs' });
  check('inputs need a password', anon.ok === false);

  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const inputs = get(s, { action: 'inputs', token: auth.token });
  check('it answers for the right project', inputs.ok === true && inputs.project === 'alpha');
  check('the jobs tab is read from that project\'s settings',
        inputs.jobsWorksheet === 'Jobs_Test', inputs.jobsWorksheet);
  check('keywords come across', inputs.keywords.join() === 'data analyst,ml engineer',
        inputs.keywords.join());
  check('the settings tab comes across whole',
        inputs.settingsRows.length === 4 && inputs.settingsRows[0][1] === 'Setting',
        String(inputs.settingsRows.length));
  check('every settings cell is a string',
        inputs.settingsRows.every(r => r.every(c => typeof c === 'string')));
  check('the company map skips rows with no url',
        Object.keys(inputs.companyLinkedIn).join() === 'acme',
        Object.keys(inputs.companyLinkedIn).join());

  // The machine running the pipeline must not be handed the jobs already
  // collected — only enough to skip them.
  check('already-seen links come back as hashes', inputs.existingLinkHashes.length === 2);
  check('and not as URLs', !JSON.stringify(inputs.existingLinkHashes).includes('example.com'),
        JSON.stringify(inputs.existingLinkHashes));
  check('nor does anything else leak the sheet id',
        !JSON.stringify(inputs).includes('sheet-a'));

  const crossed = get(s, { action: 'inputs', token: auth.token, project: 'beta' });
  check('a session cannot ask for another project\'s inputs', crossed.ok === false);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\ntaking the results back');
  const sheet = world.sheets['sheet-a'];
  sheet.sheets.push(new FakeSheet('Jobs_Test', [
    ['Company', 'Role', 'Job Link'],
    ['Old', 'Analyst', 'https://example.com/job/1'],
  ]));
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const jobs = () => world.sheets['sheet-a'].getSheetByName('Jobs_Test');

  const anon = post(s, { action: 'appendJobs', rows: [{ Company: 'X' }] });
  check('appending needs a password', anon.ok === false);

  const added = post(s, { action: 'appendJobs', token: auth.token, worksheet: 'Jobs_Test',
    rows: [{ Company: 'New', Role: 'Scientist', 'Job Link': 'https://example.com/job/9' }] });
  check('a new row is written', added.ok === true && added.added === 1, JSON.stringify(added));
  check('into the right tab', jobs().rows.length === 3, String(jobs().rows.length));
  check('aligned to the tab\'s own header',
        jobs().rows[2][0] === 'New' && jobs().rows[2][2] === 'https://example.com/job/9',
        JSON.stringify(jobs().rows[2]));

  // The caller's idea of what exists is a snapshot from the start of a run that
  // may have taken an hour, so this is the check that can see the sheet now.
  const dupe = post(s, { action: 'appendJobs', token: auth.token, worksheet: 'Jobs_Test',
    rows: [{ Company: 'Old again', 'Job Link': 'https://example.com/job/1' }] });
  check('a link already in the sheet is skipped',
        dupe.added === 0 && dupe.duplicates === 1, JSON.stringify(dupe));
  check('and the sheet did not grow', jobs().rows.length === 3);

  const within = post(s, { action: 'appendJobs', token: auth.token, worksheet: 'Jobs_Test',
    rows: [{ Company: 'A', 'Job Link': 'https://example.com/dup' },
           { Company: 'B', 'Job Link': 'https://example.com/dup' }] });
  check('duplicates inside one batch are caught too',
        within.added === 1 && within.duplicates === 1, JSON.stringify(within));

  // A sheet with extra or reordered columns must be filled, not shifted.
  jobs().rows[0] = ['Job Link', 'Company', 'Role', 'Keyword'];
  const reordered = post(s, { action: 'appendJobs', token: auth.token, worksheet: 'Jobs_Test',
    rows: [{ Company: 'Ordered', Role: 'Chemist', 'Job Link': 'https://example.com/o',
             Keyword: 'chemistry' }] });
  const last = jobs().rows[jobs().rows.length - 1];
  check('a reordered header is respected',
        reordered.ok === true && last[0] === 'https://example.com/o' && last[1] === 'Ordered',
        JSON.stringify(last));

  const missing = post(s, { action: 'appendJobs', token: auth.token,
                            worksheet: 'NoSuchTab', rows: [{ Company: 'X' }] });
  check('a missing tab is an error, not a silent no-op', missing.ok === false, missing.error);

  const none = post(s, { action: 'appendJobs', token: auth.token, worksheet: 'Jobs_Test',
                         rows: [] });
  check('an empty batch is harmless', none.ok === true && none.added === 0);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\ncopying a project');
  world.props.PROJECTS_FOLDER_ID = 'folder-1';
  // Give the source some results and some inherited sharing.
  const src = world.sheets['sheet-a'];
  src.getSheetByName('Jobs').rows.push(['Acme', 'Engineer'], ['Beta', 'Analyst']);
  world.editors['sheet-a'] = ['someone.from.before@example.com'];
  world.viewers['sheet-a'] = ['a.viewer@example.com'];

  const anon = post(s, { action: 'copyProject', name: 'Nope', password: 'nope-secret-1' });
  check('a copy needs the source project open', anon.ok === false);

  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const copy = post(s, { action: 'copyProject', token: auth.token,
                         name: 'Alpha Clone', password: 'clone-secret-1' });
  check('it copies', copy.ok === true && copy.copiedFrom === 'alpha', JSON.stringify(copy));
  check('into the projects folder', /folder-1/.test(copy.filedIn), copy.filedIn);
  check('the copy is its own project', copy.project === 'alpha-clone');

  const opened = get(s, { action: 'auth', password: 'clone-secret-1' });
  check('and opens with its own password', opened.ok === true && opened.project === 'alpha-clone');
  check('with its own data key, not the source\'s',
        opened.dataKey !== get(s, { action: 'auth', password: 'pw-alpha-secret' }).dataKey);

  const copied = world.sheets[copy.spreadsheetId];
  check('the configuration comes across',
        copied.getSheetByName('Settings').rows.length === src.getSheetByName('Settings').rows.length);
  check('and the keywords', copied.getSheetByName('Keywords').rows.length === 3);
  // Inheriting several thousand scraped rows would look like the new project's
  // own findings.
  check('but the scraped results are emptied',
        copied.getSheetByName('Jobs').rows.length === 1, JSON.stringify(copy.cleared));
  check('and it says what it cleared', /Jobs/.test(copy.cleared.join(',')), copy.cleared.join(','));

  // A Drive copy inherits the source's sharing, which would hand the new
  // project to whoever could read the old one.
  check('the source\'s editors do not come with it',
        !(world.editors[copy.spreadsheetId] || []).includes('someone.from.before@example.com'),
        JSON.stringify(world.editors[copy.spreadsheetId]));
  check('nor its viewers',
        !(world.viewers[copy.spreadsheetId] || []).includes('a.viewer@example.com'),
        JSON.stringify(world.viewers[copy.spreadsheetId]));
  check('but the service account is kept',
        (world.editors[copy.spreadsheetId] || []).includes('bot@example.iam.gserviceaccount.com'));

  const kept = post(s, { action: 'copyProject', token: auth.token, name: 'With Results',
                         password: 'withresults-1', includeResults: true });
  check('results can be kept on request',
        world.sheets[kept.spreadsheetId].getSheetByName('Jobs').rows.length === 3,
        String(world.sheets[kept.spreadsheetId].getSheetByName('Jobs').rows.length));

  const dup = post(s, { action: 'copyProject', token: auth.token, name: 'Dup',
                        password: 'pw-beta-secret' });
  check('a password another project uses is refused', dup.ok === false);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\ndeleting a project');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const before = _projectCount(world);

  const noConfirm = post(s, { action: 'deleteProject', token: auth.token,
                              password: 'pw-alpha-secret', confirmName: 'Alpha Ltd' });
  check('confirm:true is required', noConfirm.ok === false && /confirm/.test(noConfirm.error));

  const noName = post(s, { action: 'deleteProject', token: auth.token, confirm: true,
                           password: 'pw-alpha-secret', confirmName: 'wrong name' });
  check('the name must be typed exactly',
        noName.ok === false && /type the project name/.test(noName.error), noName.error);

  // A borrowed session must not be able to destroy someone's project.
  const noPassword = post(s, { action: 'deleteProject', token: auth.token, confirm: true,
                               confirmName: 'Alpha Ltd' });
  check('the password is required even with a valid token',
        noPassword.ok === false && /password/.test(noPassword.error), noPassword.error);
  check('nothing was deleted by any of those', _projectCount(world) === before);

  const gone = post(s, { action: 'deleteProject', token: auth.token, confirm: true,
                         password: 'pw-alpha-secret', confirmName: 'alpha ltd' });
  check('with all three it deletes', gone.ok === true && gone.deleted === 'alpha',
        JSON.stringify(gone));
  check('the row is removed', _projectCount(world) === before - 1);
  check('its password no longer opens anything',
        get(s, { action: 'auth', password: 'pw-alpha-secret' }).ok === false);
  check('and its session is dead', get(s, { token: auth.token }).ok === false);

  // The sheet is very often somebody's only copy of that work.
  check('the spreadsheet is left alone by default',
        world.trashed['sheet-a'] !== true && /left untouched/.test(gone.sheetNote),
        gone.sheetNote);
  check('the other project is untouched',
        get(s, { action: 'auth', password: 'pw-beta-secret' }).ok === true);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\ndeleting, and binning the sheet too');
  const auth = get(s, { action: 'auth', password: 'pw-beta-secret' });
  const gone = post(s, { action: 'deleteProject', token: auth.token, confirm: true,
                         password: 'pw-beta-secret', confirmName: 'Beta Corp',
                         trashSheet: true });
  check('the sheet goes to the bin when asked',
        gone.ok === true && world.trashed['sheet-b'] === true);
  check('and it says it is recoverable', /30 days/.test(gone.sheetNote), gone.sheetNote);

  // A sheet that cannot be binned is no reason to leave the project reachable.
  const world2 = makeWorld(); const s2 = buildSandbox(world2); seedProjects(s2, world2);
  world2.untrashable.push('sheet-a');
  const auth2 = get(s2, { action: 'auth', password: 'pw-alpha-secret' });
  const partial = post(s2, { action: 'deleteProject', token: auth2.token, confirm: true,
                             password: 'pw-alpha-secret', confirmName: 'Alpha Ltd',
                             trashSheet: true });
  check('the project still goes when the sheet cannot be binned',
        partial.ok === true && partial.sheetTrashed === false, JSON.stringify(partial));
  check('and it says why', /could not be binned/.test(partial.sheetNote), partial.sheetNote);
}

{
  const world = makeWorld(); const s = buildSandbox(world); seedProjects(s, world);
  console.log('\nchanging a password');
  const auth = get(s, { action: 'auth', password: 'pw-alpha-secret' });
  const noCurrent = post(s, { token: auth.token, action: 'changePassword',
                              currentPassword: 'wrong', newPassword: 'a-new-secret' });
  check('the current password is always required', noCurrent.ok === false);
  const taken = post(s, { token: auth.token, action: 'changePassword',
                          currentPassword: 'pw-alpha-secret', newPassword: 'pw-beta-secret' });
  check('another project\'s password cannot be taken', taken.ok === false);
  const done = post(s, { token: auth.token, action: 'changePassword',
                         currentPassword: 'pw-alpha-secret', newPassword: 'a-new-secret' });
  check('the change succeeds', done.ok === true && done.sessionsRevoked === true);
  check('the old session is revoked', get(s, { token: auth.token }).ok === false);
  const again = get(s, { action: 'auth', password: 'a-new-secret' });
  check('the new password works', again.ok === true && again.project === 'alpha');
  check('and the data key is unchanged, so published files still decrypt',
        again.dataKey === 'key-alpha');
  check('the old password no longer works',
        get(s, { action: 'auth', password: 'pw-alpha-secret' }).ok === false);
}

{
  const world = makeWorld(); const s = buildSandbox(world);
  console.log('\nsetup mistakes read as setup mistakes');
  delete world.props.CONTROL_SHEET_ID;
  const p = get(s, { ping: '1' });
  check('a missing CONTROL_SHEET_ID is named in the ping', p.controlSheetConfigured === false);
  const a = get(s, { action: 'auth', password: 'anything' });
  check('and in the sign-in error', /CONTROL_SHEET_ID/.test(a.error), a.error);
  world.props.CONTROL_SHEET_ID = 'not-a-real-id';
  const q = get(s, { ping: '1' });
  check('an unreachable control sheet is reported, not thrown',
        q.controlSheetReadable === false && /no such spreadsheet/.test(q.error || ''));
}

console.log('\n' + passed + ' passed, ' + failed + ' failed\n');
process.exit(failed ? 1 : 0);
