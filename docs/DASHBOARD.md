# Control Panel

A local web dashboard over the same automation the command line runs. Three
things in one page:

| Tab | What it is for |
|---|---|
| **Data** | Browse and filter the Google Sheet tabs the automation fills, without opening Sheets. |
| **Run** | Launch any automation and watch its log stream live, line by line. |
| **Settings** | Every switch in `config.yaml` as a labelled control with an explanation. |

---

## Start it

```bash
./start_dashboard.sh          # creates the venv, installs deps, opens the browser
```

or, if the environment is already set up:

```bash
python -m web.app             # http://127.0.0.1:5000
```

`DASHBOARD_PORT` and `DASHBOARD_HOST` override the defaults; `DASHBOARD_OPEN=0`
stops it opening a browser tab.

> **It binds to localhost on purpose.** The service-account key under `secrets/`
> has read/write on the whole spreadsheet, and the Run tab executes commands.
> Do not expose this port to a network without putting authentication in front
> of it first.

---

## Data tab

- **Worksheet picker** — the four tabs named in `config.yaml`. Change the target
  tabs in Settings and this list follows, so it always shows what the automation
  is actually pointed at.
- **Search** — matches across every column at once.
- **Column filters** — generated automatically for any column with a small
  number of distinct values (Platform, Keyword, Job Status, Organization Type).
  A column of unique job links never becomes a filter.
- **Sort** — click any header; click again to reverse.
- **Status colouring** — `Active` green, `Expired`/`Removed` red, `Unknown`
  amber, organisation types blue, matching the colours written into the sheet.
- **Export CSV** — exports *what is currently filtered*, not the whole tab.

Reads are cached for 60 seconds to stay inside the Sheets API quota. **Refresh**
forces a re-read, and finishing a run refreshes automatically — a run's whole
purpose is to change the sheet.

---

## Run tab

Every card launches exactly the command a developer would type; the log below is
that process's real stdout. There is no second code path, so a run cannot behave
differently depending on whether a person or the browser started it.

| Card | Command it runs |
|---|---|
| Full pipeline | `automation_pipeline.py` |
| Job-board scraping | `main.py` |
| Career-page scraping | `automation_pipeline.py --skip-enrichment --skip-keyword-scraping --skip-validation` |
| Company enrichment | `company_enricher.py` |
| Job validation | `job_validator.py` |
| Data mismatch flagging | `company_validator.py` |
| Organisation classification | `company_classifier.py` |
| Pagination analysis | `pagination_analyzer.py` |

The controls on a card are **per-run** switches — a `--limit` for a quick trial,
`--dry-run` to rehearse. Anything you set once and leave alone belongs in
Settings instead.

**One run at a time.** Every tool writes to the same sheet tabs, so two
concurrent runs would interleave writes and corrupt each other's row colouring.
A second launch is refused rather than queued. Use **Stop run** to end one early
— it terminates the whole process group, not just the parent.

The log console colours errors red, warnings amber and completion green, follows
the tail while **Follow** is ticked, and keeps the last 4000 lines so a browser
that connects late still sees the whole run. **Recent runs** keeps the last 40,
with each one's exact command line.

---

## Settings tab

Each control maps to one key in `config.yaml`, with a plain-language explanation
and — where relevant — a **⚠ affects production data** flag. The groups:

- **Target sheets** — the test/production switch. This is the one to check
  before any run.
- **Search keywords** — read live from the Keywords tab, or from `keywords.txt`.
- **Job boards** — which platforms to search. The ones marked *blocked* sit
  behind Cloudflare or render jobs in JavaScript; enabling them yields zero
  rows, not more rows.
- **LinkedIn depth**, **Job validation**, **Career-page scraping**,
  **Organisation classification**, **Pagination analysis**, **Network & logging**.

Changed rows highlight, and the save bar counts unsaved edits. **Discard**
reloads from disk.

### Saves keep the file readable

`config.yaml` carries ~120 comments that document what each key does — that file
is the operator's manual. Saves go through round-trip YAML, so editing a value
in the browser leaves every comment, blank line and indentation choice exactly
as it was. An edit-and-revert produces a byte-identical file, and there is a
test that asserts it.

Each save also copies the previous version to `logs/config_backups/`, so a bad
edit from the browser is one file-copy away from being undone.

---

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_web_unit.py -q
```

16 offline tests, no network and no Google Sheets: every task points at a real
script and passes `--config config.yaml`; blank numeric boxes do not become
`--limit 0` overriding the config; the runner captures output, reports non-zero
exits, refuses a concurrent run and kills a stopped one; every schema path
resolves in the real `config.yaml`; and the round-trip save is byte-identical.

(Plugin autoload is disabled because a system-wide ROS pytest plugin on this
machine breaks collection.)
