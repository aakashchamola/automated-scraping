import unittest

from scrapers import career_page


SAMPLE_HTML = """
<html><body>
  <nav>
    <a href="/about">About Us</a>
    <a href="/contact">Contact</a>
    <a href="/careers">Careers</a>
  </nav>
  <ul class="openings">
    <li><a href="/jobs/123-backend-engineer">Backend Engineer</a></li>
    <li><a href="/jobs/124-frontend-engineer">Frontend Engineer (Remote)</a></li>
    <li><a href="https://boards.greenhouse.io/acme/jobs/999">Senior Data Scientist</a></li>
    <li><a href="/jobs/123-backend-engineer">Backend Engineer</a></li>  <!-- dup -->
    <li><a href="/jobs/125-apply">Apply</a></li>            <!-- junk title -->
    <li><a href="/life-at-company">Life at Stripe</a></li>  <!-- no listing id -->
    <li><a href="/jobs">See all jobs</a></li>              <!-- no listing id -->
  </ul>
</body></html>
"""


class ExtractJobsTest(unittest.TestCase):
    def setUp(self):
        self.jobs = career_page.extract_jobs_from_html(
            SAMPLE_HTML, base_url="https://acme.com/careers"
        )

    def test_extracts_real_titles_only(self):
        titles = {j["title"] for j in self.jobs}
        self.assertIn("Backend Engineer", titles)
        self.assertIn("Frontend Engineer (Remote)", titles)
        self.assertIn("Senior Data Scientist", titles)

    def test_filters_junk_and_non_listing_links(self):
        titles = {j["title"] for j in self.jobs}
        self.assertNotIn("Apply", titles)          # junk title
        self.assertNotIn("Life at Stripe", titles)  # no listing id in URL
        self.assertNotIn("See all jobs", titles)    # no listing id in URL
        self.assertNotIn("About Us", titles)

    def test_deduplicates_urls(self):
        urls = [j["url"] for j in self.jobs]
        self.assertEqual(len(urls), len(set(urls)))

    def test_resolves_relative_urls_to_absolute(self):
        backend = next(j for j in self.jobs if j["title"] == "Backend Engineer")
        self.assertEqual(backend["url"], "https://acme.com/jobs/123-backend-engineer")


class ListingHrefTest(unittest.TestCase):
    def test_listing_ids_match(self):
        self.assertTrue(career_page._is_listing_href("/jobs/123"))
        self.assertTrue(career_page._is_listing_href("/job/45678"))
        self.assertTrue(career_page._is_listing_href("/x?gh_jid=987"))
        self.assertTrue(career_page._is_listing_href("/listing/account-exec/75974"))
        self.assertTrue(
            career_page._is_listing_href("/o/3f2c1a9b-1234-4abc-9def-0123456789ab")
        )

    def test_non_listing_rejected(self):
        self.assertFalse(career_page._is_listing_href("/jobs"))
        self.assertFalse(career_page._is_listing_href("/about-us"))
        self.assertFalse(career_page._is_listing_href("/life-at-company"))

    def test_is_real_title(self):
        self.assertTrue(career_page._is_real_title("Backend Engineer"))
        self.assertFalse(career_page._is_real_title("apply now"))
        self.assertFalse(career_page._is_real_title("  "))
        self.assertFalse(career_page._is_real_title("(3)"))


class DetectAtsTest(unittest.TestCase):
    def test_detect_greenhouse_embed(self):
        html = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=gitlab"></script>'
        self.assertEqual(career_page.detect_ats(html), ("greenhouse", "gitlab"))

    def test_detect_greenhouse_new_host(self):
        html = '<a href="https://job-boards.greenhouse.io/acmecorp">Jobs</a>'
        self.assertEqual(career_page.detect_ats(html), ("greenhouse", "acmecorp"))

    def test_detect_lever(self):
        self.assertEqual(
            career_page.detect_ats("", "https://jobs.lever.co/netflix"),
            ("lever", "netflix"),
        )

    def test_detect_ashby(self):
        html = '<iframe src="https://jobs.ashbyhq.com/ramp"></iframe>'
        self.assertEqual(career_page.detect_ats(html), ("ashby", "ramp"))

    def test_no_ats(self):
        self.assertEqual(career_page.detect_ats("<html>nothing</html>", "https://x.com"), (None, None))


class AtsParserTest(unittest.TestCase):
    def test_parse_greenhouse(self):
        payload = {"jobs": [
            {"title": "Backend Engineer", "absolute_url": "https://gh/jobs/1",
             "location": {"name": "Remote"}},
            {"title": "", "absolute_url": "https://gh/jobs/2"},  # dropped (no title)
        ]}
        jobs = career_page.parse_greenhouse(payload, "Acme", "career_page")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["Company"], "Acme")
        self.assertEqual(jobs[0]["Role"], "Backend Engineer")
        self.assertEqual(jobs[0]["Location"], "Remote")
        self.assertEqual(jobs[0]["Platform"], "career_page")
        self.assertEqual(jobs[0]["Job Link"], "https://gh/jobs/1")

    def test_parse_lever(self):
        payload = [
            {"text": "Designer", "hostedUrl": "https://lever/o/abc",
             "categories": {"location": "NYC"}},
        ]
        jobs = career_page.parse_lever(payload, "Beta", "career_page")
        self.assertEqual(jobs[0]["Role"], "Designer")
        self.assertEqual(jobs[0]["Location"], "NYC")
        self.assertEqual(jobs[0]["Job Link"], "https://lever/o/abc")

    def test_parse_ashby(self):
        payload = {"jobs": [
            {"title": "Security Engineer", "jobUrl": "https://ashby/x", "location": "SF"},
        ]}
        jobs = career_page.parse_ashby(payload, "Gamma", "career_page")
        self.assertEqual(jobs[0]["Role"], "Security Engineer")
        self.assertEqual(jobs[0]["Job Link"], "https://ashby/x")


class KeywordMatchTest(unittest.TestCase):
    KEYWORDS = [
        "Microbiologist",
        "Research Assistant Biology",
        "Clinical Research Associate",
        "Bioinformatics Analyst",
        "Biotech Intern",
    ]

    def test_single_token_keyword(self):
        self.assertEqual(
            career_page.match_keyword("Senior Microbiologist II", self.KEYWORDS),
            "Microbiologist",
        )

    def test_all_tokens_must_be_present(self):
        self.assertEqual(
            career_page.match_keyword("Research Assistant - Molecular Biology", self.KEYWORDS),
            "Research Assistant Biology",
        )
        # Missing "biology" -> should NOT match that keyword
        self.assertEqual(
            career_page.match_keyword("Research Assistant, Chemistry", self.KEYWORDS),
            "",
        )

    def test_unrelated_title_no_match(self):
        self.assertEqual(career_page.match_keyword("Senior Software Engineer", self.KEYWORDS), "")

    def test_clinical_research_associate_variants(self):
        self.assertEqual(
            career_page.match_keyword("Clinical Research Associate II", self.KEYWORDS),
            "Clinical Research Associate",
        )

    def test_empty_keywords_returns_empty(self):
        self.assertEqual(career_page.match_keyword("Microbiologist", []), "")


class NormalizeCareerUrlTest(unittest.TestCase):
    def test_bare_domain_gets_https(self):
        self.assertEqual(
            career_page.normalize_career_url("careers.veranahealth.com"),
            "https://careers.veranahealth.com",
        )

    def test_domain_with_path(self):
        self.assertEqual(
            career_page.normalize_career_url("astrazeneca.com/careers"),
            "https://astrazeneca.com/careers",
        )

    def test_keeps_full_url(self):
        self.assertEqual(
            career_page.normalize_career_url("https://x.com/careers"),
            "https://x.com/careers",
        )

    def test_rejects_na_text(self):
        self.assertEqual(career_page.normalize_career_url("N/A (no public career page)"), "")
        self.assertEqual(career_page.normalize_career_url(""), "")


class FilterByKeywordsTest(unittest.TestCase):
    def test_filters_and_tags(self):
        raw = [
            {"Company": "X", "Role": "Microbiologist", "Keyword": "career_page", "Job Link": "u1"},
            {"Company": "X", "Role": "Sales Manager", "Keyword": "career_page", "Job Link": "u2"},
        ]
        kept = career_page._filter_by_keywords(raw, ["Microbiologist"])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["Role"], "Microbiologist")
        self.assertEqual(kept[0]["Keyword"], "Microbiologist")  # re-tagged

    def test_no_keywords_keeps_all(self):
        raw = [{"Company": "X", "Role": "Anything", "Keyword": "career_page", "Job Link": "u"}]
        self.assertEqual(career_page._filter_by_keywords(raw, []), raw)


class ScrapeCompaniesInputTest(unittest.TestCase):
    def test_skips_rows_without_usable_url(self):
        companies = [
            {"Company": "NoUrl Inc", "Career-Page": ""},
            {"Company": "NA Co", "Career-Page": "N/A (no public career page)"},
            {"Company": "", "Career-Page": "https://x.com"},
        ]
        self.assertEqual(
            career_page.scrape_companies(companies, keywords=["x"], delay_sec=0), []
        )


if __name__ == "__main__":
    unittest.main()
