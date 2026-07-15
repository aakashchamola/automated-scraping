# Legal & Compliance Review — Job Scraping Automation

**Task #7** — Review whether the current scraping automation complies with platform
policies and legal requirements, and identify potential compliance risks.

> ⚠️ **Disclaimer.** This is an engineering-level risk assessment by the
> development team, **not legal advice**. Scraping law is unsettled and varies by
> jurisdiction. Before scaling this automation commercially, have the items in
> the *Action Items* section reviewed by qualified counsel.

_Last reviewed: 2026-06-14_

---

## 1. What the automation actually does

| Component | Data accessed | Method | Auth used? |
|-----------|---------------|--------|-----------|
| `scrapers/linkedin.py` | Public job postings | LinkedIn **guest** `seeMoreJobPostings` endpoint | No login / no token |
| `scrapers/indeed.py` | Public job search results | Public search HTML | No |
| `scrapers/internshala.py` | Public job/internship listings | Public search HTML | No |
| `scrapers/career_page.py` | Company career pages | Direct HTML/ATS fetch | No |
| `company_enricher.py` | Company metadata (employee count, career page, LinkedIn URL) | LinkedIn guest pages + website probing | No |
| `job_validator.py` | HTTP status of already-collected job URLs | HEAD/GET probe | No |
| `pagination_analyzer.py` | Counts of public postings (read-only) | LinkedIn guest endpoint | No |
| Google Sheets I/O | Our own spreadsheet | Service-account OAuth | Yes (our own data) |

**Key facts that lower risk:**
- Only **publicly accessible** data is read — no login, no paywall, no auth bypass.
- No personal credentials are used or stored for the scraped platforms.
- The data collected is **factual job-posting metadata** (title, company, location,
  link) — facts are not copyrightable.
- Requests are **rate-limited and retried politely** (see `http` config and
  per-platform `page_delay_seconds`).

**Key facts that raise risk:**
- LinkedIn's and Indeed's Terms of Service **prohibit automated access**, regardless
  of whether the data is public. ToS breach is a *contract* matter even where it is
  not a *computer-crime* (CFAA) matter.
- The `People` tab and `company_enricher` employee data may contain **personal data**
  (names, profiles) → triggers GDPR/CCPA obligations.

---

## 2. Platform-by-platform policy assessment

### LinkedIn — ⚠️ **High ToS risk, lower statutory risk**
- **Policy:** LinkedIn's User Agreement §8.2 explicitly forbids scraping, bots, and
  automated data collection.
- **Statutory:** In *hiQ Labs v. LinkedIn* (9th Cir., 2022) the court held that
  scraping **publicly available** profiles does **not** violate the Computer Fraud
  and Abuse Act (CFAA), because public data is not "access without authorization."
  *However*, the case ultimately **settled with hiQ enjoined** for breaching
  LinkedIn's ToS and for using fake accounts — i.e. the *contract* claim survived.
- **Our posture:** We use the **guest endpoint** (no account, no fake accounts, no
  auth circumvention), which is materially better than hiQ's conduct. CFAA exposure
  is low; **ToS/contract exposure remains** and LinkedIn may IP-block or send a
  cease-and-desist. Mitigation = low volume, caching, no account use.

### Indeed — ⚠️ **Medium**
- ToS prohibits scraping; Indeed also offers an official Publisher/Employer API.
- We read only public search HTML at low volume. Recommend migrating to the official
  API if/when volume grows.

### Internshala — ⚠️ **Medium**
- Public listings, no official API. Same low-volume posture as Indeed.

### Glassdoor / Wellfound / SimplyHired — ✅ **Not used**
- These are **disabled in config** (Cloudflare 403 from server IPs). No scraping
  occurs. Keeping them off is also the lower-risk choice.

### Y Combinator — ✅ **Not used** (JS-rendered, no static data).

### Company career pages — ✅ **Low**
- First-party public pages; lowest-risk source. Still respect `robots.txt` and
  per-site rate limits.

### Google Sheets — ✅ **No risk** (our own spreadsheet, authenticated with our own
service account).

---

## 3. Legal frameworks in scope

| Framework | Relevance | Current exposure |
|-----------|-----------|-----------------|
| **CFAA (US, 18 U.S.C. §1030)** | "Unauthorized access" to computers | **Low** — public data only, no auth bypass (*hiQ*, *Van Buren v. US* 2021 narrowed CFAA). |
| **Breach of contract (ToS)** | LinkedIn/Indeed ToS forbid bots | **Medium–High** — this is the primary realistic risk. |
| **Copyright / EU database rights** | Compilations of listings | **Low** — we collect *facts*, store our own compilation; do not copy whole pages or proprietary descriptions verbatim. |
| **GDPR (EU)** | Personal data of EU persons | **Medium** — only if `People`/employee data covers EU individuals; needs lawful basis, retention limits, subject-access handling. |
| **CCPA/CPRA (California)** | Personal data of CA residents | **Medium** — similar; honor deletion/opt-out for any personal data. |
| **`robots.txt` / trespass to chattels** | Crawl etiquette, server load | **Low** — low volume + delays; but we do not currently parse `robots.txt` (gap). |
| **Recent precedent** | *Meta v. Bright Data* (N.D. Cal. 2024) reaffirmed scraping **public, logged-out** data is generally permissible | Supports our logged-out posture. |

---

## 4. Safeguards already implemented in the codebase

These are real, code-level mitigations already in place:

1. **No authentication against scraped platforms** — guest/public endpoints only
   (`scrapers/linkedin.py`, `company_enricher.py`). Avoids the fake-account conduct
   that sank hiQ.
2. **Rate limiting & backoff** — `http.max_retries`, `http.retry_delay_seconds`,
   `http.delay_between_requests_seconds`, and per-platform `page_delay_seconds`
   (`scrapers/http_utils.py:build_session`).
3. **Blocked platforms stay off** — Glassdoor/Wellfound/SimplyHired/YC are commented
   out in `config.yaml` (no Cloudflare/anti-bot evasion is attempted).
4. **Bounded crawl depth** — `max_pages`, `max_probe_pages`, `max_jobs_per_company`
   cap total requests per run.
5. **Read-only diagnostics** — `pagination_analyzer.py` and `job_validator.py` never
   create new postings; they only measure/verify existing public data.
6. **Identifiable User-Agent** — a real browser UA string is sent (no spoofing of
   internal APIs beyond the publicly documented guest endpoints).

---

## 5. Gaps & risks identified

| # | Risk | Severity | Notes |
|---|------|----------|-------|
| R1 | LinkedIn/Indeed **ToS breach** | Medium–High | Inherent to scraping these sites at all. Mitigate with low volume + official APIs. |
| R2 | **No `robots.txt` check** before fetching career pages | Medium | Add a robots check to `career_page.py` / scrapers. |
| R3 | **Personal data** (People/employee enrichment) without documented lawful basis / retention policy | Medium | Define retention window, purpose, and deletion path (GDPR/CCPA). |
| R4 | **No per-domain throttle** on career-page scraping (only a fixed sleep) | Low | Add adaptive backoff on 429/503. |
| R5 | Storing scraped data in a shared Google Sheet | Low | Restrict sheet sharing; service-account key lives in `secrets/` (already git-ignored — verify). |
| R6 | No **terms acceptance / attribution** record for sources | Low | Keep this review + a data-source log. |

---

## 6. Action items (recommended)

**Now (low effort):**
- [ ] Keep scrape volume low and the per-page delays **on** (do not set delays to 0
      for LinkedIn/Indeed in production).
- [ ] Confirm `secrets/google-service-account.json` is git-ignored and the sheet is
      shared only with intended users. *(`.gitignore` present — verify it covers `secrets/`.)*
- [ ] Add a short **data-retention note** for any personal data in `People`.

**Soon (medium effort):**
- [ ] Add a `robots.txt` check to career-page and HTML scrapers (R2).
- [ ] Add adaptive backoff on HTTP 429/503 (R4).
- [ ] Migrate Indeed to its **official Publisher API** if volume increases (R1).

**Before any commercial scale-up:**
- [ ] Obtain **legal counsel review** of LinkedIn/Indeed ToS exposure and GDPR/CCPA
      obligations for any personal data.

---

## 7. Compliance-relevant config knobs

All in `config.yaml`:

```yaml
http:
  delay_between_requests_seconds: 0   # ↑ raise this in production for politeness
  max_retries: 3
  retry_delay_seconds: 1

scraping:
  platforms:                          # ← keep blocked platforms commented out
    - linkedin
    - indeed
    - internshala
  platform_settings:
    linkedin:
      max_pages: 12                   # ← bound crawl depth
      page_delay_seconds: 2.0         # ← politeness delay between pages

pagination_analysis:
  max_probe_pages: 50                 # read-only, but still bounded
  page_delay_seconds: 1.5
```

---

## 8. Bottom line

The automation's design is **defensible**: it reads only public, logged-out data,
uses no fake accounts or auth bypass, and rate-limits itself. The **main residual
risk is contractual** (LinkedIn/Indeed ToS), not criminal (CFAA), and is best
managed by keeping volume low, preferring official APIs where available, and adding
the `robots.txt`/personal-data safeguards listed above. None of the currently
*enabled* sources (LinkedIn guest, Indeed, Internshala, first-party career pages)
involves anti-bot circumvention, which is the conduct courts have penalized most.
