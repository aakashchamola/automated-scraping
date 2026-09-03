/**
 * Settings.gs — the published dashboard's server side. STANDALONE, multi-project.
 *
 * The dashboard is a static page on GitHub Pages with no credentials, so it
 * cannot talk to the Sheets API. This Web App is the missing half: it runs as
 * you, so it can read and write every spreadsheet you own.
 *
 * ── WHY STANDALONE ─────────────────────────────────────────────────────────
 * The previous version was bound to one spreadsheet and used
 * getActiveSpreadsheet(). A bound script can only ever see its own container,
 * which makes more than one project impossible — and in a standalone project
 * that call returns null, which is the "Cannot read properties of null" error.
 * Everything here opens sheets by id instead.
 *
 * ── THE CONTROL SHEET IS THE DATABASE ──────────────────────────────────────
 * One registry spreadsheet has a Projects tab listing every project and where
 * its data lives. Nothing about a project is stored in this file, so adding one
 * never means editing or redeploying this script.
 *
 *   id  name  spreadsheet_id  status  data_key  pw_salt  pw_hash  created_at  notes
 *
 * Two secrets per project, and they are deliberately different things:
 *
 *   data_key   encrypts the published dashboard files. Generated once, never
 *              changed — rotating it would strand every file already published.
 *              Handed to the browser only after the password checks out.
 *   pw_hash    what the operator types, salted and iterated. Changeable freely,
 *              precisely because it is not the data key.
 *
 * The password selects the project: there is no project list to browse without
 * one, so opening the URL reveals no project names.
 *
 * ── WHY SERVICE ACCOUNTS CANNOT CREATE THE SHEETS ──────────────────────────
 * A service account on a consumer Google account has no Drive storage quota, so
 * creating a spreadsheet as one fails with "storage quota exceeded". This
 * script runs as you, so it creates each project sheet in YOUR Drive, owned by
 * you, and then adds the service account as an editor itself. That is why
 * provisioning lives here and not on the Python side.
 *
 * ── TWO CORS RULES, both learned the hard way ──────────────────────────────
 *   1. A Web App's /exec response carries NO Access-Control-Allow-Origin, so a
 *      cors-mode fetch that READS the response fails with ERR_FAILED. Writes
 *      must be sent no-cors (fire-and-forget, response unreadable).
 *   2. Because a write's result cannot be read, the caller confirms by reading
 *      back — and that read must be JSONP (a <script> tag), which CORS does
 *      not apply to.
 *
 * So doGet serves JSONP for every read, and doPost writes and says nothing the
 * caller can hear.
 *
 * ── SET UP ONCE ────────────────────────────────────────────────────────────
 * It never needs editing again. Settings are read and written by whatever rows
 * the Settings tab happens to contain, keyed by the Setting column, so adding
 * settings later is a change to that tab — never to this file.
 *
 *   1. script.google.com -> New project  (NOT Extensions -> Apps Script, which
 *      would create a bound script again).
 *   2. Paste this file in, replacing everything.
 *   3. Project Settings -> Script Properties:
 *        CONTROL_SHEET_ID    id of the registry spreadsheet   (required)
 *        SERVICE_ACCOUNT     the service account's email      (recommended)
 *        PROJECTS_FOLDER_ID  Drive folder for new sheets      (optional)
 *        ADMIN_PASSWORD      required to create projects      (optional)
 *   4. Deploy -> New deployment -> Web app
 *        Execute as:      Me
 *        Who has access:  Anyone
 *   5. Authorise it. Because it is standalone it asks for Drive and Sheets
 *      access, so Google shows "Google hasn't verified this app" —
 *      Advanced -> Go to <name> (unsafe). It is your own script.
 *   6. Check it: open <the /exec URL>?ping=1 in a browser.
 *   7. gh secret set SETTINGS_WEB_APP_URL   (paste the /exec URL)
 */

var CONTROL_TAB = 'Projects';
var CONTROL_HEADER = ['id', 'name', 'spreadsheet_id', 'status', 'data_key',
                      'pw_salt', 'pw_hash', 'created_at', 'notes'];

var SHEET_NAME = 'Settings';
var KEY_COLUMN = 'Setting';
var VALUE_COLUMN = 'Value';

// The Keywords tab drives every scrape. Editing it from the dashboard means
// the search terms can change without opening the spreadsheet.
var KEYWORDS_SHEET = 'Keywords';
var KEYWORDS_COLUMN = 'Search Term';

var TOKEN_TTL_MS = 10 * 24 * 60 * 60 * 1000;   // matches "stay signed in"
var TOKEN_PROPERTY = 'SESSION_TOKENS';
var MIN_PASSWORD_LENGTH = 8;

// Kept in step with HASH_ROUNDS in projects_registry.py. Changing it
// invalidates every stored hash, so it is a constant, not a setting.
var HASH_ROUNDS = 1000;

// Tabs a brand-new project starts with. Headers only — the Settings rows are
// filled in by the pipeline on its first run, from the schema it already owns,
// so a new setting in the code never means editing this list.
var PROJECT_TEMPLATE = [
  { name: 'Jobs',      header: ['Company', 'Role', 'Location', 'Platform', 'Job Link', 'Keyword'] },
  { name: 'Company',   header: ['Company', 'Avg. Employee-Count', 'Career-Page', 'Linkedin-Url', 'Job Link'] },
  { name: 'Companies', header: ['Company', 'Avg. Employee-Count', 'Career-Page', 'Linkedin-Url', 'Job Link'] },
  { name: 'Keywords',  header: [KEYWORDS_COLUMN] },
  { name: 'Settings',  header: ['Group', 'Setting', 'Value', 'Type', 'Options', 'Description'] }
];


function _props() {
  return PropertiesService.getScriptProperties();
}

function _controlId() {
  return _props().getProperty('CONTROL_SHEET_ID') || '';
}

function _serviceAccount() {
  return _props().getProperty('SERVICE_ACCOUNT') || '';
}

/**
 * Which account this deployment runs as, masked.
 *
 * Enough to tell two accounts apart — which is what it is for; a script
 * deployed under the wrong account is a confusing failure, because it can
 * publish a project's data perfectly while being unable to open its
 * spreadsheet. But ?ping=1 needs no password, so the full address would be
 * handed to anyone with the URL, and it is the owner's own address.
 */
function _effectiveUser() {
  try {
    var email = Session.getEffectiveUser().getEmail();
    if (!email) return '';
    var at = email.indexOf('@');
    if (at < 2) return '***' + email.substring(at);
    return email.charAt(0) + '***' + email.charAt(at - 1) + email.substring(at);
  } catch (err) {
    // Needs the userinfo.email scope, which is not granted by default, so it
    // must never be allowed to fail the ping.
    return '';
  }
}

function _adminPassword() {
  return _props().getProperty('ADMIN_PASSWORD') || '';
}


/* ── Password hashing ───────────────────────────────────────────────────────
   Salted, iterated SHA-256 over hex strings — hex at every step, rather than
   raw bytes, so this matches projects_registry.py exactly without any
   byte-signedness games. It is not PBKDF2 because Apps Script has no PBKDF2;
   that is an acceptable trade only because the hash lives in a private
   spreadsheet whose reader can already read every project's data directly. */

function _sha256Hex(text) {
  var bytes = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8);
  var hex = '';
  for (var i = 0; i < bytes.length; i++) {
    // computeDigest returns signed bytes; mask back to 0-255 before hexing.
    var b = (bytes[i] + 256) % 256;
    hex += (b < 16 ? '0' : '') + b.toString(16);
  }
  return hex;
}

function _hashPassword(password, salt) {
  var digest = _sha256Hex(salt + ':' + password);
  for (var i = 1; i < HASH_ROUNDS; i++) digest = _sha256Hex(digest);
  return digest;
}

/** Constant-time comparison so a wrong value cannot be found by timing. */
function _constantTimeEquals(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function _passwordMatches(project, given) {
  if (!project || !given) return false;
  if (!project.pw_hash || !project.pw_salt) return false;
  return _constantTimeEquals(project.pw_hash, _hashPassword(given, project.pw_salt));
}


/* ── The control sheet ──────────────────────────────────────────────────── */

function _controlSheet() {
  var id = _controlId();
  if (!id) {
    throw new Error('CONTROL_SHEET_ID is not set. Project Settings -> ' +
                    'Script Properties -> add CONTROL_SHEET_ID.');
  }
  var sheet = SpreadsheetApp.openById(id).getSheetByName(CONTROL_TAB);
  if (!sheet) throw new Error("the control spreadsheet has no '" + CONTROL_TAB + "' tab");
  return sheet;
}

/** Every project row, with its 1-based sheet row number attached. */
function _projects() {
  var values = _controlSheet().getDataRange().getValues();
  if (!values.length) return [];
  var header = values[0].map(function (h) { return String(h).trim(); });
  var out = [];
  for (var r = 1; r < values.length; r++) {
    var row = {};
    for (var c = 0; c < header.length; c++) {
      if (header[c]) row[header[c]] = String(values[r][c]);
    }
    if (!String(row.id || '').trim()) continue;
    row._row = r + 1;
    out.push(row);
  }
  return out;
}

function _activeProjects() {
  return _projects().filter(function (p) {
    return String(p.status || '').toLowerCase() !== 'archived';
  });
}

function _projectById(id) {
  var wanted = String(id || '').trim().toLowerCase();
  var found = _projects().filter(function (p) {
    return String(p.id).trim().toLowerCase() === wanted;
  });
  return found.length ? found[0] : null;
}

/**
 * The project a password unlocks, or null.
 *
 * Every active project is checked even after a match, so the time taken does
 * not reveal how far down the list the answer was.
 */
function _projectByPassword(password) {
  if (!password) return null;
  var found = null;
  _activeProjects().forEach(function (p) {
    if (_passwordMatches(p, password) && found === null) found = p;
  });
  return found;
}


/* ── Sessions ───────────────────────────────────────────────────────────────
   A token is issued once the password checks out, and the page remembers that
   instead of the password. So staying signed in never means keeping what
   someone typed. Each token is bound to one project — holding a session for
   one project grants nothing on another. */

function _tokens() {
  try {
    return JSON.parse(_props().getProperty(TOKEN_PROPERTY) || '{}');
  } catch (err) {
    return {};
  }
}

function _saveTokens(map) {
  _props().setProperty(TOKEN_PROPERTY, JSON.stringify(map));
}

function _issueToken(projectId) {
  var map = _tokens();
  var now = Date.now();
  Object.keys(map).forEach(function (t) {
    if (!map[t] || map[t].exp < now) delete map[t];
  });
  var token = Utilities.getUuid().replace(/-/g, '') +
              Utilities.getUuid().replace(/-/g, '');
  map[token] = { exp: now + TOKEN_TTL_MS, project: projectId };
  _saveTokens(map);
  return token;
}

/** The project id a token is good for, or ''. */
function _tokenProject(token) {
  if (!token) return '';
  var map = _tokens();
  var entry = map[token];
  if (!entry) return '';
  if (entry.exp < Date.now()) {
    delete map[token];
    _saveTokens(map);
    return '';
  }
  return entry.project || '';
}

function _revokeProjectTokens(projectId) {
  var map = _tokens();
  Object.keys(map).forEach(function (t) {
    if (map[t] && map[t].project === projectId) delete map[t];
  });
  _saveTokens(map);
}

/**
 * The project this request is authorised for, or null.
 *
 * A live token is the normal path; the password is accepted directly so that a
 * caller which has not signed in yet still works in one round trip.
 */
function _authorise(params) {
  var viaToken = _tokenProject(params.token || '');
  if (viaToken) {
    var project = _projectById(viaToken);
    // Archiving is how a project is taken away, so it has to end the sessions
    // too — otherwise a token issued beforehand keeps working indefinitely.
    if (project && String(project.status || '').toLowerCase() !== 'archived') {
      return project;
    }
  }
  return _projectByPassword(params.password || '');
}

/** Why a request was refused, so a setup mistake reads as a setup mistake. */
function _authError(params) {
  if (!_controlId()) {
    return 'CONTROL_SHEET_ID is not set. Project Settings -> Script Properties ' +
           '-> add CONTROL_SHEET_ID.';
  }
  if (!_activeProjects().length) {
    return 'the control sheet lists no active projects';
  }
  if (!(params.password || params.token)) return 'no password sent';
  return 'no project matched that password';
}


/* ── A project's own sheets ─────────────────────────────────────────────── */

function _projectSheet(project, name) {
  if (!project.spreadsheet_id) {
    throw new Error("project '" + project.id + "' has no spreadsheet_id");
  }
  var sheet = SpreadsheetApp.openById(project.spreadsheet_id).getSheetByName(name);
  if (!sheet) {
    throw new Error("no '" + name + "' tab in the sheet for project '" + project.id + "'");
  }
  return sheet;
}

function _readAll(project) {
  var values = _projectSheet(project, SHEET_NAME).getDataRange().getValues();
  if (!values.length) return { columns: [], rows: [] };
  var header = values[0].map(function (h) { return String(h).trim(); });
  var rows = [];
  for (var r = 1; r < values.length; r++) {
    var row = {};
    var blank = true;
    for (var c = 0; c < header.length; c++) {
      if (!header[c]) continue;
      row[header[c]] = String(values[r][c]);
      if (String(values[r][c]).trim()) blank = false;
    }
    if (!blank) rows.push(row);
  }
  return { columns: header.filter(String), rows: rows };
}

/**
 * Apply {setting: value} to the Value column. Only rows that already exist are
 * touched: the tab is generated from the pipeline's schema, so an unknown key
 * is a stale client rather than a new setting, and silently appending it would
 * create a row nothing ever reads.
 */
function _applyUpdates(project, updates) {
  var sheet = _projectSheet(project, SHEET_NAME);
  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function (h) { return String(h).trim(); });
  var keyAt = header.indexOf(KEY_COLUMN);
  var valueAt = header.indexOf(VALUE_COLUMN);
  if (keyAt < 0 || valueAt < 0) {
    throw new Error("'" + SHEET_NAME + "' needs both a " + KEY_COLUMN +
                    ' and a ' + VALUE_COLUMN + ' column');
  }

  var rowOf = {};
  for (var r = 1; r < values.length; r++) {
    var key = String(values[r][keyAt]).trim();
    if (key) rowOf[key] = r + 1;          // 1-indexed for getRange
  }

  var applied = [], unknown = [], unchanged = [];
  Object.keys(updates).forEach(function (path) {
    var rowNumber = rowOf[path];
    if (!rowNumber) { unknown.push(path); return; }
    var next = String(updates[path]);
    var current = String(values[rowNumber - 1][valueAt]);
    if (current === next) { unchanged.push(path); return; }
    sheet.getRange(rowNumber, valueAt + 1).setValue(next);
    applied.push(path + ': ' + current + ' -> ' + next);
  });
  return { applied: applied, unknown: unknown, unchanged: unchanged };
}


/* ── Keywords ───────────────────────────────────────────────────────────────
   A plain list rather than key/value, so it is read and written as a whole
   column. Only that one column is touched: the tab carries per-platform
   columns beside it that nothing here should disturb. */

function _readKeywords(project) {
  var sheet = _projectSheet(project, KEYWORDS_SHEET);
  var values = sheet.getDataRange().getValues();
  if (!values.length) return { keywords: [] };
  var header = values[0].map(function (h) { return String(h).trim(); });
  var at = header.indexOf(KEYWORDS_COLUMN);
  if (at < 0) {
    throw new Error("'" + KEYWORDS_SHEET + "' has no '" + KEYWORDS_COLUMN + "' column");
  }
  var out = [];
  for (var r = 1; r < values.length; r++) {
    var term = String(values[r][at] || '').trim();
    if (term) out.push(term);
  }
  return { keywords: out, column: KEYWORDS_COLUMN, sheet: KEYWORDS_SHEET };
}

/**
 * Replace the whole Search Term column with *list*.
 *
 * Rewriting the column rather than diffing rows keeps add, edit, reorder and
 * delete as one operation. Cells below the new list are blanked rather than
 * the rows being deleted, so the neighbouring per-platform columns keep their
 * alignment with whatever is left.
 */
function _writeKeywords(project, list) {
  var clean = [];
  (list || []).forEach(function (raw) {
    var term = String(raw || '').trim();
    // Duplicates would scrape the same search twice for no benefit.
    if (term && clean.indexOf(term) === -1) clean.push(term);
  });
  if (!clean.length) throw new Error('refusing to leave the Keywords tab empty');

  var sheet = _projectSheet(project, KEYWORDS_SHEET);
  var values = sheet.getDataRange().getValues();
  var header = values[0].map(function (h) { return String(h).trim(); });
  var at = header.indexOf(KEYWORDS_COLUMN);
  if (at < 0) {
    throw new Error("'" + KEYWORDS_SHEET + "' has no '" + KEYWORDS_COLUMN + "' column");
  }

  var previous = values.length - 1;              // data rows currently present
  var needed = Math.max(clean.length, previous);
  var column = [];
  for (var i = 0; i < needed; i++) {
    column.push([i < clean.length ? clean[i] : '']);
  }
  if (sheet.getMaxRows() < needed + 1) {
    sheet.insertRowsAfter(sheet.getMaxRows(), needed + 1 - sheet.getMaxRows());
  }
  sheet.getRange(2, at + 1, needed, 1).setValues(column);
  return { count: clean.length, cleared: Math.max(0, previous - clean.length) };
}


/* ── Running the pipeline somewhere else ────────────────────────────────────
   The scraping does not need Google credentials — it needs the keywords to
   search for, the settings to obey, and somewhere to put what it finds. Those
   three things can be handed over and taken back through here, which means a
   machine running the pipeline holds nothing but the project's password.

   The alternative is passing out the service-account key, and that key can
   read and write every sheet it has ever been shared with. There is no way to
   scope it to one project, so it can never leave the owner.

   Unlike the dashboard, the caller here is a program rather than a browser, so
   none of the CORS rules above apply: it can POST and read the reply directly.
   That is why these two actions are ordinary request/response.                */

var LINK_HASH_CHARS = 12;   // 48 bits; collisions across a few thousand are nil

/** One setting's value from a project's Settings tab, or a default. */
function _settingValue(rows, key, fallback) {
  if (!rows || !rows.length) return fallback;
  var header = rows[0].map(function (h) { return String(h).trim(); });
  var keyAt = header.indexOf(KEY_COLUMN);
  var valueAt = header.indexOf(VALUE_COLUMN);
  if (keyAt < 0 || valueAt < 0) return fallback;
  for (var r = 1; r < rows.length; r++) {
    if (String(rows[r][keyAt]).trim() === key) {
      var value = String(rows[r][valueAt]).trim();
      if (value) return value;
    }
  }
  return fallback;
}

/** A column's values from a tab, by header name. */
function _columnValues(sheet, headerName) {
  if (!sheet) return [];
  var values = sheet.getDataRange().getValues();
  if (values.length < 2) return [];
  var header = values[0].map(function (h) { return String(h).trim(); });
  var at = header.indexOf(headerName);
  if (at < 0) return [];
  var out = [];
  for (var r = 1; r < values.length; r++) {
    out.push(String(values[r][at] == null ? '' : values[r][at]).trim());
  }
  return out;
}

function _linkHash(url) {
  return _sha256Hex(String(url || '').trim()).substring(0, LINK_HASH_CHARS);
}

/**
 * Everything the pipeline needs to read, in one round trip.
 *
 * Already-seen job links come back as short hashes rather than URLs: it is all
 * the caller needs in order to skip duplicates, it keeps the response small,
 * and it means a machine running the pipeline never receives the list of jobs
 * already collected.
 */
function _pipelineInputs(project) {
  var spreadsheet = SpreadsheetApp.openById(project.spreadsheet_id);

  var settingsSheet = spreadsheet.getSheetByName(SHEET_NAME);
  var settingsRows = settingsSheet ? settingsSheet.getDataRange().getValues() : [];
  // Dates and numbers would arrive as objects the caller cannot use.
  settingsRows = settingsRows.map(function (row) {
    return row.map(function (cell) { return String(cell == null ? '' : cell); });
  });

  var jobsTab = _settingValue(settingsRows, 'google_sheets.jobs_worksheet', 'Jobs');
  var companyTab = _settingValue(settingsRows,
    'google_sheets.company_sheet.worksheet', 'Company');
  var companyCol = _settingValue(settingsRows,
    'google_sheets.company_sheet.company_column', 'Company');
  var linkedinCol = _settingValue(settingsRows,
    'google_sheets.company_sheet.linkedin_url_column', 'Linkedin-Url');

  var jobsSheet = spreadsheet.getSheetByName(jobsTab);
  var jobsHeader = [];
  var existing = [];
  if (jobsSheet) {
    var jobsValues = jobsSheet.getDataRange().getValues();
    if (jobsValues.length) {
      jobsHeader = jobsValues[0].map(function (h) { return String(h).trim(); })
        .filter(String);
      var linkAt = jobsValues[0].map(function (h) { return String(h).trim(); })
        .indexOf('Job Link');
      if (linkAt >= 0) {
        for (var r = 1; r < jobsValues.length; r++) {
          var url = String(jobsValues[r][linkAt] || '').trim();
          if (url) existing.push(_linkHash(url));
        }
      }
    }
  }

  var companySheet = spreadsheet.getSheetByName(companyTab);
  var names = _columnValues(companySheet, companyCol);
  var urls = _columnValues(companySheet, linkedinCol);
  var companyLinkedIn = {};
  for (var i = 0; i < names.length; i++) {
    if (names[i] && urls[i]) companyLinkedIn[names[i].toLowerCase()] = urls[i];
  }

  return {
    project: project.id,
    settingsRows: settingsRows,
    keywords: _readKeywords(project).keywords,
    jobsWorksheet: jobsTab,
    jobsHeader: jobsHeader,
    existingLinkHashes: existing,
    linkHashChars: LINK_HASH_CHARS,
    companyLinkedIn: companyLinkedIn
  };
}

/**
 * Append scraped rows to a project's jobs tab.
 *
 * Deduplicated here as well as by the caller. The caller's copy of what already
 * exists is a snapshot from the start of a run that may have taken an hour, so
 * this is the only check that can see the sheet as it is now — and it is the
 * one that matters, since two machines can be running at once.
 *
 * Rows arrive as objects keyed by column name and are aligned to whatever
 * header the tab actually has, so a sheet with extra or reordered columns is
 * filled correctly rather than shifted.
 */
function _appendJobs(project, body) {
  var rows = body.rows || [];
  if (!rows.length) return { added: 0, skipped: 0, duplicates: 0 };

  var spreadsheet = SpreadsheetApp.openById(project.spreadsheet_id);
  var settingsSheet = spreadsheet.getSheetByName(SHEET_NAME);
  var settingsRows = settingsSheet ? settingsSheet.getDataRange().getValues() : [];
  var jobsTab = String(body.worksheet || '').trim() ||
                _settingValue(settingsRows, 'google_sheets.jobs_worksheet', 'Jobs');

  var sheet = spreadsheet.getSheetByName(jobsTab);
  if (!sheet) throw new Error("no '" + jobsTab + "' tab in this project's spreadsheet");

  var values = sheet.getDataRange().getValues();
  var header = (values[0] || []).map(function (h) { return String(h).trim(); });
  if (!header.filter(String).length) {
    throw new Error("'" + jobsTab + "' has no header row");
  }
  var linkAt = header.indexOf('Job Link');

  var seen = {};
  if (linkAt >= 0) {
    for (var r = 1; r < values.length; r++) {
      var url = String(values[r][linkAt] || '').trim();
      if (url) seen[url] = true;
    }
  }

  var toAppend = [];
  var duplicates = 0;
  rows.forEach(function (record) {
    var link = String(record['Job Link'] || '').trim();
    if (link && seen[link]) { duplicates++; return; }
    if (link) seen[link] = true;          // also within this batch
    toAppend.push(header.map(function (column) {
      var cell = record[column];
      return cell == null ? '' : String(cell);
    }));
  });

  if (toAppend.length) {
    // One write for the batch. Appending row by row would spend an API call
    // each and meet the per-minute write quota within a few hundred rows.
    sheet.getRange(sheet.getLastRow() + 1, 1, toAppend.length, header.length)
         .setValues(toAppend);
  }

  return {
    worksheet: jobsTab, added: toAppend.length, duplicates: duplicates,
    received: rows.length, total: sheet.getLastRow() - 1
  };
}

/* ── The rest of the pipeline, credential-free ──────────────────────────────
   `inputs` and `appendJobs` cover the scrape. The other stages — validation,
   enrichment, the mismatch and classifier passes, career pages, row cleanup —
   read arbitrary tabs and write columns back, so without these they were the
   one part that still demanded the service-account key. That key cannot be
   scoped to a single project, so needing it for four of the eight run modes
   meant "run it on your own machine" was only ever half true.

   These grant nothing the password did not already grant: the dashboard can
   read and write this project's Settings and Keywords with the same password,
   and every one of these actions is confined to the project the password
   selects. What they add is reach across that one project's own tabs.        */

/** Every row of one of this project's tabs, as strings. */
function _tabRows(project, name) {
  var tab = String(name || '').trim();
  if (!tab) throw new Error('a worksheet name is required');
  var sheet = SpreadsheetApp.openById(project.spreadsheet_id).getSheetByName(tab);
  // Absent is empty, not an error: the Sheets-API store creates a missing tab
  // on demand and reads back nothing, and a read should not make a tab either
  // way. The write actions below do create one.
  if (!sheet) return [];
  return sheet.getDataRange().getValues().map(function (row) {
    return row.map(function (cell) { return String(cell == null ? '' : cell); });
  });
}

/** The tab a request means, defaulting to the project's jobs tab. */
function _tabOrJobs(project, name) {
  var tab = String(name || '').trim();
  if (tab) return tab;
  var spreadsheet = SpreadsheetApp.openById(project.spreadsheet_id);
  var settings = spreadsheet.getSheetByName(SHEET_NAME);
  var rows = settings ? settings.getDataRange().getValues() : [];
  return _settingValue(rows, 'google_sheets.jobs_worksheet', 'Jobs');
}

function _sheetForWriting(project, name) {
  var spreadsheet = SpreadsheetApp.openById(project.spreadsheet_id);
  var tab = _tabOrJobs(project, name);
  var sheet = spreadsheet.getSheetByName(tab);
  if (!sheet) sheet = spreadsheet.insertSheet(tab);
  return sheet;
}

/**
 * The 1-based position of a header, adding the column if it is missing.
 *
 * How the validator finds its "Job Status" column on a sheet that has never
 * been validated before.
 */
function _ensureColumn(project, body) {
  var header = String(body.header || '').trim();
  if (!header) throw new Error('a header name is required');
  var sheet = _sheetForWriting(project, body.worksheet);
  var values = sheet.getDataRange().getValues();
  var row = (values[0] || []).map(function (h) { return String(h).trim(); });
  var at = row.indexOf(header);
  if (at >= 0) return { worksheet: sheet.getName(), position: at + 1, added: false };
  var position = row.length + 1;
  sheet.getRange(1, position).setValue(header);
  return { worksheet: sheet.getName(), position: position, added: true };
}

/**
 * Write a column in ONE call.
 *
 * The whole column at once rather than a cell at a time, for the reason the
 * Sheets-API side batches too: 60 writes a minute per user means per-row
 * updates start failing a few hundred rows in, and the failures were being
 * logged and discarded under a summary that read like success.
 */
function _writeColumn(project, body) {
  var col = parseInt(body.col, 10);
  if (!(col >= 1)) throw new Error('col must be a 1-based column number');
  var startRow = parseInt(body.startRow, 10) || 2;
  if (startRow < 1) throw new Error('startRow must be 1 or more');

  // write_column_values sends [[v], [v], …]; a flat list is accepted too.
  var values = (body.values || []).map(function (v) {
    return [String((Array.isArray(v) ? v[0] : v) == null
      ? '' : (Array.isArray(v) ? v[0] : v))];
  });
  if (!values.length) return { worksheet: _tabOrJobs(project, body.worksheet), written: 0 };

  var sheet = _sheetForWriting(project, body.worksheet);
  var needed = startRow + values.length - 1;
  if (sheet.getMaxRows() < needed) {
    sheet.insertRowsAfter(sheet.getMaxRows(), needed - sheet.getMaxRows());
  }
  if (sheet.getMaxColumns() < col) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), col - sheet.getMaxColumns());
  }
  sheet.getRange(startRow, col, values.length, 1).setValues(values);
  return { worksheet: sheet.getName(), written: values.length,
           col: col, startRow: startRow };
}

/**
 * Replace a tab's whole contents with *rows*.
 *
 * Two passes wanted exactly this — the pagination report and the Settings
 * seeder — and both did it by clearing a gspread worksheet and writing A1
 * downwards. It is the one thing that genuinely needed a live worksheet
 * handle, so having it here is what lets the remote store drop open_worksheet
 * altogether.
 *
 * Cleared and rewritten rather than diffed: the caller is rebuilding the tab,
 * and a partial overwrite would leave whatever the old table had further down.
 */
function _replaceTab(project, body) {
  var rows = body.rows || [];
  if (!rows.length) throw new Error('refusing to replace a tab with nothing');

  var width = 0;
  var table = rows.map(function (row) {
    var cells = (row || []).map(function (c) { return String(c == null ? '' : c); });
    if (cells.length > width) width = cells.length;
    return cells;
  });
  if (!width) throw new Error('refusing to replace a tab with empty rows');
  // setValues demands a rectangle; a ragged table is padded, not rejected.
  table.forEach(function (cells) {
    while (cells.length < width) cells.push('');
  });

  var sheet = _sheetForWriting(project, body.worksheet);
  sheet.clear();
  if (sheet.getMaxRows() < table.length) {
    sheet.insertRowsAfter(sheet.getMaxRows(), table.length - sheet.getMaxRows());
  }
  if (sheet.getMaxColumns() < width) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
  }
  sheet.getRange(1, 1, table.length, width).setValues(table);
  if (body.freezeHeader !== false) {
    try { sheet.setFrozenRows(1); } catch (err) { /* cosmetic */ }
  }
  return { worksheet: sheet.getName(), rows: table.length, columns: width };
}

/**
 * Delete rows by their 1-based sheet row numbers.
 *
 * Descending, always: deleting row 5 renumbers everything below it, so working
 * downwards would take out the wrong rows after the first one. The caller sorts
 * too — this does not trust it to have.
 */
function _deleteRows(project, body) {
  var wanted = (body.rows || []).map(function (n) { return parseInt(n, 10); })
    .filter(function (n) { return n >= 2; });          // never the header
  if (!wanted.length) return { worksheet: _tabOrJobs(project, body.worksheet), deleted: 0 };

  var sheet = _sheetForWriting(project, body.worksheet);
  var last = sheet.getLastRow();
  var unique = {};
  wanted.forEach(function (n) { if (n <= last) unique[n] = true; });
  var ordered = Object.keys(unique).map(Number).sort(function (a, b) { return b - a; });

  var deleted = 0;
  ordered.forEach(function (n) {
    try { sheet.deleteRow(n); deleted++; } catch (err) { /* reported by count */ }
  });
  return { worksheet: sheet.getName(), deleted: deleted, requested: wanted.length };
}


/**
 * Which tabs this project has, and how big they are.
 *
 * The dashboard's index. It used to come from an encrypted file that a
 * scheduled CI job wrote, which meant the page showed whatever the last
 * successful publish contained and needed a runner to stay current. Read from
 * here it is simply what the spreadsheet says right now.
 *
 * Deliberately cheap: getLastRow and the header row only, never the contents.
 * A project with tens of thousands of rows must not make opening the dashboard
 * slow, and the rows are fetched per tab when one is actually looked at.
 */
function _projectTabs(project) {
  var spreadsheet = SpreadsheetApp.openById(project.spreadsheet_id);
  var worksheets = spreadsheet.getSheets().map(function (sheet) {
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    var columns = [];
    if (lastRow >= 1 && lastCol >= 1) {
      columns = sheet.getRange(1, 1, 1, lastCol).getValues()[0]
        .map(function (h) { return String(h).trim(); });
      while (columns.length && !columns[columns.length - 1]) columns.pop();
    }
    return {
      name: sheet.getName(),
      // Data rows, header excluded — the count the dashboard shows.
      row_count: Math.max(0, lastRow - 1),
      columns: columns.filter(String)
    };
  });
  return {
    capturedAt: new Date().toISOString(),
    spreadsheetId: project.spreadsheet_id,
    worksheets: worksheets
  };
}


/* ── The run queue ──────────────────────────────────────────────────────────
   What lets the pipeline run on someone's own machine while the website stays
   the way you ask for it.

   The website cannot start a process on a laptop, and the laptop cannot accept
   an incoming connection — it is behind a router, asleep half the time, and on
   a different address every week. So neither calls the other. Both talk to the
   sheet: the page appends a row saying what it wants, and the machine polls for
   one and claims it. That is the whole mechanism, and it works through any
   firewall because both ends only ever make outbound requests.

   The queue lives in a Runs tab in the project's own spreadsheet, for the same
   reason everything else does: it is visible, editable and debuggable without
   this script, and a project's history belongs to the project.

   Claiming is the one part that must be exactly right. Two machines polling the
   same project would otherwise both take the same row and scrape everything
   twice, so a claim reads and writes under a script lock and re-reads the row's
   status inside it — the check and the write have to be one indivisible step. */

var RUNS_TAB = 'Runs';
var RUNS_HEADER = ['id', 'mode', 'status', 'requested_at', 'requested_by',
                   'claimed_by', 'started_at', 'finished_at', 'exit_code',
                   'summary'];

// Every mode the dashboard offers. Named here so a typo or a stale page cannot
// queue a run that no agent knows how to carry out — it would sit there
// forever looking like the machine was offline.
var RUN_MODES = ['full', 'scrape-only', 'career-pages-only', 'enrich-only',
                 'validate-only', 'mismatch-only', 'classify-only',
                 'pagination-only', 'cleanup-rows', 'publish-only'];

// A run left "running" by a machine that was closed, slept or lost power would
// otherwise block the queue for good. Nothing is force-killed: it is marked
// lost once the agent has stopped saying it is alive.
var RUN_STALE_MS = 15 * 60 * 1000;

// How recently an agent must have polled to count as listening. Comfortably
// more than the poll interval, so one slow round trip does not read as offline.
var AGENT_ONLINE_MS = 90 * 1000;

// The Runs tab is history, not an archive. Trimmed from the top so the newest
// are always kept.
var RUNS_KEEP = 300;

function _runsSheet(project) {
  var spreadsheet = SpreadsheetApp.openById(project.spreadsheet_id);
  var sheet = spreadsheet.getSheetByName(RUNS_TAB);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(RUNS_TAB);
    sheet.getRange(1, 1, 1, RUNS_HEADER.length).setValues([RUNS_HEADER]);
    sheet.setFrozenRows(1);
    return sheet;
  }
  // A tab that exists but has no header — someone cleared it — is repaired
  // rather than failing every run from then on.
  var first = sheet.getRange(1, 1, 1, RUNS_HEADER.length).getValues()[0];
  if (first.every(function (v) { return !String(v).trim(); })) {
    sheet.getRange(1, 1, 1, RUNS_HEADER.length).setValues([RUNS_HEADER]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

/** Runs newest first, each with its sheet row number. */
function _runRows(project) {
  var values = _runsSheet(project).getDataRange().getValues();
  if (values.length < 2) return [];
  var header = values[0].map(function (h) { return String(h).trim(); });
  var out = [];
  for (var r = 1; r < values.length; r++) {
    var run = {};
    for (var c = 0; c < header.length; c++) {
      if (header[c]) run[header[c]] = String(values[r][c] == null ? '' : values[r][c]);
    }
    if (!String(run.id || '').trim()) continue;
    run._row = r + 1;
    out.push(run);
  }
  return out.reverse();
}

function _writeRun(project, run, changes) {
  var sheet = _runsSheet(project);
  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(function (h) { return String(h).trim(); });
  Object.keys(changes).forEach(function (field) {
    var at = header.indexOf(field);
    if (at >= 0) sheet.getRange(run._row, at + 1).setValue(changes[field]);
  });
}

function _now() {
  return new Date().toISOString().replace(/\.\d+Z$/, 'Z');
}

/**
 * A run's age in milliseconds, or Infinity when it has no usable timestamp.
 *
 * Infinity rather than 0 deliberately: a row with a corrupt started_at should
 * read as long dead and be cleared, not as brand new and blocking forever.
 */
function _ageMs(stamp) {
  var parsed = Date.parse(String(stamp || ''));
  if (isNaN(parsed)) return Infinity;
  return Date.now() - parsed;
}

/* ── The agent's heartbeat ──────────────────────────────────────────────────
   In a script property rather than a sheet cell. The agent polls every few
   seconds, and a spreadsheet write per poll would be both slow and a steady
   drain on the per-minute write quota that the actual runs need. */

function _agentKey(projectId) {
  return 'AGENT:' + projectId;
}

function _recordHeartbeat(projectId, agent, version) {
  _props().setProperty(_agentKey(projectId), JSON.stringify({
    agent: String(agent || 'unnamed'), at: Date.now(),
    version: String(version || '')
  }));
}

function _agentStatus(projectId) {
  var raw = _props().getProperty(_agentKey(projectId));
  if (!raw) return { online: false, everSeen: false };
  var seen;
  try {
    seen = JSON.parse(raw);
  } catch (err) {
    return { online: false, everSeen: false };
  }
  var since = Date.now() - (seen.at || 0);
  return {
    online: since < AGENT_ONLINE_MS,
    everSeen: true,
    agent: seen.agent || '',
    version: seen.version || '',
    secondsAgo: Math.round(since / 1000)
  };
}

/**
 * Runs left behind by a machine that stopped without saying so.
 *
 * A laptop that was closed mid-run leaves a row saying "running" that nothing
 * will ever finish, and while it sits there a queued run behind it never
 * starts. Anything running with no agent alive and no progress for long enough
 * is marked lost, which unblocks the queue without pretending it succeeded.
 */
function _reapStaleRuns(project) {
  var agent = _agentStatus(project.id);
  if (agent.online) return 0;          // still alive; it is simply a long run
  var reaped = 0;
  _runRows(project).forEach(function (run) {
    if (run.status !== 'running' && run.status !== 'cancelling') return;
    if (_ageMs(run.started_at) < RUN_STALE_MS) return;
    _writeRun(project, run, {
      status: 'lost', finished_at: _now(),
      summary: 'the machine running this stopped reporting; it may or may not ' +
               'have finished. Nothing was killed — check the sheet.'
    });
    reaped++;
  });
  return reaped;
}

/** Queue a run. The dashboard's half of the exchange. */
function _requestRun(project, body) {
  var mode = String(body.mode || '').trim();
  if (RUN_MODES.indexOf(mode) === -1) {
    throw new Error("'" + mode + "' is not a run mode this project knows");
  }

  var sheet = _runsSheet(project);
  var existing = _runRows(project);

  // One at a time. Queueing the same mode twice while it is already waiting is
  // almost always a double click, and two full scrapes at once would fight
  // over the same sheet.
  var waiting = existing.filter(function (run) {
    return (run.status === 'queued' || run.status === 'running') && run.mode === mode;
  });
  if (waiting.length) {
    return { queued: false, already: true, run: _publicRun(waiting[0]),
             message: "'" + mode + "' is already " + waiting[0].status };
  }

  var id = 'r' + Date.now().toString(36) + Utilities.getUuid().substring(0, 4);
  var record = {
    id: id, mode: mode, status: 'queued', requested_at: _now(),
    requested_by: _plainText(String(body.requestedBy || 'dashboard')),
    claimed_by: '', started_at: '', finished_at: '', exit_code: '', summary: ''
  };
  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(function (h) { return String(h).trim(); });
  sheet.appendRow(header.map(function (col) {
    return record.hasOwnProperty(col) ? record[col] : '';
  }));

  // Trim oldest-first, after appending, so history never grows without bound.
  var total = sheet.getLastRow() - 1;
  if (total > RUNS_KEEP) sheet.deleteRows(2, total - RUNS_KEEP);

  var agent = _agentStatus(project.id);
  return {
    queued: true, run: record, agentOnline: agent.online,
    message: agent.online
      ? 'queued — your machine should pick it up within a few seconds'
      : (agent.everSeen
          ? 'queued, but no machine is listening right now. It will start as ' +
            'soon as the agent is running again.'
          : 'queued, but no machine has ever connected to this project. Run ' +
            'the agent on the machine that should do the work.')
  };
}

/**
 * Take the oldest queued run, or answer that there is none.
 *
 * Also the agent's heartbeat, deliberately: it polls constantly, and folding
 * "I am alive" into the same request halves the traffic and means a machine
 * cannot look online while failing to actually poll for work.
 */
function _claimRun(project, body) {
  var agent = String(body.agent || 'unnamed');
  _recordHeartbeat(project.id, agent, body.version);
  var reaped = _reapStaleRuns(project);

  var queued = _runRows(project).filter(function (run) {
    return run.status === 'queued';
  });
  if (!queued.length) return { run: null, reaped: reaped };

  // Oldest first: _runRows is newest-first, so the last one is the oldest.
  var chosen = queued[queued.length - 1];

  // Re-read this row inside the lock the caller holds. The list above came
  // from a read that another agent may already have acted on, and claiming on
  // a stale status is exactly how two machines end up running the same job.
  var fresh = _runRows(project).filter(function (run) {
    return run.id === chosen.id;
  })[0];
  if (!fresh || fresh.status !== 'queued') return { run: null, reaped: reaped };

  var startedAt = _now();
  _writeRun(project, fresh, {
    status: 'running', claimed_by: _plainText(agent), started_at: startedAt
  });
  // Mirrored onto the object as well, because that is what is sent back — and
  // the agent uses started_at to report how long it has been going.
  fresh.status = 'running';
  fresh.claimed_by = agent;
  fresh.started_at = startedAt;
  return { run: _publicRun(fresh), reaped: reaped };
}

/** Report progress or completion. */
function _updateRun(project, body) {
  var id = String(body.id || '').trim();
  if (!id) throw new Error('a run id is required');
  var run = _runRows(project).filter(function (r) { return r.id === id; })[0];
  if (!run) throw new Error("no run '" + id + "' in this project");

  var allowed = ['running', 'done', 'failed', 'cancelled'];
  var changes = {};
  if (body.status) {
    if (allowed.indexOf(String(body.status)) === -1) {
      throw new Error("'" + body.status + "' is not a run status");
    }
    var next = String(body.status);
    // A progress report must never overwrite 'cancelling'. It arrives every
    // twenty seconds saying 'running', and letting it win means the request to
    // stop is erased moments after it is made and the machine is never told.
    if (!(next === 'running' && run.status === 'cancelling')) {
      changes.status = next;
      if (next !== 'running') changes.finished_at = _now();
    }
  }
  if (body.summary !== undefined) {
    // Sheets refuses a cell over 50,000 characters, and a summary is a summary.
    changes.summary = _plainText(String(body.summary).substring(0, 4000));
  }
  if (body.exitCode !== undefined && body.exitCode !== null) {
    changes.exit_code = String(body.exitCode);
  }
  _writeRun(project, run, changes);

  // Answered on every update so a long run learns it was cancelled without
  // needing a separate call: the agent stops at the next checkpoint.
  var after = _runRows(project).filter(function (r) { return r.id === id; })[0];
  return { updated: id, cancelRequested: after && after.status === 'cancelling' };
}

/**
 * Ask for a run to stop.
 *
 * A queued run is simply dropped. A running one cannot be killed from here —
 * the process is on somebody's laptop — so it is marked cancelling and the
 * agent is told at its next update. It stops at a checkpoint rather than being
 * torn down mid-write, which is what you want when the thing it is writing to
 * is a spreadsheet.
 */
function _cancelRun(project, body) {
  var id = String(body.id || '').trim();
  var run = _runRows(project).filter(function (r) { return r.id === id; })[0];
  if (!run) throw new Error("no run '" + id + "' in this project");

  if (run.status === 'queued') {
    _writeRun(project, run, { status: 'cancelled', finished_at: _now(),
                              summary: 'cancelled before it started' });
    return { cancelled: id, wasRunning: false };
  }
  if (run.status === 'running') {
    _writeRun(project, run, { status: 'cancelling' });
    return { cancelled: id, wasRunning: true,
             message: 'asked the machine to stop; it will finish the step it ' +
                      'is on first' };
  }
  return { cancelled: id, wasRunning: false,
           message: 'that run had already ' + run.status };
}

/** The fields worth sending on. `_row` is bookkeeping and would only confuse. */
function _publicRun(run) {
  var out = {};
  RUNS_HEADER.forEach(function (field) { out[field] = run[field] || ''; });
  return out;
}

/** Recent history plus whether a machine is listening — the dashboard's view. */
function _runsView(project, params) {
  var limit = parseInt(params.limit, 10) || 20;
  _reapStaleRuns(project);
  var runs = _runRows(project).slice(0, Math.min(limit, 100)).map(_publicRun);
  return {
    runs: runs, modes: RUN_MODES, agent: _agentStatus(project.id),
    onlineWithinSec: Math.round(AGENT_ONLINE_MS / 1000)
  };
}


/* ── Creating a project ─────────────────────────────────────────────────────
   Everything a new project needs, in one call: the spreadsheet, its tabs, the
   service account's editor grant, and the registry row. Nothing is left for
   anyone to do by hand in the Drive UI. */

function _randomKey() {
  // Two UUIDs of entropy, base64 without padding — the same shape as
  // secrets.token_urlsafe(32) on the Python side.
  var raw = Utilities.getUuid().replace(/-/g, '') +
            Utilities.getUuid().replace(/-/g, '');
  return Utilities.base64EncodeWebSafe(raw).replace(/=+$/, '');
}

/**
 * Neutralise a value that a person chose, before it is written to a sheet.
 *
 * appendRow writes as if typed, so a name beginning with = + - or @ becomes a
 * live formula in the control sheet — and a formula there runs as the owner,
 * with IMPORTRANGE and friends available. A leading apostrophe makes Sheets
 * treat it as text; it is not shown in the cell.
 */
function _plainText(value) {
  var text = String(value == null ? '' : value);
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}

function _slugify(name) {
  var slug = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-')
             .replace(/^-+|-+$/g, '');
  return (slug || 'project').substring(0, 32);
}

function _uniqueId(base) {
  var candidate = base, n = 2;
  while (_projectById(candidate)) {
    candidate = (base + '-' + n).substring(0, 32);
    n++;
  }
  return candidate;
}

/** Add any missing template tab, and give a tab a header if it has none. */
function _ensureTemplateTabs(spreadsheet) {
  PROJECT_TEMPLATE.forEach(function (tpl) {
    var sheet = spreadsheet.getSheetByName(tpl.name);
    if (!sheet) sheet = spreadsheet.insertSheet(tpl.name);
    var first = sheet.getRange(1, 1, 1, tpl.header.length).getValues()[0];
    var empty = first.every(function (v) { return !String(v).trim(); });
    if (empty) {
      sheet.getRange(1, 1, 1, tpl.header.length).setValues([tpl.header]);
      sheet.setFrozenRows(1);
    }
  });
  // A brand-new spreadsheet arrives with a stray "Sheet1" that nothing uses.
  var stray = spreadsheet.getSheetByName('Sheet1');
  if (stray && spreadsheet.getSheets().length > 1 && stray.getLastRow() === 0) {
    spreadsheet.deleteSheet(stray);
  }
}

/**
 * Move a newly created sheet into the configured projects folder.
 *
 * Returns the folder's name on success, '' when no folder is configured, and a
 * message beginning "could not file" when it failed. It never throws: a sheet
 * that exists in the wrong place is recoverable, one that failed to be created
 * is not. But it does report, because the previous version logged the failure
 * where nobody would ever read it and left the sheet in My Drive looking fine.
 */
function _fileIntoProjectsFolder(spreadsheetId) {
  var folderId = _props().getProperty('PROJECTS_FOLDER_ID') || '';
  if (!folderId) return '';
  try {
    var file = DriveApp.getFileById(spreadsheetId);
    var folder = DriveApp.getFolderById(folderId);
    folder.addFile(file);
    // addFile ADDS a parent; without this the sheet stays in My Drive as well.
    DriveApp.getRootFolder().removeFile(file);
    return folder.getName();
  } catch (err) {
    return 'could not file it into PROJECTS_FOLDER_ID: ' + err;
  }
}

/**
 * The projects folder: its name, whether this deployment can reach it, and how
 * many people it is shared with.
 *
 * The count is there because it is the folder every project sheet is filed
 * into, and a file inherits its folder's sharing — so it says, in one number,
 * how many people can open all of them. That is deliberate here: the folder is
 * the shared team workspace, and the passwords separate projects in the app,
 * not in Drive. It is reported so a wrong folder id is visible, and because a
 * jump in the number is worth noticing.
 *
 * A count, never the addresses: this goes into the unauthenticated ping.
 */
function _projectsFolderStatus() {
  var folderId = _props().getProperty('PROJECTS_FOLDER_ID') || '';
  if (!folderId) return { configured: false };
  try {
    var folder = DriveApp.getFolderById(folderId);
    var status = { configured: true, reachable: true, name: folder.getName() };
    try {
      status.sharedWith =
        folder.getEditors().length + folder.getViewers().length;
    } catch (err) {
      // Listing who has a folder needs more than opening it.
      status.sharedWith = null;
    }
    return status;
  } catch (err) {
    return { configured: true, reachable: false, error: String(err) };
  }
}

/**
 * Move one file into *folder*, detaching it from wherever it was.
 *
 * addFile ADDS a parent rather than moving, so the old one has to be removed or
 * the file shows up in both places. Parents are collected before the add, or
 * the new folder would be in the list and promptly removed again.
 *
 * A parent that belongs to somebody else cannot be detached — Drive does not
 * let an editor move a file out of the owner's Drive — so that is caught and
 * reported rather than thrown. The file ends up reachable from the folder
 * either way, which is what was wanted.
 */
function _moveIntoFolder(fileId, folder) {
  var file = DriveApp.getFileById(fileId);
  var previous = [];
  var parents = file.getParents();
  while (parents.hasNext()) previous.push(parents.next());

  var already = previous.some(function (p) { return p.getId() === folder.getId(); });
  if (already) return { moved: false, already: true };

  folder.addFile(file);
  var detached = 0, stuck = 0;
  previous.forEach(function (parent) {
    try {
      parent.removeFile(file);
      detached++;
    } catch (err) {
      stuck++;          // someone else's Drive; the file is in both places now
    }
  });
  return { moved: true, detached: detached, stuck: stuck };
}

/**
 * Put the control sheet and every project sheet into PROJECTS_FOLDER_ID.
 *
 * For tidying up what already exists — new sheets are filed as they are
 * created. Reports every file individually: a sheet owned by someone else
 * usually cannot be moved, and that is worth saying plainly rather than
 * failing the whole run or pretending it worked.
 */
function _organiseFiles() {
  var folderId = _props().getProperty('PROJECTS_FOLDER_ID') || '';
  if (!folderId) throw new Error('PROJECTS_FOLDER_ID is not set');
  var folder = DriveApp.getFolderById(folderId);

  var targets = [{ label: 'control sheet', id: _controlId() }];
  _projects().forEach(function (p) {
    if (p.spreadsheet_id) {
      targets.push({ label: "project '" + p.id + "'", id: p.spreadsheet_id });
    }
  });

  var results = [];
  targets.forEach(function (target) {
    if (!target.id) return;
    try {
      var outcome = _moveIntoFolder(target.id, folder);
      if (outcome.already) {
        results.push(target.label + ': already there');
      } else if (outcome.stuck) {
        results.push(target.label + ': added to the folder, but it stays in its ' +
                     "owner's Drive too — it is not yours to move");
      } else {
        results.push(target.label + ': moved');
      }
    } catch (err) {
      results.push(target.label + ': could not be moved — ' + err);
    }
  });
  return { folder: folder.getName(), results: results };
}

/**
 * Tabs a copy starts empty.
 *
 * A copy is for reusing a set-up, not for inheriting somebody else's results —
 * a new project arriving with several thousand scraped jobs in it would be
 * wrong, and worse, they would look like its own findings. Which tabs those are
 * is read from the copy's own Settings, because a project can point its jobs
 * and enrichment anywhere; the template names are a fallback for a project
 * whose Settings tab has not been filled in yet.
 */
var RESULT_SETTINGS = ['google_sheets.jobs_worksheet',
                       'google_sheets.enrichment_output_worksheet'];
var RESULT_TABS_FALLBACK = ['Jobs', 'Companies'];

/** The tab names this spreadsheet treats as automation output. */
function _resultTabs(spreadsheet) {
  var names = RESULT_TABS_FALLBACK.slice();
  var settings = spreadsheet.getSheetByName(SHEET_NAME);
  if (!settings) return names;
  var values = settings.getDataRange().getValues();
  if (!values.length) return names;
  var header = values[0].map(function (h) { return String(h).trim(); });
  var keyAt = header.indexOf(KEY_COLUMN);
  var valueAt = header.indexOf(VALUE_COLUMN);
  if (keyAt < 0 || valueAt < 0) return names;
  for (var r = 1; r < values.length; r++) {
    if (RESULT_SETTINGS.indexOf(String(values[r][keyAt]).trim()) === -1) continue;
    var tab = String(values[r][valueAt]).trim();
    if (tab && names.indexOf(tab) === -1) names.push(tab);
  }
  return names;
}

/** Empty a tab's data, keeping its header row. */
function _clearRows(sheet) {
  var last = sheet.getLastRow();
  if (last > 1) sheet.deleteRows(2, last - 1);
}

/**
 * Copy an existing project into a new one.
 *
 * Drive copies the whole spreadsheet in a single call — every tab, its
 * formatting and its column layout — which is far more faithful than
 * reconstructing tabs from a template. The scraped results are then emptied,
 * so what carries over is the set-up: Settings, Keywords, and the
 * hand-maintained Company list, which is usually the expensive part to rebuild.
 *
 * The copy is a project in its own right: its own id, its own password, its own
 * data key. Nothing links it back to the source, so changing one never affects
 * the other.
 */
function _copyProject(source, body) {
  var name = String(body.name || '').trim();
  if (!name) throw new Error('a project name is required');
  var password = String(body.password || '');
  if (password.length < MIN_PASSWORD_LENGTH) {
    throw new Error('the password must be at least ' + MIN_PASSWORD_LENGTH +
                    ' characters');
  }
  if (_projectByPassword(password)) {
    throw new Error('another project already uses that password');
  }
  if (!source.spreadsheet_id) {
    throw new Error("project '" + source.id + "' has no spreadsheet to copy");
  }

  var projectId = _uniqueId(_slugify(body.id || name));

  // Copy straight into the projects folder when one is configured, so the file
  // is never briefly loose in My Drive.
  var folderId = _props().getProperty('PROJECTS_FOLDER_ID') || '';
  var copy;
  var filedIn = '';
  var sourceFile = DriveApp.getFileById(source.spreadsheet_id);
  if (folderId) {
    try {
      var folder = DriveApp.getFolderById(folderId);
      copy = sourceFile.makeCopy('Automation — ' + name, folder);
      filedIn = folder.getName();
    } catch (err) {
      copy = sourceFile.makeCopy('Automation — ' + name);
      filedIn = 'could not file it into PROJECTS_FOLDER_ID: ' + err;
    }
  } else {
    copy = sourceFile.makeCopy('Automation — ' + name);
  }

  var spreadsheetId = copy.getId();
  var spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  _ensureTemplateTabs(spreadsheet);

  var cleared = [];
  if (!body.includeResults) {
    _resultTabs(spreadsheet).forEach(function (tabName) {
      var sheet = spreadsheet.getSheetByName(tabName);
      if (!sheet) return;
      var rows = Math.max(0, sheet.getLastRow() - 1);
      if (rows) {
        _clearRows(sheet);
        cleared.push(tabName + ' (' + rows + ' rows)');
      }
    });
  }

  var grantedTo = '';
  var ownerEmail = String(body.ownerEmail || '').trim();
  if (ownerEmail) {
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(ownerEmail)) {
      throw new Error('that does not look like an email address: ' + ownerEmail);
    }
    try {
      copy.addEditor(ownerEmail);
      grantedTo = ownerEmail;
    } catch (err) {
      grantedTo = 'could not share it with ' + ownerEmail + ': ' + err;
    }
  }

  // A copy inherits the source's sharing, which would hand the new project to
  // whoever could read the old one. Drop everyone the copy did not earn.
  var keep = {};
  if (_serviceAccount()) keep[_serviceAccount()] = true;
  // grantedTo holds an error string when the share failed, so compare against
  // the address that was actually asked for.
  if (grantedTo && grantedTo === ownerEmail) keep[ownerEmail] = true;
  copy.getEditors().forEach(function (editor) {
    var email = editor.getEmail();
    if (!keep[email]) {
      try { copy.removeEditor(email); } catch (err) { /* the owner cannot be removed */ }
    }
  });
  copy.getViewers().forEach(function (viewer) {
    try { copy.removeViewer(viewer.getEmail()); } catch (err) { /* ditto */ }
  });

  var shared = '';
  var serviceAccount = _serviceAccount();
  if (serviceAccount) {
    try {
      copy.addEditor(serviceAccount);
      shared = serviceAccount;
    } catch (err) {
      throw new Error('the copy was made but sharing it with ' + serviceAccount +
        ' failed, so the automation cannot reach it: ' + err);
    }
  }

  var salt = _randomKey().substring(0, 32);
  var record = {
    id: projectId,
    name: _plainText(name),
    spreadsheet_id: spreadsheetId,
    status: 'active',
    data_key: _randomKey(),          // its own; the source's must not be reused
    pw_salt: salt,
    pw_hash: _hashPassword(password, salt),
    created_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
    notes: _plainText(String(body.notes || ('copied from ' + source.id)))
  };

  var control = _controlSheet();
  var header = control.getRange(1, 1, 1, Math.max(control.getLastColumn(), 1))
               .getValues()[0].map(function (h) { return String(h).trim(); });
  control.appendRow(header.map(function (col) {
    return record.hasOwnProperty(col) ? record[col] : '';
  }));

  return {
    project: projectId, name: name, spreadsheetId: spreadsheetId,
    url: copy.getUrl(), copiedFrom: source.id, filedIn: filedIn,
    grantedTo: grantedTo, sharedWith: shared,
    cleared: cleared, keptResults: Boolean(body.includeResults)
  };
}

/**
 * Remove a project from the registry.
 *
 * Deliberately not destructive by default. Deleting the row is what makes the
 * project unreachable — no password opens it, no run finds it — but the
 * spreadsheet is left alone, because it holds work that took real time to
 * gather and is very often somebody's only copy. Pass trashSheet to move it to
 * Drive's bin as well, which is still recoverable for thirty days. Nothing here
 * deletes anything permanently.
 *
 * Three things are required together, and each rules out a different accident:
 *
 *   confirm: true      no request arrives here by mistake
 *   confirmName        the project's own name, typed out — proves the caller
 *                      knows WHICH project they are deleting, which a session
 *                      alone does not
 *   password           the current password, even with a valid token, so a
 *                      borrowed session cannot destroy someone's project
 */
function _deleteProject(project, body) {
  if (body.confirm !== true) {
    throw new Error('deleting a project needs confirm:true');
  }
  if (!_passwordMatches(project, body.password || '')) {
    throw new Error("the project's current password is required to delete it");
  }
  var typed = String(body.confirmName || '').trim();
  var actual = String(project.name || '').trim();
  if (typed.toLowerCase() !== actual.toLowerCase()) {
    throw new Error('type the project name exactly to confirm: "' + actual + '"');
  }

  var trashed = false;
  var trashError = '';
  if (body.trashSheet === true && project.spreadsheet_id) {
    try {
      // Bin, never a hard delete: recoverable for thirty days.
      DriveApp.getFileById(project.spreadsheet_id).setTrashed(true);
      trashed = true;
    } catch (err) {
      // The row still goes; a sheet that could not be binned is not a reason to
      // leave the project reachable.
      trashError = String(err);
    }
  }

  // Read the row number fresh. The one on `project` came from a read that may
  // now be stale, and deleting by a stale index would remove the wrong project.
  var control = _controlSheet();
  var values = control.getDataRange().getValues();
  var header = values[0].map(function (h) { return String(h).trim(); });
  var idAt = header.indexOf('id');
  if (idAt < 0) throw new Error("the control sheet has no 'id' column");

  var rowNumber = 0;
  for (var r = 1; r < values.length; r++) {
    if (String(values[r][idAt]).trim().toLowerCase() === String(project.id).toLowerCase()) {
      rowNumber = r + 1;
      break;
    }
  }
  if (!rowNumber) throw new Error('that project is no longer in the control sheet');

  control.deleteRow(rowNumber);
  _revokeProjectTokens(project.id);

  return {
    deleted: project.id, name: actual,
    spreadsheetId: project.spreadsheet_id,
    sheetTrashed: trashed,
    // The sheet outlives the project unless asked otherwise, so say where it is.
    sheetNote: trashed
      ? "the spreadsheet is in Drive's bin and recoverable for 30 days"
      : (trashError
          ? 'the project is gone, but the spreadsheet could not be binned: ' + trashError
          : 'the spreadsheet was left untouched in Drive')
  };
}

function _createProject(body) {
  var name = String(body.name || '').trim();
  if (!name) throw new Error('a project name is required');
  var password = String(body.password || '');
  if (password.length < MIN_PASSWORD_LENGTH) {
    throw new Error('the password must be at least ' + MIN_PASSWORD_LENGTH +
                    ' characters');
  }
  if (_projectByPassword(password)) {
    // The password is what selects the project, so a duplicate would make one
    // of the two permanently unreachable.
    throw new Error('another project already uses that password');
  }

  var projectId = _uniqueId(_slugify(body.id || name));
  var spreadsheetId = String(body.spreadsheetId || '').trim();
  var created = false;
  var url = '';
  var filedIn = '';

  if (spreadsheetId) {
    // ADOPTION IS PRIVILEGED, creation is not.
    //
    // Creating a sheet makes an empty one nobody else has anything in.
    // Adopting names a sheet that already exists, and this script can open
    // ANY sheet its owner can — so without this check a tenant holding one
    // project's password could point a new project of their own at another
    // project's spreadsheet, give it a password they choose, and read and
    // write it. That is exactly the isolation the whole design rests on.
    if (!_adminPassword()) {
      throw new Error('adopting an existing spreadsheet requires ADMIN_PASSWORD ' +
        'to be set in Script Properties. Without it, only new sheets may be ' +
        'created.');
    }
    if (!_constantTimeEquals(_adminPassword(), String(body.adminPassword || ''))) {
      throw new Error('the admin password is not correct');
    }
    if (spreadsheetId === _controlId()) {
      throw new Error('that is the control spreadsheet, not a project');
    }
    var alreadyUsed = _projects().filter(function (p) {
      return String(p.spreadsheet_id).trim() === spreadsheetId;
    });
    if (alreadyUsed.length) {
      throw new Error('another project already uses that spreadsheet');
    }

    // Open it before a registry row promises that it can be opened.
    var existing = SpreadsheetApp.openById(spreadsheetId);
    url = existing.getUrl();
    _ensureTemplateTabs(existing);
  } else {
    var fresh = SpreadsheetApp.create('Automation — ' + name);
    spreadsheetId = fresh.getId();
    url = fresh.getUrl();
    created = true;
    _ensureTemplateTabs(fresh);

    filedIn = _fileIntoProjectsFolder(spreadsheetId);
  }

  // Whoever created the project gets a direct grant on its sheet.
  //
  // Named per-file rather than relying on the projects folder, so the sheet
  // lands in their "Shared with me" and the response can hand back a link they
  // can actually open — a folder grant alone leaves them hunting for it.
  var grantedTo = '';
  var ownerEmail = String(body.ownerEmail || '').trim();
  if (ownerEmail) {
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(ownerEmail)) {
      throw new Error('that does not look like an email address: ' + ownerEmail);
    }
    try {
      DriveApp.getFileById(spreadsheetId).addEditor(ownerEmail);
      grantedTo = ownerEmail;
    } catch (err) {
      // The project is usable without this; say so rather than failing it.
      grantedTo = 'could not share it with ' + ownerEmail + ': ' + err;
    }
  }

  // The pipeline reads and writes this sheet as the service account, so it
  // needs editor rights. Doing it here is the whole reason provisioning lives
  // in this script: a service account cannot create the file itself.
  var shared = '';
  var serviceAccount = _serviceAccount();
  if (serviceAccount) {
    try {
      DriveApp.getFileById(spreadsheetId).addEditor(serviceAccount);
      shared = serviceAccount;
    } catch (err) {
      throw new Error('the sheet was created but sharing it with ' +
        serviceAccount + ' failed, so the automation cannot reach it: ' + err);
    }
  }

  var salt = _randomKey().substring(0, 32);
  var record = {
    id: projectId,
    name: _plainText(name),
    spreadsheet_id: spreadsheetId,
    status: 'active',
    data_key: _randomKey(),
    pw_salt: salt,
    pw_hash: _hashPassword(password, salt),
    created_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
    notes: _plainText(String(body.notes || ''))
  };

  var control = _controlSheet();
  var header = control.getRange(1, 1, 1, Math.max(control.getLastColumn(), 1))
               .getValues()[0].map(function (h) { return String(h).trim(); });
  if (!header.length || !header[0]) {
    control.getRange(1, 1, 1, CONTROL_HEADER.length).setValues([CONTROL_HEADER]);
    control.setFrozenRows(1);
    header = CONTROL_HEADER;
  }
  control.appendRow(header.map(function (col) {
    return record.hasOwnProperty(col) ? record[col] : '';
  }));

  return {
    project: projectId, name: name, spreadsheetId: spreadsheetId,
    url: url, createdSheet: created, sharedWith: shared,
    grantedTo: grantedTo,
    // '' when no folder is configured, the folder's name when it was filed,
    // and a 'could not file…' string when it was not — so a misconfigured
    // folder is visible rather than silently leaving sheets in My Drive.
    filedIn: filedIn
  };
}

/**
 * Who may create a project.
 *
 * With ADMIN_PASSWORD set, only that password may; without it, any signed-in
 * session may, which is the right default while you are the only operator.
 * Set it once you hand a project password to someone else.
 */
function _authoriseAdmin(body) {
  var admin = _adminPassword();
  if (admin) {
    if (!_constantTimeEquals(admin, String(body.adminPassword || ''))) {
      return { ok: false, error: 'the admin password is not correct' };
    }
    return { ok: true };
  }
  if (!_authorise(body)) {
    return { ok: false, error: _authError(body), signedOut: true };
  }
  return { ok: true };
}


/* ── Reads (JSONP — CORS never applies to a <script> tag) ───────────────── */

function doGet(e) {
  var params = (e && e.parameter) || {};
  var payload;
  try {
    var action = params.action || (params.ping ? 'ping' : 'settings');

    if (action === 'ping') {
      // No password, and no sheet contents — just enough to verify a fresh
      // deployment in a browser before anything is wired to it.
      var controlId = _controlId();
      var count = 0;
      var controlError = '';
      try {
        count = _activeProjects().length;
      } catch (err) {
        controlError = String(err);
      }
      payload = {
        ok: !controlError && Boolean(controlId),
        version: 4,
        standalone: true,
        controlSheetConfigured: Boolean(controlId),
        controlSheetReadable: Boolean(controlId) && !controlError,
        // A count, never the names: the landing page must not disclose which
        // projects exist to someone who has no password.
        activeProjects: count,
        serviceAccountConfigured: Boolean(_serviceAccount()),
        adminPasswordConfigured: Boolean(_adminPassword()),
        projectsFolder: _projectsFolderStatus(),
        // Diagnostic only, and it needs the userinfo.email scope, which is not
        // granted by default — so it must never be allowed to fail the ping.
        runsAs: _effectiveUser(),
        error: controlError || undefined
      };

    } else if (action === 'auth') {
      // Sign in: the password selects the project, then the data key and a
      // token bound to that project are handed over.
      var project = _projectByPassword(params.password || '');
      if (!project) {
        payload = { ok: false, error: _authError(params) };
      } else if (!project.data_key) {
        payload = { ok: false, error:
          "project '" + project.id + "' has no data_key in the control sheet, " +
          'so its published data cannot be decrypted.' };
      } else {
        payload = { ok: true, project: project.id, name: project.name,
                    dataKey: project.data_key, token: _issueToken(project.id),
                    ttlMs: TOKEN_TTL_MS };
      }

    } else {
      var authed = _authorise(params);
      if (!authed) {
        payload = { ok: false, error: _authError(params), signedOut: true };
      } else if (params.project &&
                 String(params.project).toLowerCase() !== String(authed.id).toLowerCase()) {
        // A session is good for its own project only.
        payload = { ok: false, error: 'that session is not valid for project ' +
                    params.project, signedOut: true };
      } else if (action === 'keywords') {
        payload = { ok: true, project: authed.id, keywords: _readKeywords(authed),
                    readAt: new Date().toISOString() };
      } else if (action === 'tabs') {
        payload = _projectTabs(authed);
        payload.ok = true;
        payload.project = authed.id;
      } else if (action === 'runs') {
        payload = _runsView(authed, params);
        payload.ok = true;
        payload.project = authed.id;
      } else if (action === 'rows') {
        // Any one tab of this project's own spreadsheet. What the validator,
        // the enricher and the career-page pass all need and could not get.
        payload = { ok: true, project: authed.id,
                    worksheet: params.worksheet || '',
                    rows: _tabRows(authed, params.worksheet) };
      } else if (action === 'inputs') {
        // Everything the pipeline needs to read, so a machine running it holds
        // no Google credentials at all.
        payload = _pipelineInputs(authed);
        payload.ok = true;
      } else if (action === 'project') {
        payload = { ok: true, project: authed.id, name: authed.name,
                    spreadsheetId: authed.spreadsheet_id,
                    createdAt: authed.created_at, notes: authed.notes };
      } else if (action === 'settings') {
        payload = { ok: true, project: authed.id, settings: _readAll(authed),
                    readAt: new Date().toISOString() };
      } else {
        // Never fall through to a settings read. A caller asking for something
        // this deployment does not have would otherwise be handed a perfectly
        // valid answer to a different question — an older version answered
        // action=rows with the Settings tab, so a validation run saw an empty
        // jobs tab, changed nothing, and reported success.
        payload = { ok: false, unknownAction: action, error:
          "this deployment does not know the action '" + action + "'. It is " +
          'probably older than the code asking — re-paste apps-script/' +
          'Settings.gs and deploy a new version.' };
      }
    }
  } catch (err) {
    payload = { ok: false, error: String(err) };
  }

  // JSONP only for whoever asked for it.
  //
  // The browser dashboard must have it: /exec sends no CORS header, so a page
  // can only read this by loading it as a <script>. A program has no such
  // problem and wants plain JSON — and wrapping it regardless meant the
  // pipeline's own reader fed `x({...});` to a JSON parser and died on the
  // first character. So the wrapper follows the request for one.
  var callback = params.callback || '';
  if (!callback) {
    return ContentService
      .createTextOutput(JSON.stringify(payload))
      .setMimeType(ContentService.MimeType.JSON);
  }
  // Only a bare identifier may be interpolated into the response.
  var safe = /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(callback) ? callback : 'callback';
  return ContentService
    .createTextOutput(safe + '(' + JSON.stringify(payload) + ');')
    .setMimeType(ContentService.MimeType.JAVASCRIPT);
}


/* ── Writes (sent no-cors; the caller confirms with a follow-up doGet) ──── */

function doPost(e) {
  var result;
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');

    if (body.action === 'organiseFiles') {
      // Admin-gated: this rearranges the owner's Drive, not a project's data.
      var mayOrganise = _authoriseAdmin(body);
      return _json(mayOrganise.ok
        ? (function () { var r = _organiseFiles(); r.ok = true; return r; })()
        : mayOrganise);
    }

    if (body.action === 'deleteProject') {
      // Authorised by the project itself: you can only delete what you can
      // open. The password is demanded again inside, token or not.
      var doomed = _authorise(body);
      if (!doomed) {
        return _json({ ok: false, error: _authError(body), signedOut: true });
      }
      var deleteLock = LockService.getScriptLock();
      deleteLock.waitLock(30000);
      try {
        var removed = _deleteProject(doomed, body);
        removed.ok = true;
        return _json(removed);
      } finally {
        deleteLock.releaseLock();
      }
    }

    if (body.action === 'copyProject') {
      // Authorised by the project being copied, not by an admin: if you can
      // open it you can already read everything the copy would contain, so
      // copying grants you nothing you did not have.
      var sourceProject = _authorise(body);
      if (!sourceProject) {
        return _json({ ok: false, error: _authError(body), signedOut: true });
      }
      var copyLock = LockService.getScriptLock();
      copyLock.waitLock(30000);
      try {
        var copied = _copyProject(sourceProject, body);
        copied.ok = true;
        return _json(copied);
      } finally {
        copyLock.releaseLock();
      }
    }

    if (body.action === 'createProject') {
      var allowed = _authoriseAdmin(body);
      if (!allowed.ok) {
        result = allowed;
      } else {
        var createLock = LockService.getScriptLock();
        createLock.waitLock(30000);
        try {
          result = _createProject(body);
          result.ok = true;
        } finally {
          createLock.releaseLock();
        }
      }
      return _json(result);
    }

    var project = _authorise(body);
    if (!project) {
      result = { ok: false, error: _authError(body), signedOut: true };

    } else if (body.action === 'changePassword') {
      var next = String(body.newPassword || '');
      if (next.length < MIN_PASSWORD_LENGTH) {
        result = { ok: false,
                   error: 'the new password must be at least ' +
                          MIN_PASSWORD_LENGTH + ' characters' };
      } else if (!_passwordMatches(project, body.currentPassword || '')) {
        // Changing the password always requires the current one, even when the
        // caller already holds a valid token — otherwise a borrowed session
        // could lock its owner out.
        result = { ok: false, error: 'the current password is not correct' };
      } else if (_projectByPassword(next)) {
        result = { ok: false, error: 'another project already uses that password' };
      } else {
        var salt = _randomKey().substring(0, 32);
        var control = _controlSheet();
        var header = control.getRange(1, 1, 1, control.getLastColumn()).getValues()[0]
                     .map(function (h) { return String(h).trim(); });
        control.getRange(project._row, header.indexOf('pw_salt') + 1).setValue(salt);
        control.getRange(project._row, header.indexOf('pw_hash') + 1)
               .setValue(_hashPassword(next, salt));
        _revokeProjectTokens(project.id);   // every existing session is now stale
        result = { ok: true, changed: true, sessionsRevoked: true,
                   project: project.id };
      }

    } else {
      // One writer at a time: two dashboards saving at once would interleave
      // cell writes and leave a mix of both.
      var writeLock = LockService.getScriptLock();
      writeLock.waitLock(20000);
      try {
        if (body.action === 'appendJobs') {
          result = _appendJobs(project, body);
        } else if (body.action === 'ensureColumn') {
          result = _ensureColumn(project, body);
        } else if (body.action === 'writeColumn') {
          result = _writeColumn(project, body);
        } else if (body.action === 'deleteRows') {
          result = _deleteRows(project, body);
        } else if (body.action === 'replaceTab') {
          result = _replaceTab(project, body);
        } else if (body.action === 'requestRun') {
          result = _requestRun(project, body);
        } else if (body.action === 'claimRun') {
          result = _claimRun(project, body);
        } else if (body.action === 'updateRun') {
          result = _updateRun(project, body);
        } else if (body.action === 'cancelRun') {
          result = _cancelRun(project, body);
        } else if (body.action === 'saveKeywords') {
          result = _writeKeywords(project, body.keywords || []);
        } else if (!body.action || body.action === 'saveSettings') {
          result = _applyUpdates(project, body.updates || {});
        } else {
          // The dangerous one. An unrecognised write used to land here with no
          // updates in it, so it changed nothing and answered ok — a column of
          // validation statuses would be discarded and reported as written.
          throw new Error("this deployment does not know the action '" +
            body.action + "'. It is probably older than the code asking — " +
            're-paste apps-script/Settings.gs and deploy a new version.');
        }
        result.ok = true;
        result.project = project.id;
      } finally {
        writeLock.releaseLock();
      }
    }
  } catch (err) {
    result = { ok: false, error: String(err) };
  }
  return _json(result);
}

function _json(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
