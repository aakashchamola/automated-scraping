/**
 * Settings.gs — the published dashboard's server side.
 *
 * The dashboard is a static page on GitHub Pages with no credentials, so it
 * cannot talk to the Sheets API. This Web App is the missing half: it runs as
 * the sheet's owner, checks the login password, and reads and writes the
 * Settings tab.
 *
 * TWO SECRETS, kept deliberately separate — this is the whole reason the
 * password can be changed from the dashboard:
 *
 *   DASHBOARD_PASSWORD   what people type to sign in. Lives here, in Script
 *                        Properties, and can be changed from the dashboard,
 *                        because nothing is encrypted with it.
 *   DASHBOARD_DATA_KEY   the key the published data files were encrypted with
 *                        at publish time. Handed to the page only after the
 *                        password checks out. It must never change — every
 *                        already-published file was encrypted with it.
 *
 * Making the password itself the encryption key (the earlier design) is what
 * made it unchangeable: altering it meant re-encrypting and republishing every
 * file, which a browser cannot do.
 *
 * TWO CORS RULES, both learned the hard way on an earlier project:
 *
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
 * It never needs editing again. It reads and writes whatever rows the Settings
 * tab happens to contain, keyed by the Setting column, so adding settings
 * later is a change to that tab — never to this file.
 *
 *   1. Extensions -> Apps Script from the spreadsheet (a bound script).
 *   2. Paste this file in, replacing everything.
 *   3. Project Settings -> Script Properties, add two rows:
 *        DASHBOARD_PASSWORD   the login password
 *        DASHBOARD_DATA_KEY   the value of the DASHBOARD_DATA_KEY repo secret
 *   4. Deploy -> New deployment -> Web app
 *        Execute as:      Me
 *        Who has access:  Anyone
 *   5. Check it: open <the /exec URL>?ping=1 in a browser. Everything it
 *      reports should be true.
 *   6. gh secret set SETTINGS_WEB_APP_URL   (paste the /exec URL)
 */

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

function _props() {
  return PropertiesService.getScriptProperties();
}

function _password() {
  return _props().getProperty('DASHBOARD_PASSWORD') || '';
}

function _dataKey() {
  return _props().getProperty('DASHBOARD_DATA_KEY') || '';
}

/** Why a request was refused, so a setup mistake reads as a setup mistake
 *  rather than as "wrong password" forever. */
function _authError(given) {
  if (!_password()) {
    return 'DASHBOARD_PASSWORD is not set. Project Settings -> Script Properties -> ' +
           'add DASHBOARD_PASSWORD.';
  }
  if (!given) return 'no password sent';
  return 'wrong password';
}

/** Constant-time-ish comparison so a wrong password cannot be found by timing. */
function _passwordMatches(given) {
  var expected = _password();
  if (!expected || !given || given.length !== expected.length) return false;
  var diff = 0;
  for (var i = 0; i < expected.length; i++) {
    diff |= expected.charCodeAt(i) ^ given.charCodeAt(i);
  }
  return diff === 0;
}

/* ── Sessions ───────────────────────────────────────────────────────────────
   A token is issued once the password checks out, and the page remembers that
   instead of the password. So staying signed in never means keeping what
   someone typed, and changing the password can revoke every session at once. */

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

function _issueToken() {
  var map = _tokens();
  var now = Date.now();
  Object.keys(map).forEach(function (t) { if (map[t] < now) delete map[t]; });
  var token = Utilities.getUuid().replace(/-/g, '') +
              Utilities.getUuid().replace(/-/g, '');
  map[token] = now + TOKEN_TTL_MS;
  _saveTokens(map);
  return token;
}

function _tokenValid(token) {
  if (!token) return false;
  var map = _tokens();
  var expiry = map[token];
  if (!expiry) return false;
  if (expiry < Date.now()) {
    delete map[token];
    _saveTokens(map);
    return false;
  }
  return true;
}

/** A caller is authorised by a live token or by the password itself. */
function _authorised(params) {
  return _tokenValid(params.token || '') || _passwordMatches(params.password || '');
}

/* ── Sheet ─────────────────────────────────────────────────────────────── */

function _sheet() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) throw new Error("no '" + SHEET_NAME + "' tab in this spreadsheet");
  return sheet;
}

function _readAll() {
  var values = _sheet().getDataRange().getValues();
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
function _applyUpdates(updates) {
  var sheet = _sheet();
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

function _keywordSheet() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(KEYWORDS_SHEET);
  if (!sheet) throw new Error("no '" + KEYWORDS_SHEET + "' tab in this spreadsheet");
  return sheet;
}

function _readKeywords() {
  var sheet = _keywordSheet();
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
function _writeKeywords(list) {
  var clean = [];
  (list || []).forEach(function (raw) {
    var term = String(raw || '').trim();
    // Duplicates would scrape the same search twice for no benefit.
    if (term && clean.indexOf(term) === -1) clean.push(term);
  });
  if (!clean.length) throw new Error('refusing to leave the Keywords tab empty');

  var sheet = _keywordSheet();
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
  if (needed > previous) {
    var missing = needed - previous;
    if (sheet.getMaxRows() < needed + 1) sheet.insertRowsAfter(sheet.getMaxRows(), missing);
  }
  sheet.getRange(2, at + 1, needed, 1).setValues(column);
  return { count: clean.length, cleared: Math.max(0, previous - clean.length) };
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
      payload = {
        ok: true,
        sheet: SHEET_NAME,
        sheetFound: Boolean(
          SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME)),
        passwordConfigured: Boolean(_password()),
        dataKeyConfigured: Boolean(_dataKey()),
        keywordsFound: Boolean(
          SpreadsheetApp.getActiveSpreadsheet().getSheetByName(KEYWORDS_SHEET)),
        version: 3
      };
    } else if (action === 'auth') {
      // Sign in: check the password, then hand over the data key and a token.
      if (!_passwordMatches(params.password || '')) {
        payload = { ok: false, error: _authError(params.password || '') };
      } else if (!_dataKey()) {
        payload = { ok: false, error:
          'DASHBOARD_DATA_KEY is not set in Script Properties, so the data ' +
          'cannot be decrypted.' };
      } else {
        payload = { ok: true, dataKey: _dataKey(), token: _issueToken(),
                    ttlMs: TOKEN_TTL_MS };
      }
    } else if (!_authorised(params)) {
      payload = { ok: false, error: _authError(params.password || ''),
                  signedOut: true };
    } else if (action === 'keywords') {
      payload = { ok: true, keywords: _readKeywords(),
                  readAt: new Date().toISOString() };
    } else {
      payload = { ok: true, settings: _readAll(),
                  readAt: new Date().toISOString() };
    }
  } catch (err) {
    payload = { ok: false, error: String(err) };
  }

  // Only a bare identifier may be interpolated into the response.
  var callback = params.callback || 'callback';
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

    if (!_authorised(body)) {
      result = { ok: false, error: _authError(body.password || ''),
                 signedOut: true };
    } else if (body.action === 'changePassword') {
      var next = String(body.newPassword || '');
      if (next.length < MIN_PASSWORD_LENGTH) {
        result = { ok: false,
                   error: 'the new password must be at least ' +
                          MIN_PASSWORD_LENGTH + ' characters' };
      } else if (!_passwordMatches(body.currentPassword || '')) {
        // Changing the password always requires the current one, even when the
        // caller already holds a valid token — otherwise a borrowed session
        // could lock its owner out.
        result = { ok: false, error: 'the current password is not correct' };
      } else {
        _props().setProperty('DASHBOARD_PASSWORD', next);
        _saveTokens({});          // every existing session is now stale
        result = { ok: true, changed: true, sessionsRevoked: true };
      }
    } else {
      // One writer at a time: two dashboards saving at once would interleave
      // cell writes and leave a mix of both.
      var lock = LockService.getScriptLock();
      lock.waitLock(20000);
      try {
        if (body.action === 'saveKeywords') {
          result = _writeKeywords(body.keywords || []);
        } else {
          result = _applyUpdates(body.updates || {});
        }
        result.ok = true;
      } finally {
        lock.releaseLock();
      }
    }
  } catch (err) {
    result = { ok: false, error: String(err) };
  }
  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}
