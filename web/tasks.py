"""
web/tasks.py — The automations the dashboard can launch.

Every entry is exactly the command a developer would type. The dashboard adds
no logic of its own: it builds this argv, runs it, and streams the output. That
keeps one code path for "ran from the terminal" and "ran from the browser", so
a run cannot behave differently depending on who started it.

``options`` are the per-run switches drawn as controls next to the Run button —
the things you change *for this run* (a --limit for a quick trial, --dry-run to
rehearse). Anything you set once and leave alone lives in Settings instead.
"""

TASKS = [
    {
        "key": "pipeline",
        "label": "Full pipeline",
        "script": "automation_pipeline.py",
        "blurb": "Enrich companies → scrape career pages → scrape job boards → validate links.",
        "detail": "The whole recurring job. Every step honours the Settings tab; "
                  "use the skip switches to re-run just part of it.",
        "primary": True,
        "options": [
            {"flag": "--skip-enrichment", "type": "bool", "label": "Skip company enrichment"},
            {"flag": "--skip-career-scraping", "type": "bool", "label": "Skip career-page scraping"},
            {"flag": "--skip-keyword-scraping", "type": "bool", "label": "Skip job-board scraping"},
            {"flag": "--skip-validation", "type": "bool", "label": "Skip job validation"},
        ],
    },
    {
        "key": "scrape",
        "label": "Job-board scraping",
        "script": "main.py",
        "blurb": "Search LinkedIn / Indeed / Internshala for every keyword and append new jobs.",
        "detail": "Duplicates are dropped against what is already in the sheet, so "
                  "running it twice does not double up rows.",
        "options": [],
    },
    {
        "key": "career",
        "label": "Career-page scraping",
        "script": "automation_pipeline.py",
        "fixed_args": ["--skip-enrichment", "--skip-keyword-scraping", "--skip-validation"],
        "blurb": "Scrape jobs straight from company career pages, using the company list as input.",
        "detail": "Detects the company's ATS (Greenhouse / Lever / Ashby) and reads its "
                  "public API where possible, so results are real postings rather than nav links.",
        "options": [],
    },
    {
        "key": "enrich",
        "label": "Company enrichment",
        "script": "company_enricher.py",
        "blurb": "Fill in employee count, career page and LinkedIn URL for each company.",
        "options": [],
    },
    {
        "key": "validate",
        "label": "Job validation",
        "script": "job_validator.py",
        "blurb": "Probe every job link and mark it Active / Expired / Removed / Unknown.",
        "detail": "Expired and Removed rows turn red in the sheet; Unknown (a network "
                  "blip) stays yellow and is retried on the next run.",
        "primary": True,
        "options": [
            {"flag": "--limit", "type": "int", "label": "Only check the first N jobs",
             "placeholder": "0 = all", "default": 0},
        ],
    },
    {
        "key": "mismatch",
        "label": "Data mismatch flagging",
        "script": "company_validator.py",
        "blurb": "Compare the hand-curated Company sheet against what the automation scraped, "
                 "and flag the cells that genuinely disagree.",
        "detail": "Formatting differences (104,832 vs 104832, www. prefixes, trailing "
                  "slashes) are normalised away first, so a flag means a real conflict.",
        "options": [],
    },
    {
        "key": "classify",
        "label": "Organisation classification",
        "script": "company_classifier.py",
        "blurb": "Sort every organisation into Company / University / Government / "
                 "Hospital / Nonprofit / Research.",
        "detail": "Rule-based and explainable — each verdict comes from the domain TLD, "
                  "the name, or the Industry column, in that order.",
        "options": [
            {"flag": "--dry-run", "type": "bool", "label": "Rehearse only (do not write to the sheet)"},
        ],
    },
    {
        "key": "pagination",
        "label": "Pagination analysis",
        "script": "pagination_analyzer.py",
        "blurb": "Measure how many jobs sit behind 'See More Jobs' / infinite scroll, per keyword.",
        "detail": "Read-only — it never writes to the Jobs tab. This is the tool that "
                  "found the scraper was skipping ~60% of available jobs.",
        "options": [
            {"flag": "--keywords", "type": "text", "label": "Specific keywords (comma separated)",
             "placeholder": "blank = all keywords"},
            {"flag": "--limit", "type": "int", "label": "Only the first N keywords",
             "placeholder": "0 = use config", "default": 0},
            {"flag": "--max-pages", "type": "int", "label": "Max pages to probe",
             "placeholder": "0 = use config", "default": 0},
        ],
    },
]

TASKS_BY_KEY = {t["key"]: t for t in TASKS}


def build_command(python_exe: str, task: dict, options: dict) -> list:
    """argv for a task plus the per-run switches the browser selected."""
    command = [python_exe, "-u", task["script"], "--config", "config.yaml"]
    command += task.get("fixed_args", [])

    for spec in task.get("options", []):
        raw = options.get(spec["flag"])
        if raw is None:
            continue
        if spec["type"] == "bool":
            if raw:
                command.append(spec["flag"])
        elif spec["type"] == "int":
            value = int(raw)
            if value:                        # 0 means "use the config default"
                command += [spec["flag"], str(value)]
        else:
            value = str(raw).strip()
            if value:
                command += [spec["flag"], value]
    return command
