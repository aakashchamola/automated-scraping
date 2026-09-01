/**
 * test_live.js — drive the PUBLISHED dashboard, against the real deployment.
 *
 *     DASHBOARD_PASSWORD='…' node site/test_live.js
 *     DASHBOARD_URL='https://…' DASHBOARD_PASSWORD='…' node site/test_live.js
 *
 * test_browser.js proves the page works against a stub. This proves the parts
 * a stub cannot: that GitHub Pages is serving the build you think it is, that
 * the Apps Script deployment behind SETTINGS_WEB_APP_URL is the current one,
 * and that the account it runs as can actually open the project's spreadsheet.
 *
 * Every failure this has caught was in that seam rather than in any one piece:
 * a stale /exec URL in the repository secret, a deployment still serving an
 * older version, and a script running as an account with no access to the
 * sheet — each of which passes every offline test.
 *
 * The password comes from the environment. This repository is public.
 */
const PW = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const URL_ = process.env.DASHBOARD_URL ||
             'https://aakashchamola.github.io/automated-scraping/';
const PASSWORD = process.env.DASHBOARD_PASSWORD || '';

let pass = 0, fail = 0;
const check = (label, ok, detail) => ok
  ? (pass++, console.log('  ok   ' + label))
  : (fail++, console.log('  FAIL ' + label + (detail ? '\n       ' + detail : '')));

(async () => {
  if (!PASSWORD) {
    console.error('set DASHBOARD_PASSWORD to the password of a live project');
    process.exit(2);
  }
  const browser = await PW.chromium.launch();
  const page = await (await browser.newContext()).newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  try {
    console.log(`\nLIVE  ${URL_}\n`);
    await page.goto(URL_, { waitUntil: 'networkidle', timeout: 60000 });
    await page.waitForSelector('#gate:not([hidden])', { timeout: 30000 });
    check('the published page loads behind its gate', await page.isHidden('#app'));

    await page.fill('#gate-pw', 'definitely-not-the-password');
    await page.click('#gate-go');
    await page.waitForFunction(
      () => document.getElementById('gate-err').textContent.trim(),
      null, { timeout: 60000 });
    check('a wrong password is refused by the live service',
          /no project matched/i.test(await page.textContent('#gate-err')),
          await page.textContent('#gate-err'));

    await page.fill('#gate-pw', PASSWORD);
    await page.click('#gate-go');
    await page.waitForSelector('#app:not([hidden])', { timeout: 90000 });
    check('the real password opens its project', await page.isVisible('#app'));
    const name = await page.textContent('#project-name');
    check('the header names the project', Boolean(name.trim()) && name !== '…', name);

    await page.waitForFunction(
      () => document.querySelectorAll('#data-body tr').length > 1,
      null, { timeout: 60000 });
    check('the published ciphertext decrypts and renders',
          /\d/.test(await page.textContent('#data-count')),
          await page.textContent('#data-count'));

    // The seam this exists for: Settings is read from the sheet by the Apps
    // Script, so it only works when that deployment is current AND the account
    // it runs as can open the project's spreadsheet.
    await page.click('nav button:has-text("Settings")');
    await page.waitForFunction(
      () => /max_pages|Read-only|could not|snapshot/i.test(
        document.getElementById('panel-settings').textContent),
      null, { timeout: 90000 });
    const banner = (await page.textContent('#settings-error')).trim();
    check('Settings is read LIVE from the sheet, not the last snapshot',
          !/snapshot|read-only/i.test(banner),
          banner || '(no banner)');

    await page.click('nav button:has-text("Keywords")');
    await page.waitForFunction(
      () => document.getElementById('panel-keywords').textContent.trim().length > 40,
      null, { timeout: 90000 });
    const keywordsError = (await page.textContent('#keywords-error')).trim();
    check('Keywords loads from the sheet', !keywordsError, keywordsError);

    check('no uncaught page errors', errors.length === 0, errors.join('\n       '));
  } catch (ex) {
    fail++;
    console.log('  FAIL harness: ' + ex.message);
    for (const id of ['gate-err', 'data-error', 'settings-error']) {
      try {
        const text = (await page.textContent('#' + id)).trim();
        if (text) console.log(`       ${id}: ${text}`);
      } catch { /* the page may be gone */ }
    }
  } finally {
    await browser.close();
  }

  console.log(`\n${pass} passed, ${fail} failed\n`);
  process.exit(fail ? 1 : 0);
})();
