# Deploying the dashboard

The published dashboard is a **static page on GitHub Pages** behind a password.
Nothing runs on your machine, and the Google service-account key never leaves
GitHub Actions.

```
  GitHub Actions  ──►  runs the pipeline        (holds the GCP key)
        │              exports the sheet tabs
        │              encrypts them with your password
        ▼
  GitHub Pages    ──►  static page + ciphertext (holds no secrets)
        │
        ▼
  Browser         ──►  password decrypts the data locally
```

## Setup — two secrets and one toggle

```bash
# 1. the Google key the pipeline needs
gh secret set GOOGLE_SERVICE_ACCOUNT < secrets/google-service-account.json

# 2. the password people will type to open the dashboard (min 8 characters)
gh secret set DASHBOARD_PASSWORD
```

**3.** Settings → Pages → **Source: GitHub Actions**.

Then run *Scheduled scrape* once from the Actions tab. When it finishes, the
page is live at `https://<owner>.github.io/<repo>/`.

Share the URL and the password. That is the whole handover — no install, no
Python, no keys on anyone's laptop.

## Why the password is real security here

GitHub Pages is world-readable, so a password compared in JavaScript would
protect nothing: the data would sit next to the check, and anyone could fetch
the JSON directly.

Instead the password **is the decryption key**. Every published file is
AES-256-GCM ciphertext under a key derived with PBKDF2-SHA256 (200,000
iterations, a fresh random salt and IV per file). Without the password the
files are unreadable rather than merely hidden, and a wrong password fails as a
decryption error, not a comparison someone can step over in devtools.

The browser does this with built-in WebCrypto, so the page loads no crypto
library. Decrypting 543 rows takes about 50 ms.

What this does and does not give you:

- **Does:** anyone without the password sees nothing, even reading the raw
  files. The GCP key is never on Pages, never in the repo, never in a browser.
- **Does not:** it is one shared password, so it cannot tell users apart or be
  revoked per person. Rotate it by updating the `DASHBOARD_PASSWORD` secret and
  re-running the workflow. Anyone who has both the password and an old copy of
  the files can still read that old copy — rotation protects future
  publications, not past ones.

Two guards back this up: the publish job **refuses to deploy** if any cleartext
JSON is left in `site/data`, and it deletes the service-account key from the
runner before the upload step.

## Pressing Run

The Run tab lists the automations, and each button opens GitHub's own **Run
workflow** dialog. It works this way deliberately: starting a run needs write
access to the repository, and a static page cannot hold a token with write
access without publishing it to everyone. Handing over to GitHub means the
viewer's own GitHub session authorises the run, and no credential is ever
embedded in the page.

Run history is read straight from the public repository's API with no token at
all, so status and logs appear without anyone signing in.

If you want a real in-page Run button later, the missing piece is a tiny
credential broker — a free Cloudflare Worker holding a fine-grained token
scoped to `workflow: write`, which checks the password server-side before
dispatching. That is the only way to keep the token off the page.

## What runs, and what a datacenter IP cannot reach

Measured from a runner on 2026-08-29 (`probe-datacenter-ip.yml`, egress
`20.109.38.164`):

| Source | From a runner |
|---|---|
| LinkedIn guest API | works — 200, 10 items |
| Greenhouse ATS API | works — 207 jobs |
| Workday ATS API | works — 20 postings |
| Indeed | **blocked** — Cloudflare 403 |
| Internshala | **blocked** — 200 with a challenge page body |

LinkedIn carries about 92% of the jobs and career-page scraping is unaffected,
so scheduled runs are worth having. The workflow drops Indeed and Internshala
at runtime rather than spending the run collecting nothing and logging
misleading "0 jobs found" lines.

**Both still work from a home connection**, so run the full pipeline locally
now and then if those two matter. Re-run the probe workflow any time to check
whether that has changed.

## The local dashboard is a different thing

`./start_dashboard.sh` runs the full Flask app on `127.0.0.1:5000`: it can
*start* runs directly, edit `config.yaml`, and stream logs live. Keep it local —
it has no authentication and its Run tab executes commands on the host. The
published static page is the read-mostly, shareable view of the same data.

| | Local dashboard | Published page |
|---|---|---|
| Where it runs | your machine | GitHub Pages |
| Needs Python | yes | no |
| Starts runs | directly | via GitHub's dialog |
| Edits settings | yes | no |
| Live logs | yes | links to the run |
| Data freshness | live | last published run |
| Safe to share | no | yes, with the password |
