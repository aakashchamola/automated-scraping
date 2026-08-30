/**
 * test_browser.js — drives the published dashboard in a real browser.
 *
 *     node site/test_browser.js
 *
 * The parts most worth testing here cannot be tested any other way: a login
 * that talks to a cross-origin service over JSONP, a switcher backed by
 * IndexedDB, and per-project decryption. All three have broken before in ways
 * only a browser would notice — most memorably a CSS rule that beat [hidden],
 * leaving an invisible overlay that swallowed every click.
 *
 * Two projects are served, each encrypted under its own key, alongside a stub
 * of the Apps Script that speaks the same JSONP the real one does.
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const os = require('os');

const PW = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const PROJECTS = {
  main:   { name: 'LinkedIn Reachout', password: 'main-password-1',   key: 'data-key-for-main' },
  biotech:{ name: 'Biotech Jobs',      password: 'biotech-password-1', key: 'data-key-for-biotech' },
};

/* ── Building the published files, the way encrypt_snapshot.py does ─────── */

function encrypt(objectToEncrypt, password, salt) {
  const iterations = 200000;
  const key = crypto.pbkdf2Sync(password, salt, iterations, 32, 'sha256');
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const body = Buffer.concat([cipher.update(JSON.stringify(objectToEncrypt), 'utf8'),
                              cipher.final(), cipher.getAuthTag()]);
  return {
    v: 1,
    kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations, salt: salt.toString('base64') },
    cipher: { name: 'AES-GCM', iv: iv.toString('base64') },
    data: body.toString('base64'),
  };
}

function buildSite(root) {
  for (const file of ['index.html', 'app.js', 'style.css']) {
    fs.copyFileSync(path.join(__dirname, file), path.join(root, file));
  }
  fs.writeFileSync(path.join(root, 'config.js'),
    `window.DASHBOARD_CONFIG = ${JSON.stringify({
      repo: 'aakashchamola/automated-scraping',
      workflow: 'scheduled-pipeline.yml',
      branch: 'test',
      builtAt: new Date().toISOString(),
      settingsWebApp: 'http://127.0.0.1:' + STUB_PORT + '/exec',
      defaultProject: 'main',
    })};`);

  for (const [id, spec] of Object.entries(PROJECTS)) {
    const dir = path.join(root, 'data', id);
    fs.mkdirSync(dir, { recursive: true });
    const salt = crypto.randomBytes(16);          // one per project per publish
    const rows = [
      { _row: 2, Company: spec.name + ' Co', Role: 'Data Analyst', Platform: 'linkedin' },
      { _row: 3, Company: 'Second ' + id, Role: 'ML Engineer', Platform: 'greenhouse' },
    ];
    const files = {
      index: {
        captured_at: '2026-08-30T12:00:00+00:00',
        spreadsheet_id: 'sheet-' + id,
        worksheets: [
          { name: 'Settings', row_count: 2, columns: ['Group', 'Setting', 'Value', 'Type'], file: 'Settings.json' },
          { name: 'Jobs', row_count: rows.length, columns: ['Company', 'Role', 'Platform'], file: 'Jobs.json' },
        ],
      },
      Jobs: { worksheet: 'Jobs', columns: ['Company', 'Role', 'Platform'], rows, row_count: rows.length },
      Settings: {
        worksheet: 'Settings', columns: ['Group', 'Setting', 'Value', 'Type'],
        rows: [{ _row: 2, Group: 'Scraping', Setting: 'scraping.max_pages', Value: id === 'main' ? '3' : '7', Type: 'int' }],
        row_count: 1,
      },
    };
    for (const [name, payload] of Object.entries(files)) {
      fs.writeFileSync(path.join(dir, name + '.enc.json'),
                       JSON.stringify(encrypt(payload, spec.key, salt)));
    }
  }
}

/* ── The stub Apps Script, on its own origin so JSONP is genuinely needed ── */

const STUB_PORT = 8788;
const SITE_PORT = 8787;

function startStub() {
  const posts = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (req.method === 'POST') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        try { posts.push(JSON.parse(body)); } catch { posts.push({ unparsed: body }); }
        // Deliberately no Access-Control-Allow-Origin: the real Web App sends
        // none either, which is why writes must go out no-cors.
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{"ok":true}');
      });
      return;
    }
    const p = url.searchParams;
    let payload;
    if (p.get('ping')) {
      payload = { ok: true, version: 4, standalone: true, activeProjects: 2,
                  adminPasswordConfigured: false };
    } else if (p.get('action') === 'auth') {
      const given = p.get('password') || '';
      const hit = Object.entries(PROJECTS).find(([, s]) => s.password === given);
      payload = hit
        ? { ok: true, project: hit[0], name: hit[1].name, dataKey: hit[1].key,
            token: 'token-' + hit[0], ttlMs: 864e6 }
        : { ok: false, error: 'no project matched that password' };
    } else {
      const id = (p.get('token') || '').replace('token-', '');
      const spec = PROJECTS[id];
      if (!spec) payload = { ok: false, error: 'no project matched that password', signedOut: true };
      else if (p.get('action') === 'keywords') {
        payload = { ok: true, project: id, keywords: { keywords: [id + ' analyst'], column: 'Search Term' } };
      } else {
        payload = { ok: true, project: id, settings: { columns: ['Group', 'Setting', 'Value', 'Type'],
          rows: [{ Group: 'Scraping', Setting: 'scraping.max_pages',
                   Value: id === 'main' ? '3' : '7', Type: 'int', Options: '1 - 10' }] } };
      }
    }
    const cb = p.get('callback') || 'callback';
    res.writeHead(200, { 'Content-Type': 'text/javascript' });
    res.end(`${cb}(${JSON.stringify(payload)});`);
  });
  server.listen(STUB_PORT);
  return { server, posts };
}

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
               '.json': 'application/json' };

function startSite(root) {
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    const file = path.join(root, rel === '/' ? 'index.html' : rel);
    if (!file.startsWith(root) || !fs.existsSync(file)) { res.writeHead(404); res.end('no'); return; }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'text/plain' });
    res.end(fs.readFileSync(file));
  });
  server.listen(SITE_PORT);
  return server;
}

/* ── Test ──────────────────────────────────────────────────────────────── */

let passed = 0, failed = 0;
function check(label, ok, detail) {
  if (ok) { passed++; console.log('  ok   ' + label); }
  else { failed++; console.log('  FAIL ' + label + (detail ? '\n       ' + detail : '')); }
}

(async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-'));
  buildSite(root);
  const stub = startStub();
  const site = startSite(root);

  const browser = await PW.chromium.launch();
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const URL_ = `http://127.0.0.1:${SITE_PORT}/`;

  const signIn = async (password) => {
    await page.fill('#gate-pw', password);
    await page.click('#gate-go');
  };

  try {
    console.log('\nDashboard, in a browser\n');
    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])');
    console.log('the gate');
    check('asks for a password before showing anything',
          await page.isHidden('#app'));

    await signIn('definitely-wrong');
    await page.waitForFunction(() => document.getElementById('gate-err').textContent.trim());
    check('a wrong password is refused by name, not by a crypto error',
          /no project matched/i.test(await page.textContent('#gate-err')));
    check('and the app stays hidden', await page.isHidden('#app'));

    console.log('\nsigning in');
    await signIn('main-password-1');
    await page.waitForSelector('#app:not([hidden])', { timeout: 15000 });
    check('the right password opens its project', await page.isVisible('#app'));
    check('the header names the project',
          (await page.textContent('#project-name')).includes('LinkedIn Reachout'),
          await page.textContent('#project-name'));

    // The overlay bug: hidden but still covering the page, eating every click.
    const clickable = await page.evaluate(() => {
      const r = document.querySelector('header h1').getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + 5, r.y + 5);
      return hit && !hit.closest('#gate');
    });
    check('the unlocked overlay does not swallow clicks', clickable);

    await page.waitForFunction(() => document.querySelectorAll('#data-body tr').length > 0);
    check('its data decrypts and renders',
          (await page.textContent('#data-body')).includes('LinkedIn Reachout Co'));

    console.log('\nunlocking a second project');
    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    const items = await page.$$eval('#project-menu .menu-item', (n) => n.map((x) => x.textContent.trim()));
    check('the menu lists only what is unlocked, plus the actions',
          items.filter((x) => x.includes('LinkedIn Reachout')).length === 1 &&
          !items.some((x) => x.includes('Biotech')), items.join(' | '));

    await page.click('#project-menu .menu-item:has-text("Unlock another project")');
    await page.waitForSelector('#gate:not([hidden])');
    check('the gate says it is adding a project',
          (await page.textContent('#gate-title')).includes('another project'));
    check('and can be cancelled', await page.isVisible('#gate-cancel'));

    await signIn('biotech-password-1');
    // #app is already visible — the gate was opened over it — so waiting on
    // that would return before the unlock had done anything.
    await page.waitForFunction(
      () => document.getElementById('project-name').textContent.includes('Biotech'),
      null, { timeout: 15000 });
    check('the second project opens', await page.isHidden('#gate'));
    await page.waitForFunction(() =>
      document.getElementById('data-body').textContent.includes('Biotech Jobs Co'));
    check('showing its own data, not the first project\'s',
          !(await page.textContent('#data-body')).includes('LinkedIn Reachout Co'));

    console.log('\nswitching');
    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    const both = await page.$$eval('#project-menu .menu-item', (n) => n.map((x) => x.textContent.trim()));
    check('both unlocked projects are now listed',
          both.some((x) => x.includes('LinkedIn Reachout')) && both.some((x) => x.includes('Biotech Jobs')),
          both.join(' | '));

    await page.click('#project-menu .menu-item:has-text("LinkedIn Reachout")');
    await page.waitForFunction(() =>
      document.getElementById('data-body').textContent.includes('LinkedIn Reachout Co'));
    check('switching back needs no password at all',
          (await page.textContent('#project-name')).includes('LinkedIn Reachout'));

    console.log('\nstaying signed in');
    await page.reload();
    await page.waitForSelector('#app:not([hidden])', { timeout: 15000 });
    check('a reload resumes without asking again', await page.isHidden('#gate'));
    check('and returns to the project last looked at',
          (await page.textContent('#project-name')).includes('LinkedIn Reachout'));

    console.log('\nper-project settings');
    await page.click('nav button:has-text("Settings")');
    await page.waitForFunction(() =>
      document.getElementById('settings-body').textContent.includes('max_pages'), null, { timeout: 15000 });
    const mainValue = await page.inputValue('#settings-body input, #settings-body select').catch(() => '');
    check('the live sheet is read for the project you are in',
          mainValue === '3', 'got ' + JSON.stringify(mainValue));

    console.log('\nlocking');
    await page.click('#btn-lock');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    check('Lock signs out of every project', await page.isHidden('#app'));
    await page.reload();
    await page.waitForSelector('#gate:not([hidden])');
    check('and it stays locked after a reload', await page.isHidden('#app'));

    check('no uncaught page errors throughout', errors.length === 0, errors.join('\n       '));
  } catch (ex) {
    failed++;
    console.log('  FAIL harness: ' + ex.message);
    try {
      console.log('       gate says: ' + (await page.textContent('#gate-err')));
      console.log('       data err : ' + (await page.textContent('#data-error')));
    } catch { /* page may be gone */ }
    if (errors.length) console.log('       page errors:\n       ' + errors.join('\n       '));
  } finally {
    await browser.close();
    site.close();
    stub.server.close();
    fs.rmSync(root, { recursive: true, force: true });
  }

  console.log(`\n${passed} passed, ${failed} failed\n`);
  process.exit(failed ? 1 : 0);
})();
