/**
 * Settings.gs — lets the published dashboard save settings back to this sheet.
 *
 * The dashboard is a static page on GitHub Pages with no credentials, so it
 * cannot talk to the Sheets API. This Web App is the missing half: it runs as
 * the sheet's owner, checks a shared password, and writes the Settings tab.
 *
 * TWO RULES, both learned the hard way on an earlier project:
 *
 *   1. A Web App's /exec response carries NO Access-Control-Allow-Origin, so a
 *      cors-mode fetch that READS the response fails with ERR_FAILED. Writes
 *      must therefore be sent no-cors (fire-and-forget, response unreadable).
 *   2. Because the write's result cannot be read, the caller confirms by
 *      reading back — and that read must be JSONP (a <script> tag), which is
 *      not subject to CORS at all.
 *
 * So: doPost writes and says nothing useful; doGet serves JSONP for reading
 * and confirming.
 *
 * Set up ONCE. It never needs editing again: it reads and writes whatever rows
 * the Settings tab happens to contain, keyed by the Setting column, so adding
 * or removing settings later is a change to that tab and to the pipeline's
 * schema — never to this file.
 *
 *   1. Extensions -> Apps Script from the spreadsheet (a bound script).
 *   2. Paste this file in, replacing everything.
 *   3. Project Settings -> Script Properties -> add DASHBOARD_PASSWORD,
 *      matching the repository secret of the same name.
 *   4. Deploy -> New deployment -> Web app
 *        Execute as:      Me
 *        Who has access:  Anyone
 *   5. Check it: open <the /exec URL>?ping=1 in a browser. It should report
 *      sheetFound and passwordConfigured both true.
 *   6. gh secret set SETTINGS_WEB_APP_URL   (paste the /exec URL)
 */

var SHEET_NAME = 'Settings';
var KEY_COLUMN = 'Setting';
var VALUE_COLUMN = 'Value';

function _password() {
  return PropertiesService.getScriptProperties().getProperty('DASHBOARD_PASSWORD') || '';
}

/** Why a request was refused, so a setup mistake reads as a setup mistake
 *  rather than as "wrong password" forever. */
function _authError(given) {
  if (!_password()) {
    return 'DASHBOARD_PASSWORD is not set. Project Settings -> Script Properties -> ' +
           'add DASHBOARD_PASSWORD with the same value as the repository secret.';
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
 * Apply {path: value} to the Value column. Only rows that already exist are
 * touched: the tab is generated from the code's schema, so an unknown key is a
 * stale client rather than a new setting, and silently appending it would
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

/** JSONP read. Called via a <script> tag, so CORS never applies. */
function doGet(e) {
  var params = (e && e.parameter) || {};
  var callback = params.callback || 'callback';
  var payload;
  try {
    // Health check — no password, and no sheet contents. Lets the deployment
    // be verified in a browser before anything is wired up to it.
    if (params.ping) {
      payload = {
        ok: true,
        sheet: SHEET_NAME,
        sheetFound: Boolean(SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME)),
        passwordConfigured: Boolean(_password()),
        version: 1
      };
    } else if (!_passwordMatches(params.password || '')) {
      payload = { ok: false, error: _authError(params.password || '') };
    } else {
      payload = { ok: true, settings: _readAll(), readAt: new Date().toISOString() };
    }
  } catch (err) {
    payload = { ok: false, error: String(err) };
  }
  // Only a bare identifier may be interpolated into the response.
  var safe = /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(callback) ? callback : 'callback';
  return ContentService
    .createTextOutput(safe + '(' + JSON.stringify(payload) + ');')
    .setMimeType(ContentService.MimeType.JAVASCRIPT);
}

/** Write. Sent no-cors, so the caller cannot read this response — it confirms
 *  with a follow-up doGet instead. */
function doPost(e) {
  var result;
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (!_passwordMatches(body.password || '')) {
      result = { ok: false, error: _authError(body.password || '') };
    } else {
      // One writer at a time: two dashboards saving at once would interleave
      // cell writes and leave a mix of both.
      var lock = LockService.getScriptLock();
      lock.waitLock(20000);
      try {
        result = _applyUpdates(body.updates || {});
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
