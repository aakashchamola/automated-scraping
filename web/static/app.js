/* Control panel front-end.
   Three independent panels over the Flask API; no framework, no build step —
   the dashboard has to keep working after a `git pull` on a laptop with only
   Python installed. */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const banner = (host, kind, msg) => {
  host.innerHTML = '';
  if (msg) host.append(Object.assign(el('div', `banner ${kind}`, msg)));
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (res.status === 401 || body.login_required) {
    // The session expired mid-visit. Bounce to the login page rather than
    // leaving every panel showing a bare "not signed in" error.
    location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
    throw new Error('signed out');
  }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

/* ── Tabs ──────────────────────────────────────────────────────────────── */

document.querySelectorAll('nav button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach((b) => {
      const on = b === btn;
      b.setAttribute('aria-selected', String(on));
      $(b.dataset.panel).hidden = !on;
    });
  });
});

/* ── Data panel ────────────────────────────────────────────────────────── */

const data = {
  worksheet: null, columns: [], rows: [],
  search: '', facets: {}, sort: null, sortDir: 1, page: 0, pageSize: 100,
};

/* A column is worth a dropdown when it has few enough distinct values to
   choose from — Platform, Keyword, Job Status. A column of unique job links
   is not a filter, it is noise. */
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
      const have = (row[col] || '').trim() || '(blank)';
      if (have !== want) return false;
    }
    if (!q) return true;
    return data.columns.some((c) => (row[c] || '').toLowerCase().includes(q));
  });
  if (data.sort) {
    const col = data.sort;
    rows = rows.slice().sort((a, b) => {
      const x = (a[col] || ''), y = (b[col] || '');
      const nx = parseFloat(x.replace(/,/g, '')), ny = parseFloat(y.replace(/,/g, ''));
      const both = !isNaN(nx) && !isNaN(ny) && x.trim() !== '' && y.trim() !== '';
      const cmp = both ? nx - ny : x.localeCompare(y, undefined, { numeric: true });
      return cmp * data.sortDir;
    });
  }
  return rows;
}

function statusPill(col, value) {
  const v = (value || '').toLowerCase();
  const map = {
    active: 'ok', expired: 'err', removed: 'err', unknown: 'warn',
    university: 'info', government: 'info', 'nonprofit / ngo': 'info',
    'hospital / medical': 'info', 'research institute': 'info',
    'educational institution': 'info', company: 'neutral', other: 'neutral',
  };
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
    const tr = el('tr');
    const td = el('td', 'muted', 'No rows match these filters.');
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
  const total = data.rows.length;
  $('data-count').textContent = rows.length === total
    ? `${total} rows` : `${rows.length} of ${total} rows`;
}

function renderFacets() {
  const host = $('facets');
  host.innerHTML = '';
  facetableColumns(data.columns, data.rows).forEach((col) => {
    const group = el('div', 'filter-group');
    group.append(el('label', '', col));
    const sel = el('select');
    sel.append(new Option(`All`, ''));
    distinct(col).forEach(([v, n]) => sel.append(new Option(`${v} (${n})`, v)));
    sel.value = data.facets[col] || '';
    sel.addEventListener('change', () => {
      data.facets[col] = sel.value; data.page = 0; renderTable();
    });
    group.append(sel);
    host.append(group);
  });
}

async function loadSheet(worksheet, force) {
  banner($('data-error'), '', '');
  $('data-count').textContent = 'loading…';
  try {
    const payload = await api(`/api/sheet/${encodeURIComponent(worksheet)}${force ? '?refresh=1' : ''}`);
    data.worksheet = worksheet;
    data.columns = payload.columns;
    data.rows = payload.rows;
    data.facets = {}; data.page = 0; data.sort = null;
    renderFacets();
    renderTable();
  } catch (err) {
    $('data-count').textContent = '';
    banner($('data-error'), 'err', `Could not read “${worksheet}”: ${err.message}`);
  }
}

$('search').addEventListener('input', (e) => { data.search = e.target.value; data.page = 0; renderTable(); });
$('btn-clear').addEventListener('click', () => {
  data.search = ''; $('search').value = ''; data.facets = {}; data.page = 0;
  renderFacets(); renderTable();
});
$('btn-refresh').addEventListener('click', () => loadSheet(data.worksheet, true));
$('page-prev').addEventListener('click', () => { data.page--; renderTable(); });
$('page-next').addEventListener('click', () => { data.page++; renderTable(); });
$('page-size').addEventListener('change', (e) => { data.pageSize = +e.target.value; data.page = 0; renderTable(); });
$('sheet-select').addEventListener('change', (e) => loadSheet(e.target.value, false));

$('btn-export').addEventListener('click', () => {
  const rows = filteredRows();
  const esc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const csv = [data.columns.map(esc).join(',')]
    .concat(rows.map((r) => data.columns.map((c) => esc(r[c])).join(',')))
    .join('\n');
  const a = el('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  a.download = `${data.worksheet}-filtered.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ── Run panel ─────────────────────────────────────────────────────────── */

let stream = null;
let currentRunId = null;

function optionControls(task) {
  const host = el('div', 'opts');
  (task.options || []).forEach((opt) => {
    const label = el('label');
    let input;
    if (opt.type === 'bool') {
      input = el('input'); input.type = 'checkbox';
      label.append(input, document.createTextNode(opt.label));
    } else {
      input = el('input');
      input.type = opt.type === 'int' ? 'number' : 'text';
      if (opt.placeholder) input.placeholder = opt.placeholder;
      if (opt.default !== undefined) input.value = opt.default;
      label.append(document.createTextNode(opt.label), input);
    }
    input.dataset.flag = opt.flag;
    input.dataset.kind = opt.type;
    host.append(label);
  });
  return host;
}

function renderTasks(tasks) {
  const grid = $('task-grid');
  grid.innerHTML = '';
  tasks.forEach((task) => {
    const card = el('div', `task${task.primary ? ' primary' : ''}`);
    const h = el('h3', '', task.label);
    if (task.primary) h.append(el('span', 'pill info', 'main'));
    card.append(h, el('p', '', task.blurb));
    if (task.detail) card.append(el('div', 'detail', task.detail));
    const opts = optionControls(task);
    card.append(opts);
    const row = el('div', 'run-row');
    const btn = el('button', 'btn primary', 'Run');
    btn.addEventListener('click', () => {
      const options = {};
      opts.querySelectorAll('input').forEach((i) => {
        options[i.dataset.flag] = i.dataset.kind === 'bool' ? i.checked : i.value;
      });
      startRun(task.key, options);
    });
    row.append(btn);
    card.append(row);
    grid.append(card);
  });
}

function setRunning(on, summary) {
  document.querySelectorAll('#task-grid button').forEach((b) => { b.disabled = on; });
  $('btn-stop').disabled = !on;
  const ind = $('run-indicator');
  ind.className = `pill ${on ? 'warn' : 'neutral'}`;
  ind.textContent = on ? `running: ${summary.label}` : 'idle';
}

function appendLine(line) {
  const box = $('console');
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();
  const row = el('div', `ln ${line.level}`);
  row.append(el('span', 'ts', line.t), el('span', 'tx', line.text));
  box.append(row);
  if ($('autoscroll').checked) box.scrollTop = box.scrollHeight;
}

function describeRun(s) {
  const cls = { done: 'ok', failed: 'err', stopped: 'warn', running: 'warn', starting: 'warn' }[s.status] || 'neutral';
  const host = $('run-meta');
  host.innerHTML = '';
  host.append(
    el('span', `pill ${cls}`, s.status),
    el('span', '', s.label),
    el('span', 'faint', `started ${s.started_at.replace('T', ' ')}`),
    el('span', 'faint', `${s.duration_seconds}s`),
    el('span', 'faint mono', s.command),
  );
}

function attach(runId) {
  if (stream) stream.close();
  currentRunId = runId;
  stream = new EventSource(`/api/run/${runId}/stream`);
  stream.onmessage = (e) => appendLine(JSON.parse(e.data));
  stream.addEventListener('end', (e) => {
    const summary = JSON.parse(e.data);
    describeRun(summary);
    setRunning(false, summary);
    stream.close(); stream = null;
    loadHistory();
    if (data.worksheet) loadSheet(data.worksheet, true);
  });
  stream.onerror = () => { if (stream) { stream.close(); stream = null; } };
}

async function startRun(task, options) {
  banner($('run-error'), '', '');
  $('console').innerHTML = '';
  try {
    const summary = await api('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task, options }),
    });
    describeRun(summary);
    setRunning(true, summary);
    attach(summary.id);
    loadHistory();
  } catch (err) {
    banner($('run-error'), 'err', err.message);
  }
}

$('btn-stop').addEventListener('click', async () => {
  if (!currentRunId) return;
  try { await api(`/api/run/${currentRunId}/stop`, { method: 'POST' }); }
  catch (err) { banner($('run-error'), 'err', err.message); }
});
$('btn-clear-log').addEventListener('click', () => {
  $('console').innerHTML = '<span class="empty">Cleared.</span>';
});

async function loadHistory() {
  const { current, history } = await api('/api/runs');
  const body = $('history-body');
  body.innerHTML = '';
  if (!history.length) {
    const tr = el('tr'); const td = el('td', 'muted', 'No runs yet.');
    td.colSpan = 6; tr.append(td); body.append(tr);
  }
  history.forEach((s) => {
    const tr = el('tr');
    const cls = { done: 'ok', failed: 'err', stopped: 'warn' }[s.status] || 'warn';
    tr.append(
      el('td', 'faint', s.started_at.replace('T', ' ')),
      el('td', '', s.label),
    );
    const st = el('td'); st.append(el('span', `pill ${cls}`, s.status)); tr.append(st);
    tr.append(el('td', 'faint', `${s.duration_seconds}s`),
              el('td', 'faint', s.line_count),
              el('td', 'faint mono', s.command));
    body.append(tr);
  });
  if (current) {
    describeRun(current);
    setRunning(true, current);
    if (current.id !== currentRunId) attach(current.id);
  }
}

/* ── Settings panel ────────────────────────────────────────────────────── */

let settingsSnapshot = {};
const pending = {};

function markDirty() {
  const n = Object.keys(pending).length;
  $('btn-save').disabled = n === 0;
  $('btn-revert').disabled = n === 0;
  $('save-status').textContent = n ? `${n} unsaved change${n > 1 ? 's' : ''}` : '';
}

function track(path, value, fieldNode) {
  const same = JSON.stringify(value) === JSON.stringify(settingsSnapshot[path]);
  if (same) delete pending[path]; else pending[path] = value;
  fieldNode.classList.toggle('changed', !same);
  markDirty();
}

function buildField(field) {
  const node = el('div', 'field');
  const lbl = el('div', 'lbl');
  lbl.append(document.createTextNode(field.label));
  if (field.danger) lbl.append(el('span', 'flag', '⚠ affects production data'));
  if (field.help) lbl.append(el('div', 'help', field.help));
  node.append(lbl);

  const ctrl = el('div', 'ctrl');
  settingsSnapshot[field.path] = field.value;

  if (field.type === 'bool') {
    const wrap = el('label', 'switch');
    const box = el('input'); box.type = 'checkbox'; box.checked = !!field.value;
    box.addEventListener('change', () => track(field.path, box.checked, node));
    wrap.append(box, el('span', 'faint', box.checked ? '' : ''));
    ctrl.append(wrap);
  } else if (field.type === 'select') {
    const sel = el('select');
    field.options.forEach((o) => sel.append(new Option(o, o)));
    sel.value = field.value;
    sel.addEventListener('change', () => track(field.path, sel.value, node));
    ctrl.append(sel);
  } else if (field.type === 'multiselect') {
    const box = el('div', 'checks');
    const chosen = new Set(field.value || []);
    field.options.forEach((o) => {
      const blocked = /blocked/i.test(o.note || '');
      const lab = el('label', (chosen.has(o.value) ? 'on ' : '') + (blocked ? 'blocked' : ''));
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = chosen.has(o.value);
      cb.addEventListener('change', () => {
        cb.checked ? chosen.add(o.value) : chosen.delete(o.value);
        lab.classList.toggle('on', cb.checked);
        track(field.path, field.options.filter((x) => chosen.has(x.value)).map((x) => x.value), node);
      });
      lab.append(cb, document.createTextNode(o.label));
      if (o.note) lab.append(el('span', 'note', `· ${o.note}`));
      box.append(lab);
    });
    ctrl.append(box);
  } else {
    const input = el('input');
    input.type = field.type === 'text' ? 'text' : 'number';
    if (field.type === 'float') input.step = '0.1';
    if (field.min !== undefined) input.min = field.min;
    if (field.max !== undefined) input.max = field.max;
    input.value = field.value ?? '';
    input.addEventListener('input', () => {
      const raw = input.value;
      track(field.path, field.type === 'text' ? raw : Number(raw), node);
    });
    ctrl.append(input);
  }
  node.append(ctrl);
  return node;
}

async function loadSettings() {
  banner($('settings-error'), '', '');
  const payload = await api('/api/settings');
  $('config-path').textContent = payload.path;
  const host = $('settings-groups');
  host.innerHTML = '';
  settingsSnapshot = {};
  for (const key of Object.keys(pending)) delete pending[key];
  payload.groups.forEach((group) => {
    const box = el('div', 'setting-group');
    box.append(el('h3', '', group.group));
    if (group.help) box.append(el('p', 'group-help', group.help));
    group.fields.forEach((f) => box.append(buildField(f)));
    host.append(box);
  });
  markDirty();
}

$('btn-save').addEventListener('click', async () => {
  $('btn-save').disabled = true;
  try {
    const res = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: pending }),
    });
    const n = Object.keys(res.changed).length;
    await loadSettings();
    banner($('settings-error'), 'ok',
      `Saved ${n} setting${n === 1 ? '' : 's'} to config.yaml${res.backup ? ` (previous version kept at ${res.backup})` : ''}.`);
    await refreshTargets();
  } catch (err) {
    banner($('settings-error'), 'err', err.message);
    markDirty();
  }
});
$('btn-revert').addEventListener('click', loadSettings);

/* ── Boot ──────────────────────────────────────────────────────────────── */

async function refreshTargets() {
  const t = await api('/api/targets');
  const sel = $('sheet-select');
  const previous = sel.value;
  sel.innerHTML = '';
  [
    [t.jobs, `Jobs — ${t.jobs}`],
    [t.companies, `Companies (enriched) — ${t.companies}`],
    [t.company_source, `Company source — ${t.company_source}`],
    [t.keywords, `Keywords — ${t.keywords}`],
  ].forEach(([v, label]) => sel.append(new Option(label, v)));
  $('sheet-link').href = `https://docs.google.com/spreadsheets/d/${t.spreadsheet_id}`;
  const next = [...sel.options].some((o) => o.value === previous) ? previous : t.jobs;
  sel.value = next;
  return next;
}

(async function boot() {
  try {
    const first = await refreshTargets();
    const { tasks } = await api('/api/tasks');
    renderTasks(tasks);
    await Promise.all([loadSheet(first, false), loadHistory(), loadSettings()]);
  } catch (err) {
    banner($('data-error'), 'err', `Startup failed: ${err.message}`);
  }
})();
