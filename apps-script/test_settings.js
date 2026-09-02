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
  setFrozenRows(n) { this.frozen = n; }
  insertRowsAfter(_after, n) { for (let i = 0; i < n; i++) this.rows.push([]); }
  appendRow(values) { this.rows.push(values.slice()); }
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
    ContentService: {
      MimeType: { JAVASCRIPT: 'js', JSON: 'json' },
      createTextOutput: text => ({ text, setMimeType() { return this; }, getContent() { return text; } }),
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
                  parents: {}, readonlyParents: [] };

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
