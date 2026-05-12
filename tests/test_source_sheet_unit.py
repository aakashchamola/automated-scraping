import unittest

from enricher import source_sheet


class SourceSheetHelpersTest(unittest.TestCase):
    def test_extract_linkedin_company_url_direct(self):
        raw = "https://www.linkedin.com/company/example-co/jobs/?originalSubdomain=in"
        self.assertEqual(
            source_sheet._extract_linkedin_company_url(raw),
            "https://www.linkedin.com/company/example-co/",
        )

    def test_extract_linkedin_company_url_nested_query(self):
        raw = (
            "https://jobs.example.com/redirect?target="
            "https%3A%2F%2Fwww.linkedin.com%2Fschool%2Fexample-university%2Fjobs%2F"
        )
        self.assertEqual(
            source_sheet._extract_linkedin_company_url(raw),
            "https://www.linkedin.com/school/example-university/",
        )

    def test_extract_linkedin_company_url_returns_empty_for_non_linkedin(self):
        raw = "https://jobs.example.com/company/example-co/careers"
        self.assertEqual(source_sheet._extract_linkedin_company_url(raw), "")


if __name__ == "__main__":
    unittest.main()
