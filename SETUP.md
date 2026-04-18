# Setup Guide

Step-by-step instructions to get this project running from a fresh clone.

---

## Prerequisites

- Python 3.10 or later
- `git`
- A terminal (macOS Terminal, zsh, bash)
- A Google account (only needed for Google Sheets sync)

---

## Step 1: Clone the Repository

```bash
git clone <your-repo-url>
cd automated-scraping
```

---

## Step 2: Create a Virtual Environment

```bash
python3 -m venv .venv
```

This creates a `.venv/` folder inside the project. All packages install here, isolated from your system Python.

---

## Step 3: Activate the Virtual Environment

```bash
source .venv/bin/activate
```

Your prompt will change to show `(.venv)`. You must do this every time you open a new terminal, or set up auto-activation (see below).

> **macOS with Homebrew Python:** always use the `.venv` flow. Never install packages with the system `pip`.

---

## Step 4: Install Dependencies

```bash
python -m pip install -r requirements.txt
```

This installs all required packages including scraping libraries and Google Sheets integration.

---

## Step 5: Configure Keywords

Open `keywords.txt` and add one search term per line:

```
Bioinformatics Analyst
Research Assistant Biology
Clinical Research Associate
```

These are the terms the pipeline will search across all enabled platforms.

---

## Step 6: Configure Platforms and Settings

Open `config.json`. The most common things to change:

**Choose which platforms to run:**
```json
"platforms": ["linkedin", "indeed", "internshala", "simplyhired"]
```

Available platforms: `linkedin`, `indeed`, `glassdoor`, `jobs.lever`, `internshala`, `wellfound`, `ycombinator`, `simplyhired`

**Set location:**
```json
"platform_settings": {
  "indeed": {
    "country": "us",
    "location": "United States"
  },
  "linkedin": {
    "location": "United States"
  }
}
```

For Indeed, `country` accepts: `us`, `in`, `uk`, `ca`, `au`

**Control pages per keyword:**
```json
"max_pages": 2
```

Higher values return more results but take longer and increase rate-limit risk.

---

## Step 7: Run the Pipeline

```bash
python main.py
```

Or with an explicit config:

```bash
python main.py --config config.json
```

**Output:**
- `jobs.csv` — all scraped jobs, deduplicated
- `logs/run_<timestamp>.log` — full run log with per-platform counts

---

## Step 8: Quick Health Check (Optional)

Run a one-keyword test across all platforms to see which are currently returning results:

```bash
python smoke_test.py
```

---

## Step 9: Set Up Google Sheets Sync (Optional)

Skip this section if you only want local CSV output.

### 9a. Create a Google Cloud Project

1. Go to [https://console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown (top-left) → **New Project**
3. Name it anything (e.g. `automation-scripting`) and click **Create**

### 9b. Enable Required APIs

1. In the left sidebar go to **APIs & Services → Library**
2. Search **Google Sheets API** → click it → click **Enable**
3. Search **Google Drive API** → click it → click **Enable**

### 9c. Create a Service Account

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → Service Account**
3. Give it a name (e.g. `automation-scripting`) → click **Done**
4. Click on the service account row that was just created
5. Go to the **Keys** tab
6. Click **Add Key → Create new key → JSON**
7. A `.json` file downloads — this is your credentials file

### 9d. Place the Credentials File in the Project

Create the `secrets/` folder:

```bash
mkdir -p secrets
```

Move the downloaded JSON file into it:

```bash
mv ~/Downloads/your-project-*.json secrets/google-service-account.json
```

> `secrets/` is git-ignored. Never commit this file.

### 9e. Share Your Google Sheet With the Service Account

1. Open the JSON file you just saved — find the `client_email` field:
   ```
   "client_email": "automation-scripting@your-project.iam.gserviceaccount.com"
   ```
   Copy that email address.

2. Open your target Google Sheet in the browser

3. Click **Share** (top-right corner)

4. Paste the service account email into the "Add people and groups" field

5. Set the role to **Editor**

6. Uncheck **Notify people** (service accounts have no inbox)

7. Click **Share**

> If you skip this step, the pipeline will fail with a `403` permission error.

### 9f. Update `config.json`

```json
"google_sheets": {
  "enabled": true,
  "credentials_file": "secrets/google-service-account.json",
  "spreadsheet_id": "YOUR_SHEET_ID",
  "worksheet": "Jobs"
}
```

**How to find `spreadsheet_id`:**

Look at your Google Sheet's URL:
```
https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
```
Copy the string between `/d/` and `/edit`.

**`worksheet`** is the tab name at the bottom of the spreadsheet. Use an existing tab name or any name — if the tab doesn't exist, it will be created automatically.

### 9g. Run

```bash
python main.py
```

On the first run, the header row is written automatically. Each subsequent run appends only new rows — nothing is ever duplicated.

---

## Auto-Activate the Virtual Environment (Optional)

To skip manually running `source .venv/bin/activate` every time, add this to your `~/.zshrc`:

```zsh
auto_venv() {
  if [[ -f .venv/bin/activate ]]; then
    if [[ "$VIRTUAL_ENV" != "$PWD/.venv" || "$PATH" != "$PWD/.venv/bin:"* ]]; then
      source .venv/bin/activate
      rehash
    fi
  elif [[ -n "$VIRTUAL_ENV" ]]; then
    deactivate
  fi
}
add-zsh-hook chpwd auto_venv
auto_venv
```

Then reload:

```bash
source ~/.zshrc
```

Now `cd`-ing into the project folder activates the venv automatically.

---

## Day-to-Day Usage

```bash
# cd into project (auto-activates venv if hook is set up)
cd automated-scraping

# Edit keywords if needed
nano keywords.txt

# Run
python main.py

# Check output
open jobs.csv          # macOS: opens in default app
cat logs/run_*.log     # view latest log
```

---

## Troubleshooting

| Problem | Likely Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` | venv not active | `source .venv/bin/activate` |
| `Config file not found` | Wrong working directory | `cd` into project root first |
| `403` from Google Sheets | Sheet not shared with service account | Follow Step 9e |
| Platform returns 0 results | Anti-bot protection or markup changed | Try again later or reduce platforms |
| `python` still points to system | venv not activated | Run `source .venv/bin/activate` or set up auto-activate hook |
