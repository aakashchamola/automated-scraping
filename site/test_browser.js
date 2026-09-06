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

/* What each project's spreadsheet contains, as the service would serve it —
   raw rows, header first. The encrypted files are built from the same fixture,
   so a test can tell the live path from the snapshot path by content. */
/* Flipped to play a project that has just been created: the tabs exist and
   their headers are there, but nothing has ever run. */
let STUB_EMPTY = false;

function tabsFor(id) {
  if (STUB_EMPTY) {
    return {
      Jobs: [['Company', 'Role', 'Platform']],
      Settings: [['Group', 'Setting', 'Value', 'Type']],
    };
  }
  return {
    Jobs: [
      ['Company', 'Role', 'Platform'],
      [PROJECTS[id].name + ' Co', 'Data Analyst', 'linkedin'],
      ['Second ' + id, 'ML Engineer', 'greenhouse'],
    ],
    Settings: [
      ['Group', 'Setting', 'Value', 'Type'],
      ['Scraping', 'scraping.max_pages', id === 'main' ? '3' : '7', 'int'],
    ],
  };
}

/* Flipped to false to play a deployment made before `tabs` existed. It answers
   the action it does not know with a settings read, which is what the real one
   used to do. */
let STUB_KNOWS_TABS = true;

/* Flipped to play a session the service no longer recognises — a token that has
   expired or been revoked by a password change. */
let STUB_SIGNED_OUT = false;

/* Milliseconds the `tabs` reply is held back. The real service takes about
   nine seconds on a spreadsheet with ten tabs, which is exactly long enough
   for "sign-in awaits the tab index" to look like sign-in being broken. */
let STUB_TABS_DELAY_MS = 0;

/* Whether the deployment has an ADMIN_PASSWORD. It decides which gate the page
   opens with: the project list when there is something to check first, the
   password form when there is not. */
let STUB_ADMIN_PASSWORD = '';

// The queue the dashboard drives. Per project, because a run belongs to one.
const QUEUES = { main: [], biotech: [] };
const AGENTS = { main: { online: false, everSeen: false },
                 biotech: { online: false, everSeen: false } };

function startStub() {
  const posts = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (req.method === 'POST') {
      let body = '';
      req.on('data', (c) => { body += c; });
      req.on('end', () => {
        let parsed = null;
        try { parsed = JSON.parse(body); posts.push(parsed); }
        catch { posts.push({ unparsed: body }); }
        // The queue actions have to actually change something, or a read-back
        // proves nothing and the test passes on a stub that ignored the write.
        if (parsed && parsed.token) {
          const who = String(parsed.token).replace('token-', '');
          const queue = QUEUES[who];
          if (queue && parsed.action === 'requestRun') {
            queue.unshift({ id: 'run-' + (queue.length + 1), mode: parsed.mode,
                            status: 'queued', requested_at: new Date().toISOString(),
                            requested_by: parsed.requestedBy || '', claimed_by: '',
                            started_at: '', finished_at: '', exit_code: '', summary: '' });
          } else if (queue && parsed.action === 'cancelRun') {
            const hit = queue.find(r => r.id === parsed.id);
            if (hit) { hit.status = 'cancelled'; hit.finished_at = new Date().toISOString(); }
          }
        }
        // Deliberately no Access-Control-Allow-Origin: the real Web App sends
        // none either, which is why writes must go out no-cors.
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{"ok":true}');
      });
      return;
    }
    const p = url.searchParams;
    // Deliberately slow, so a timeout can be made to fire before the reply
    // arrives — which is the only way to reproduce a late JSONP callback.
    const delay = Number(p.get('delay') || 0);
    if (delay > 0) {
      const cb = p.get('callback') || 'callback';
      setTimeout(() => {
        res.writeHead(200, { 'Content-Type': 'text/javascript' });
        res.end(`${cb}({"ok":true,"slow":true});`);
      }, delay);
      return;
    }
    let payload;
    if (p.get('ping')) {
      payload = { ok: true, version: 4, standalone: true, activeProjects: 2,
                  adminPasswordConfigured: Boolean(STUB_ADMIN_PASSWORD) };
    } else if (p.get('action') === 'projects') {
      if (!STUB_ADMIN_PASSWORD) {
        payload = { ok: false, needsAdminPassword: true,
                    error: 'ADMIN_PASSWORD is not set' };
      } else if (p.get('adminPassword') !== STUB_ADMIN_PASSWORD) {
        payload = { ok: false, error: 'that is not the admin password' };
      } else {
        // Names only — never a key, a hash or a spreadsheet id.
        payload = { ok: true, projects: Object.entries(PROJECTS).map(([id, spec]) => ({
          id, name: spec.name, createdAt: '2026-01-01T00:00:00Z', notes: '' })) };
      }
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
      else if (STUB_SIGNED_OUT) {
        payload = { ok: false, error: 'no project matched that password',
                    signedOut: true };
      } else if (p.get('action') === 'tabs' && !STUB_KNOWS_TABS) {
        // What the deployed script actually answers: a named refusal, not a
        // settings read.
        payload = { ok: false, unknownAction: 'tabs',
                    error: "this deployment does not know the action 'tabs'" };
      } else if (p.get('action') === 'tabs' && STUB_KNOWS_TABS) {
        const tabs = tabsFor(id);
        payload = { ok: true, project: id, capturedAt: new Date().toISOString(),
                    spreadsheetId: 'sheet-' + id,
                    // No `columns`: the service stopped sending them, because
                    // the page never used them and they cost a round trip a tab.
                    worksheets: Object.entries(tabs).map(([name, rows]) => ({
                      name, row_count: rows.length - 1 })) };
        if (STUB_TABS_DELAY_MS) {
          const cb0 = p.get('callback') || 'callback';
          setTimeout(() => {
            res.writeHead(200, { 'Content-Type': 'text/javascript' });
            res.end(`${cb0}(${JSON.stringify(payload)});`);
          }, STUB_TABS_DELAY_MS);
          return;
        }
      } else if (p.get('action') === 'rows') {
        const want = p.get('worksheet') || '';
        payload = { ok: true, project: id, worksheet: want,
                    rows: tabsFor(id)[want] || [] };
      } else if (p.get('action') === 'runs') {
        payload = { ok: true, project: id, runs: QUEUES[id] || [],
                    agent: AGENTS[id] || { online: false, everSeen: false },
                    onlineWithinSec: 90 };
      } else if (p.get('action') === 'keywords') {
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

    /* Clearing the variables is not the same as clearing the screen. Each
       panel is drawn by its own loader, which runs when its tab is clicked, so
       switching project while sitting on Settings left the previous project's
       settings on display — editable, and belonging to another spreadsheet. */
    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    await page.click('#project-menu .menu-item:has-text("Biotech Jobs")');
    let swapped = true;
    try {
      await page.waitForFunction(() => {
        const field = document.querySelector('#settings-body input, #settings-body select');
        return field && field.value === '7';
      }, null, { timeout: 15000 });
    } catch { swapped = false; }
    check('switching project redraws the panel being looked at, with no reload',
          swapped, 'settings still showed ' +
          JSON.stringify(await page.inputValue('#settings-body input, #settings-body select')
            .catch(() => '')));

    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    await page.click('#project-menu .menu-item:has-text("LinkedIn Reachout")');
    await page.waitForFunction(() => {
      const field = document.querySelector('#settings-body input, #settings-body select');
      return field && field.value === '3';
    }, null, { timeout: 15000 });

    console.log('\nrunning it locally, and the project actions');
    await page.click('nav button:has-text("Run locally")');
    await page.waitForSelector('#panel-setup:not([hidden])');
    const command = await page.textContent('#setup-cmd');
    // Built from the page's own config, so a fork cannot leave a stale command.
    check('the install command names this repository and branch',
          command.includes('aakashchamola/automated-scraping') && command.includes('install.sh'),
          command.trim());
    check('and links to the script so it can be read first',
          (await page.getAttribute('#setup-read', 'href')).includes('/blob/'),
          await page.getAttribute('#setup-read', 'href'));

    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    const actions = await page.$$eval('#project-menu .menu-item', n => n.map(x => x.textContent));
    check('copy and delete are offered',
          actions.some(a => /Copy this project/.test(a)) &&
          actions.some(a => /Delete this project/.test(a)), actions.join(' | '));

    await page.click('#project-menu .menu-item:has-text("Delete this project")');
    await page.waitForSelector('#delete-project[open]');
    check('delete is refused until the name is typed', await page.isDisabled('#dp-go'));
    await page.fill('#dp-name', 'not the right name');
    check('a wrong name keeps it refused', await page.isDisabled('#dp-go'));
    await page.fill('#dp-name', await page.textContent('#dp-expected'));
    check('the exact name enables it', !(await page.isDisabled('#dp-go')));
    await page.click('#dp-cancel');
    await page.waitForFunction(() => !document.getElementById('delete-project').open);
    check('cancelling closes it having deleted nothing', await page.isVisible('#app'));

    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    await page.click('#project-menu .menu-item:has-text("Copy this project")');
    await page.waitForSelector('#new-project[open]');
    check('copy opens the dialog in copy mode',
          (await page.textContent('#new-project h2')).startsWith('Copy'),
          await page.textContent('#new-project h2'));
    check('and hides the adopt-a-spreadsheet section', await page.isHidden('#np-adopt'));
    check('while offering to bring the results across', await page.isVisible('#np-results-wrap'));
    await page.click('#np-cancel');
    await page.waitForFunction(() => !document.getElementById('new-project').open);
    await page.click('nav button:has-text("Data")');

    console.log('\na reply that arrives after the timeout');
    // Removing the script tag does not cancel the request, so a slow reply
    // still runs its callback. Deleting the name on timeout turned that into an
    // uncaught ReferenceError — seen on the live site, not in any stub.
    const beforeSlow = errors.length;
    const timedOut = await page.evaluate(async () => {
      try {
        // The stub takes 2s; give up after 200ms. The reply lands afterwards.
        await jsonp(`${window.DASHBOARD_CONFIG.settingsWebApp}?delay=2000`, 200);
        return false;
      } catch {
        return true;      // the timeout is expected
      }
    });
    check('the request does time out first', timedOut);
    await page.waitForTimeout(3500);   // long enough for the reply to land
    check('a late reply does not throw an uncaught error',
          errors.length === beforeSlow,
          errors.slice(beforeSlow).join('\n       '));

    console.log('\nthe switcher is legible in dark mode');
    // The switcher shipped invisible: its CSS named tokens the page never
    // defined, so the background rendered a light literal fallback while the
    // text inherited the dark theme's near-white. Nothing errored; it was
    // simply white on white. Only measuring real contrast catches that.
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    const contrast = await page.evaluate(() => {
      const lum = (c) => {
        const [r, g, b] = c.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number)
          .map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92
                                        : Math.pow((v + 0.055) / 1.055, 2.4); });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      // Walk up for the nearest painted background, as the browser does.
      const painted = (el) => {
        for (let n = el; n; n = n.parentElement) {
          const bg = getComputedStyle(n).backgroundColor;
          if (bg && !/rgba\(0, 0, 0, 0\)|transparent/.test(bg)) return bg;
        }
        return 'rgb(255,255,255)';
      };
      return [...document.querySelectorAll('#project-menu .menu-item')].map((el) => {
        const a = lum(getComputedStyle(el).color), b = lum(painted(el));
        const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        return { text: el.textContent.trim().slice(0, 26), ratio: +ratio.toFixed(2) };
      });
    });
    const worst = contrast.reduce((a, b) => (a.ratio < b.ratio ? a : b), contrast[0]);
    // 4.5:1 is WCAG AA for body text. The bug measured about 1.1:1.
    check('every menu item meets 4.5:1 against its background',
          contrast.every((c) => c.ratio >= 4.5),
          contrast.map((c) => `${c.text} ${c.ratio}:1`).join(' | '));
    console.log(`       worst: "${worst.text}" at ${worst.ratio}:1`);
    await page.keyboard.press('Escape');
    await page.click('header h1');
    await page.emulateMedia({ colorScheme: 'light' });

    console.log('\na remembered session survives a bad network');
    // Throwing a session away on any error would make one offline reload cost
    // every remembered project its full ten days.
    await page.route('**/data/**', route => route.abort());
    await page.reload();
    await page.waitForTimeout(1500);
    await page.unroute('**/data/**');
    await page.reload();
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    check('an offline reload does not sign you out',
          await page.isHidden('#gate'));
    check('and both projects are still remembered',
          (await page.$$eval('#project-menu .menu-item', n => n.length)) >= 0);

    console.log('\nlocking');
    await page.click('#btn-lock');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    check('Lock signs out of every project', await page.isHidden('#app'));
    await page.reload();
    await page.waitForSelector('#gate:not([hidden])');
    check('and it stays locked after a reload', await page.isHidden('#app'));

    console.log('\nthe data is what the sheet says now, not the last publish');
    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    await signIn('main-password-1');
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    check('the freshness chip says it is live',
          /live from the sheet/i.test(await page.textContent('#captured')),
          await page.textContent('#captured'));

    // The published files are a snapshot a CI job used to write. With them
    // unreachable the dashboard must be entirely unaffected, because it is not
    // reading them any more.
    await page.route('**/data/**', route => route.abort());
    await page.reload();
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    await page.waitForFunction(
      () => document.getElementById('data-body').textContent.includes('LinkedIn Reachout Co'),
      null, { timeout: 20000 });
    check('with no published files at all, the data still loads',
          (await page.textContent('#data-body')).includes('ML Engineer'));
    check('and it is still marked live',
          /live from the sheet/i.test(await page.textContent('#captured')));
    await page.unroute('**/data/**');

    // The other direction: a deployment that cannot serve tabs must fall back
    // to the snapshot rather than showing an empty dashboard. An older one
    // answers an action it does not know by reading Settings instead — a valid
    // reply to a different question, which is exactly what makes it dangerous.
    STUB_KNOWS_TABS = false;
    await page.reload();
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    await page.waitForFunction(
      () => document.getElementById('data-body').textContent.includes('LinkedIn Reachout Co'),
      null, { timeout: 20000 });
    check('an older deployment falls back to the published snapshot',
          /data from/i.test(await page.textContent('#captured')),
          await page.textContent('#captured'));
    check('and says it is a snapshot rather than implying it is current',
          /last export/i.test(await page.getAttribute('#captured', 'title') || ''),
          await page.getAttribute('#captured', 'title'));
    STUB_KNOWS_TABS = true;

    await page.click('#btn-lock');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });

    console.log('\nchoosing a project from a list');
    /* The password used to be the only way in, and it SELECTED the project —
       so you had to know which password went with which project before you
       could open anything. With an admin password set, the page lists them
       first and each still opens with its own. */
    STUB_ADMIN_PASSWORD = 'the-admin-password';
    await page.goto(URL_);
    await page.waitForSelector('#admin-form:not([hidden])', { timeout: 20000 });
    check('it asks for the admin password first', await page.isHidden('#gate-form'));
    check('and shows no project names before it is given',
          !/LinkedIn Reachout|Biotech/.test(await page.textContent('#gate')));

    await page.fill('#admin-pw', 'wrong');
    await page.click('#admin-go');
    await page.waitForFunction(
      () => document.getElementById('admin-err').textContent.trim(),
      null, { timeout: 20000 });
    check('a wrong admin password is refused',
          /not the admin password/i.test(await page.textContent('#admin-err')));
    check('and still lists nothing', await page.isHidden('#picker'));

    await page.fill('#admin-pw', 'the-admin-password');
    await page.click('#admin-go');
    await page.waitForSelector('#picker:not([hidden])', { timeout: 20000 });
    const listed = await page.textContent('#picker-list');
    check('the right one lists every project',
          /LinkedIn Reachout/.test(listed) && /Biotech Jobs/.test(listed), listed);
    check('and the password form is out of the way',
          await page.isHidden('#gate-form'));

    await page.click('#picker-list .picker-item:has-text("Biotech Jobs")');
    await page.waitForSelector('#gate-form:not([hidden])', { timeout: 15000 });
    check('picking one asks for that project\'s password',
          /Biotech Jobs/.test(await page.textContent('#gate-title')),
          await page.textContent('#gate-title'));

    // The password still selects the project, so the wrong one would otherwise
    // drop you silently into somebody else's.
    await page.fill('#gate-pw', 'main-password-1');
    await page.click('#gate-go');
    await page.waitForFunction(
      () => document.getElementById('gate-err').textContent.trim(),
      null, { timeout: 25000 });
    check('another project\'s password is refused by name, not silently accepted',
          /not the password for Biotech Jobs/i.test(await page.textContent('#gate-err')),
          await page.textContent('#gate-err'));
    check('and it did not let anyone in', await page.isHidden('#app'));

    await page.click('#gate-back');
    await page.waitForSelector('#picker:not([hidden])', { timeout: 15000 });
    check('Back returns to the list', await page.isHidden('#gate-form'));

    await page.click('#picker-list .picker-item:has-text("Biotech Jobs")');
    await page.fill('#gate-pw', 'biotech-password-1');
    await page.click('#gate-go');
    await page.waitForSelector('#app:not([hidden])', { timeout: 25000 });
    check('its own password opens it',
          (await page.textContent('#project-name')).includes('Biotech'),
          await page.textContent('#project-name'));

    // Being handed one project password should be enough on its own.
    await page.click('#btn-lock');
    await page.waitForSelector('#admin-form:not([hidden])', { timeout: 15000 });
    await page.click('#admin-skip');
    await page.waitForSelector('#gate-form:not([hidden])', { timeout: 15000 });
    await page.fill('#gate-pw', 'main-password-1');
    await page.click('#gate-go');
    await page.waitForSelector('#app:not([hidden])', { timeout: 25000 });
    check('someone with only a project password can skip the list entirely',
          (await page.textContent('#project-name')).includes('LinkedIn Reachout'));

    await page.click('#btn-lock');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    STUB_ADMIN_PASSWORD = '';

    // With no admin password there is nothing to check, so listing every
    // project to anyone with the URL is not the default.
    await page.goto(URL_);
    await page.waitForSelector('#gate-form:not([hidden])', { timeout: 20000 });
    check('with no admin password set it goes straight to the password',
          await page.isHidden('#admin-form') && await page.isHidden('#picker'));

    console.log('\nsigning in does not wait for the data');
    /* It did, and for nine seconds: reading a real spreadsheet's tab index is
       slow, and the door was held shut for the whole of it while the password
       had already been accepted. Signing in waits for the password and
       nothing else. */
    STUB_TABS_DELAY_MS = 6000;
    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    const startedAt = Date.now();
    await signIn('main-password-1');
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    const openedIn = Date.now() - startedAt;
    check('the app opens without waiting for the tab index',
          openedIn < 4000, `${openedIn}ms, with the index held back 6000ms`);
    check('and says the sheet is still being read',
          /reading the sheet/i.test(await page.textContent('#captured')),
          await page.textContent('#captured'));

    // Everything that does not need the index is usable immediately.
    await page.click('[data-panel="panel-runs"]');
    await page.waitForSelector('#run-actions .task', { timeout: 10000 });
    check('and the other panels work while it is still loading',
          await page.isEnabled('#run-actions .task button'));

    // Then it arrives and fills itself in, with no reload.
    await page.click('[data-panel="panel-data"]');
    await page.waitForFunction(
      () => document.getElementById('data-body').textContent.includes('LinkedIn Reachout Co'),
      null, { timeout: 25000 });
    check('the data appears on its own once the index arrives',
          /live from the sheet/i.test(await page.textContent('#captured')),
          await page.textContent('#captured'));
    STUB_TABS_DELAY_MS = 0;

    await page.click('#btn-lock');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });

    console.log('\nsigning in when there is no data to be had');
    /* Exactly how it shipped broken: publishing stopped writing snapshots in
       the same change that started reading tabs live, against a deployment
       that did not serve tabs yet. Neither source existed and sign-in died on
       a 404 for data nobody had asked to see. Signing in may depend on the
       password and nothing else. */
    STUB_KNOWS_TABS = false;
    await page.route('**/data/**', route => route.fulfill({ status: 404, body: 'no' }));
    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    await signIn('main-password-1');
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    check('the password still gets you in', await page.isHidden('#gate'));
    check('and it says why the table is empty rather than showing nothing',
          /could not read the sheet/i.test(await page.textContent('#data-error')),
          await page.textContent('#data-error'));
    check('the chip does not claim data it does not have',
          /no data yet/i.test(await page.textContent('#captured')),
          await page.textContent('#captured'));

    // The panels that need no data must be entirely unaffected.
    await page.click('[data-panel="panel-keywords"]');
    // Editable keywords are inputs, so their text is in .value, not textContent.
    await page.waitForFunction(
      () => [...document.querySelectorAll('#keywords-list input')]
        .some(i => /analyst/.test(i.value)),
      null, { timeout: 20000 });
    check('Keywords still works, and is still editable',
          await page.isEnabled('#keyword-add'));
    await page.click('[data-panel="panel-runs"]');
    await page.waitForSelector('#run-actions .task', { timeout: 15000 });
    check('and so does Runs', await page.isEnabled('#run-actions .task button'));

    /* THE BUG THIS SHIPPED WITH. Staying signed in was stored correctly and
       then thrown away on the next load: the record was only kept if it had a
       decryption key, and once the page read the sheet live instead of reading
       published snapshots there was nothing to derive a key from. So the
       password was asked for on every single reload. The test that covered
       reloading passed throughout, because its stub served a snapshot and so
       always had a key — which is exactly why this is checked HERE, in the
       block where there is no snapshot at all. */
    await page.reload();
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    check('a reload still does not ask again when there is no snapshot to key from',
          await page.isHidden('#gate'));

    // A DEAD SESSION is the one refusal that must still stop everything —
    // signing in on a stale token would leave every panel failing instead.
    await page.evaluate(() => {
      window.__realFetchToken = true;
    });
    STUB_SIGNED_OUT = true;
    await page.reload();
    await page.waitForSelector('#gate:not([hidden])', { timeout: 20000 });
    check('but a session the service has forgotten asks for the password again',
          await page.isHidden('#app'));
    STUB_SIGNED_OUT = false;
    STUB_KNOWS_TABS = true;
    await page.unroute('**/data/**');

    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });

    console.log('\na project that has just been created');
    /* Every tab has a header and no rows. Dropping empty tabs from the picker
       left the panel blank under "no worksheets", which is the first thing
       anyone sees after making a project and reads as a failure. */
    STUB_EMPTY = true;
    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    await signIn('main-password-1');
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    await page.waitForFunction(() =>
      [...document.querySelectorAll('#sheet-select option')]
        .some((o) => o.textContent.includes('Jobs')), null, { timeout: 20000 });
    const emptyOption = await page.textContent('#sheet-select option');
    check('an empty tab is still offered, and says it is empty',
          /empty/i.test(emptyOption), emptyOption);
    await page.waitForFunction(() =>
      document.getElementById('data-head').textContent.includes('Company'),
      null, { timeout: 15000 });
    check('its columns are shown, so the shape of what is coming is visible',
          /Company/.test(await page.textContent('#data-head'))
          && /Role/.test(await page.textContent('#data-head')),
          await page.textContent('#data-head'));
    check('and the table says it is waiting rather than blaming a filter',
          /nothing here yet/i.test(await page.textContent('#data-body')),
          await page.textContent('#data-body'));
    STUB_EMPTY = false;
    // Sign out again: this block signed in, and now that a session survives a
    // reload the next block would resume straight past its own gate.
    await page.click('#btn-lock');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });

    console.log('\nstarting a run on your own machine');
    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    await signIn('main-password-1');
    await page.waitForSelector('#app:not([hidden])', { timeout: 20000 });
    await page.click('[data-panel="panel-runs"]');
    await page.waitForSelector('#run-actions .task', { timeout: 15000 });

    // Nothing has ever connected, and that must be said rather than implied by
    // a run that silently never starts.
    const cold = await page.textContent('#agent-status');
    check('with no machine connected the page says so',
          /No machine has connected/i.test(cold), cold);
    check('but Run is still offered, because the queue waits',
          await page.isEnabled('#run-actions .task button'));

    AGENTS.main = { online: true, everSeen: true, agent: 'the-laptop', secondsAgo: 3 };
    await page.click('#btn-refresh-runs');
    await page.waitForFunction(
      () => /the-laptop/.test(document.getElementById('agent-status').textContent),
      null, { timeout: 15000 });
    check('and names the machine once one is listening',
          /listening/.test(await page.textContent('#agent-status')));

    const queueLength = () => QUEUES.main.length;
    await page.click('#run-actions .task button');
    await page.waitForFunction(
      () => document.querySelectorAll('#runs-body tr').length > 0 &&
            !/Nothing has been run/.test(document.getElementById('runs-body').textContent),
      null, { timeout: 25000 });
    check('pressing Run queues exactly one run', queueLength() === 1,
          JSON.stringify(QUEUES.main));
    check('and it was sent as a request, not a command',
          stub.posts.some(p => p.action === 'requestRun' && p.mode === 'full'),
          JSON.stringify(stub.posts.slice(-1)));
    check('the button then shows the run is queued, and cannot be pressed twice',
          await page.isDisabled('#run-actions .task button'));

    // The row is the proof: the page must show what the queue actually says.
    const rowText = await page.textContent('#runs-body');
    check('the queued run is listed', /queued/.test(rowText), rowText.slice(0, 120));

    // Cancel, and prove it reached the far side rather than only the screen.
    await page.click('#runs-body button');
    await page.waitForFunction(
      () => /cancelled/.test(document.getElementById('runs-body').textContent),
      null, { timeout: 25000 });
    check('cancelling reaches the queue',
          QUEUES.main[0].status === 'cancelled', QUEUES.main[0].status);
    check('and the card is offered again once it is over',
          await page.isEnabled('#run-actions .task button'));

    // A finished run's output is on the machine; the summary is here.
    QUEUES.main.unshift({ id: 'run-old', mode: 'validate-only', status: 'failed',
                          requested_at: new Date().toISOString(), requested_by: 'dashboard',
                          claimed_by: 'the-laptop', started_at: new Date().toISOString(),
                          finished_at: new Date().toISOString(), exit_code: '3',
                          summary: 'exit 3 after 12s\n\nTraceback: the sheet is gone' });
    await page.click('#btn-refresh-runs');
    await page.waitForFunction(
      () => /failed/.test(document.getElementById('runs-body').textContent),
      null, { timeout: 15000 });
    await page.click('#runs-body tr:first-child button');
    await page.waitForSelector('#run-detail:not([hidden])', { timeout: 10000 });
    const detail = await page.textContent('#run-detail');
    check('a failed run shows what went wrong', /the sheet is gone/.test(detail), detail);
    check('and says where the full log is', /logs\/agent\/run-old\.log/.test(detail), detail);

    // A queue belongs to its project, like everything else. Signed into rather
    // than switched to: the lock test above signed out of everything, so
    // Biotech is not unlocked at this point.
    await page.click('#project-btn');
    await page.waitForSelector('#project-menu:not([hidden])');
    await page.click('#project-menu .menu-item:has-text("Unlock another project")');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    await signIn('biotech-password-1');
    await page.waitForFunction(
      () => document.getElementById('project-name').textContent.includes('Biotech'),
      null, { timeout: 20000 });
    await page.click('[data-panel="panel-runs"]');
    await page.waitForSelector('#run-actions .task', { timeout: 15000 });
    const otherRuns = await page.textContent('#runs-body');
    check("another project's queue is its own",
          /Nothing has been run/.test(otherRuns), otherRuns.slice(0, 80));

    // Locked again, because the next test starts from a signed-out page.
    await page.click('#btn-lock');
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });

    console.log('\nwhen the sign-in service is down');
    // There is no offline fallback any more: it stamped a real project id into
    // a world-readable config.js, and only ever worked for a project whose
    // data key happened to equal its password.
    stub.server.close();
    await page.goto(URL_);
    await page.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
    await signIn('main-password-1');
    await page.waitForFunction(() => document.getElementById('gate-err').textContent.trim(),
                               null, { timeout: 20000 });
    const downMessage = await page.textContent('#gate-err');
    check('it says so plainly rather than failing as a crypto error',
          /unreachable/i.test(downMessage), downMessage);
    check('and does not let anyone in', await page.isHidden('#app'));

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
    try { stub.server.close(); } catch { /* the last test closes it */ }
    fs.rmSync(root, { recursive: true, force: true });
  }

  console.log(`\n${passed} passed, ${failed} failed\n`);
  process.exit(failed ? 1 : 0);
})();
