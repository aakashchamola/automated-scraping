# Projects

The automation runs many projects. Each is one Google spreadsheet with its own
jobs, companies, keywords and settings, and its own password. One **control
spreadsheet** lists them all.

```
  Control spreadsheet ── Projects tab
    id      name                spreadsheet_id   status   data_key  pw_salt  pw_hash
    main    LinkedIn Reachout   1SEIHZ…          active   ·         ·        ·
    biotech Biotech Jobs        1AbC…            active   ·         ·        ·

  Each project's own spreadsheet
    Jobs · Company · Companies · Keywords · Settings
```

Nothing about a project lives in this repository. Adding one means **no commit,
no repository secret and no redeploy**.

## Creating one

From the dashboard: the project menu in the header → **New project…** → a name
and a password. The Apps Script does the rest — it creates the spreadsheet in
your Drive, gives it the tabs the automation expects, shares it with the service
account, and registers it.

It has to be the Apps Script rather than Python, because a service account on a
consumer Google account has **no Drive storage quota** and so cannot own a file
at all; creating one as the service account fails with *"The user's Drive
storage quota has been exceeded"*. The script runs as you, so the sheet is
created in your Drive and owned by you.

To adopt a spreadsheet you already have, put its id in **Use a spreadsheet I
already have**. Missing tabs are added; existing ones are left alone.

⚠️ **An adopted sheet must also be shared with the Google account the Apps
Script runs as**, as an Editor. Sharing it with the service account is not
enough — that only lets the *pipeline* reach it, not the Settings service, and
the two are different identities (see "Two identities" below). A sheet the
script cannot open fails with *"You do not have permission to access the
requested document"*, served by Google as an HTML page that the script cannot
catch and turn into a readable error. Sheets created through the dialog are
owned by that account already, so this only applies to ones you adopt.

## Running one

```bash
python automation_pipeline.py --project biotech
python job_validator.py       --project biotech
python publish_projects.py                        # every active project
python publish_projects.py    --project biotech   # just one
```

With no `--project`, the first active project in the control sheet is used, so
a single-project setup behaves exactly as it did before there were projects.
`PROJECT_ID` in the environment does the same thing, which is how the GitHub
Actions workflow passes its **Project** input down to every step.

## Copying and deleting

Both are done from the project menu, and both are authorised by the project you
have open — you can only copy or delete what your password already opens.

**Copy** duplicates the spreadsheet through Drive, so every tab, its formatting
and its columns come across exactly. The scraped results are then emptied, since
a new project arriving with thousands of somebody else's jobs in it would look
like its own findings; tick *Also copy the scraped results* to keep them. What
carries over is the expensive part: settings, keywords and the hand-maintained
company list. The copy gets its own id, password and data key, and a Drive copy
inherits the source's sharing, so everyone the copy did not earn is removed from
it.

**Delete** removes the project from the registry: no password opens it and no
run finds it. Three things are required together, each ruling out a different
accident — the project's name typed out, its current password (asked for again
even with a live session, so a browser left open on a shared machine cannot
destroy someone's work), and an explicit confirmation. The spreadsheet is left
untouched in Drive unless you tick the box, and even then it goes to the bin and
is recoverable for thirty days. Nothing here deletes anything permanently.

## Running it on your own machine

The **Run locally** tab hands out a one-line installer. It clones the
repository, builds a Python virtual environment, installs the dependencies and
checks they import — into `~/automated-scraping`, with no admin rights, and safe
to re-run to update.

Worth doing beyond saving a server: LinkedIn and the ATS APIs answer a
datacenter address fine, but Indeed and Internshala return a Cloudflare 403 and
a challenge page. A home connection gets both back, which is why CI drops them
and a local run does not.

### Runs happen on your machine, started from the website

The website cannot start a process on a laptop, and the laptop cannot accept an
incoming connection. So neither calls the other — both talk to the project's
sheet:

```
  dashboard ──── appends a row ────►  Runs tab  ◄──── polls every 15s ──── agent.py
     (a browser)                    (the queue)                       (your machine)
```

Press **Run** and a row is queued. `agent.py`, running on whichever machine
should do the work, claims it and reports back. Both ends only ever make
*outbound* requests, which is why this works from any network with no tunnel,
no port forward and no fixed address — and why closing the laptop simply means
queued runs wait rather than fail.

```bash
curl -fsSL https://raw.githubusercontent.com/aakashchamola/automated-scraping/automateV2/install.sh | bash
```

That is the whole setup. It asks for the project password at the end, checks it
against the service, saves it to `.env` beside the code with mode 0600, and
offers to start the agent. Nothing is exported, so nothing is lost when the
terminal closes. Afterwards, and at any time:

```bash
cd ~/automated-scraping && ./.venv/bin/python agent.py   # leave it running
```

Polling doubles as the heartbeat, so the dashboard says whether a machine is
listening — a run that quietly never happens is the failure worth designing
against. Run stays enabled when nobody is there; the row waits.

Two things that only matter when it goes wrong:

- **Cancel** cannot kill a process on somebody else's laptop, so the run is
  marked `cancelling` and the agent is told at its next progress report. It gets
  SIGINT, not SIGKILL, so the pipeline unwinds rather than dying halfway through
  writing to a spreadsheet.
- A machine that is closed mid-run leaves a row saying `running` that nothing
  will finish, blocking whatever is behind it. A run with no live agent and no
  progress for fifteen minutes is marked **lost** — never killed, never called
  successful.

A run also leaves your working copy alone: CI rewrites `config.yaml` on a
throwaway checkout, but this is a real repository, so the Settings overlay goes
to a temporary copy.

### On a timer

The weekly cadence is a **time-driven trigger in the Apps Script**, not a cron
on your machine — a laptop cron fires only if the machine happens to be awake,
and silently does not if it is not. Queued from the script, a run waits and then
happens.

Set it up once: Apps Script editor → Triggers → Add trigger → `scheduledRun`,
Time-driven, Week timer. Then each project opts in through its own Settings tab,
so that one trigger serves every project:

| setting | meaning |
|---|---|
| `schedule.enabled` | `true` to take part; absent means off |
| `schedule.mode` | `full`, `scrape-only`, `validate-only`, … (default `full`) |

### The dashboard shows the sheet, not a snapshot

Data used to come from encrypted files a CI job exported and committed, which
meant the page showed the last successful publish — indefinitely, with no sign
it had stopped being true. It now reads each tab from the Web App as you open
it. A published snapshot is still used as a fallback if the service is
unreachable, and the chip above the table says which you are looking at.

### What still touches GitHub

Only Pages, and only as static hosting: a push that changes `site/` redeploys
the page. That job runs no Python, handles no service-account key and exports no
data. The old pipeline job is kept as a manual fallback for when no machine is
available — it is the one thing that still needs the key, and it never runs on
its own.

### It needs a password, not a key

The machine running the pipeline needs **no Google credentials at all**:

```bash
# install.sh has already written both of these to .env; env_file reads it for
# every entry point, so this needs nothing in the shell.
#   SETTINGS_WEB_APP_URL=https://script.google.com/macros/s/…/exec
#   PROJECT_PASSWORD=the project password
python main.py --config config.yaml
```

The scraping itself never needed credentials — `scrapers/` contains no
reference to any. It needed three things: the keywords to search for, the
settings to obey, and somewhere to put what it finds. All three go through the
Apps Script, which does the reading and writing as the sheet's owner.

Every run mode goes through it, not only the scrape:

| mode | what it needs from the sheet |
|---|---|
| `scrape-only` | keywords, settings, the links already collected |
| `validate-only` | the jobs tab, and a status column written back |
| `enrich-only`, `classify-only`, `mismatch-only` | the Company tab, and columns written back |
| `career-pages-only` | the Company tab's career pages, and rows appended |
| `cleanup-rows` | the jobs tab, and rows deleted |

Four of these used to build the Sheets-API store directly, so they demanded
the key however the machine was set up — "run it on your own machine" was true
for the scrape alone. `tests/test_store_parity_unit.py` now fails the build if
any module does that again.

That matters because a service-account key **cannot be scoped to one project**.
It can read and write every spreadsheet it has ever been shared with, so
handing it to someone to run a scrape gives them everything. A password reaches
one project, and it can be changed from the dashboard.

| | The service account | A project password |
|---|---|---|
| reaches | every sheet shared with it | one project |
| revoked by | rotating the key everywhere | changing that password |
| safe to hand out | no | yes |

### What crosses the wire

The **spreadsheet id never leaves**, so the machine cannot open the sheet
directly even if it wanted to, and every request is confined to the one project
its password selects.

Beyond that, be clear-eyed about what a password holder can read. The scrape is
narrow by design: the jobs already collected come back as short **hashes**
rather than URLs — enough to skip a duplicate and nothing more. But the other
modes read whole tabs, because they cannot do their job otherwise: validation
exists to check that each job URL still resolves, so it has to be given the
URLs. Enrichment and classification read the Company tab for the same reason.

So a project password is not a read-restricted credential. It is full access to
**that one project**, which is what the dashboard already grants it — the same
password there edits Settings and Keywords. The isolation it buys is between
projects, not within one.

Results are deduplicated twice: by the caller against its snapshot, and again
by the script against the live sheet. Only the second can see rows another
machine added while the run was going, and a run can take an hour.

With `secrets/google-service-account.json` present the pipeline uses it
directly instead, so nothing about running this on the owner's own machine
changes. Colour is the one thing that does not cross either way: the Web App
has no formatting action, so a credential-free run logs that it skipped the
row shading and writes identical data.

Both stores expose the same fourteen methods, and a test compares them method
by method — parameter names, order and defaults included, since a store taking
`(values, col)` instead of `(col, values)` would write the wrong cells rather
than raise.

## Passwords and keys

Two secrets per project, and they are deliberately not the same thing:

| | What it is | Can it change? |
|---|---|---|
| `pw_hash` | what you type to open the project | **yes**, from the dashboard |
| `data_key` | encrypts that project's published files | **no** — never |

Changing a data key would strand every file already published, because the
browser could no longer decrypt them. Keeping the password separate is exactly
what makes the password changeable. Sign-in works like this:

1. the page sends the password to the Apps Script
2. the script finds which project it unlocks and returns that project's data key
3. the page derives an AES key from it and decrypts `data/<project>/…`

The password is what **selects** the project, so the landing page never lists
them — opening the URL discloses no project names, and a wrong password says
only "no project matched".

The hash is salted, iterated SHA-256 rather than PBKDF2, because Apps Script has
no PBKDF2 and the same hash has to be computable there. That is weaker against
offline cracking, and acceptable only because the hash lives in a private
spreadsheet whose reader can already read every project's data directly.

## Where the sheets live, and who can see them

Set `PROJECTS_FOLDER_ID` in Script Properties and every sheet created here is
moved into that Drive folder. `?ping=1` reports whether the folder is reachable,
and a create call reports which folder the sheet was filed into — a bad id used
to be logged where nobody would read it, leaving sheets loose in My Drive.

**Keep that folder to yourself.** A file inherits the sharing of the folder it
sits in, so a projects folder shared with everyone who creates projects would
let each of them open and edit every other project's spreadsheet — straight past
the password that is supposed to separate them.

Grant access per sheet instead. The new-project form takes the creator's Google
address and shares that one spreadsheet with them, so they can open it in Google
Sheets and see nothing else. It is optional; without it the sheet is reachable
only by you and the service account.

## Who may create projects

By default, anyone signed in to any project. Once you hand a project password to
someone else, set `ADMIN_PASSWORD` in the Apps Script's Script Properties — then
creating a project needs that password instead.

## Switching in the dashboard

The header shows the project you are in. Its menu lists **only projects already
unlocked on this device** — the page has never been told what else exists.
Unlocking another adds to that list rather than replacing it, so switching back
and forth never asks for a password again. Each session lasts 10 days and they
lapse independently.

## Setting it up

The Apps Script needs these Script Properties:

| Property | Required | What it is |
|---|---|---|
| `CONTROL_SHEET_ID` | yes | the registry spreadsheet's id |
| `SERVICE_ACCOUNT` | recommended | the service account's email, so new sheets are shared automatically |
| `PROJECTS_FOLDER_ID` | no | a Drive folder to file new project sheets into |
| `ADMIN_PASSWORD` | no | required to create projects, when set |

and `config.yaml` needs the control block:

```yaml
control:
  enabled: true
  spreadsheet_id: <the registry spreadsheet id>
  worksheet: Projects
```

`CONTROL_SPREADSHEET_ID` in the environment overrides it.

## Two identities

Two different accounts touch the sheets, and they are granted separately:

| Who | How it gets access | Used by |
|---|---|---|
| the service account | shared as Editor on each sheet — done for you when a project is created | the Python pipeline, publishing |
| the Apps Script's account | owns sheets it creates; must be shared into ones you adopt | Settings, Keywords, creating projects |

They are granted separately, and a sheet reachable by one is not automatically
reachable by the other. The usual symptom is a project whose data publishes
perfectly but whose Settings panel will not load: that is the pipeline
succeeding as the service account while the Settings service is locked out.
