import unittest

from enricher import normalizers


class NormalizersTest(unittest.TestCase):
    def test_normalize_linkedin_url_strips_query_and_forces_trailing_slash(self):
        self.assertEqual(
            normalizers.normalize_linkedin_url(
                "https://www.linkedin.com/company/example-company/?trk=public_profile"
            ),
            "https://www.linkedin.com/company/example-company/",
        )

    def test_normalize_career_page_adds_https_for_bare_domain(self):
        self.assertEqual(
            normalizers.normalize_career_page("jobs.example.com/careers"),
            "https://jobs.example.com/careers",
        )

    def test_is_numeric_employee_count_accepts_suffix_and_range_formats(self):
        self.assertTrue(normalizers.is_numeric_employee_count("500k"))
        self.assertTrue(normalizers.is_numeric_employee_count("500-600"))
        self.assertTrue(normalizers.is_numeric_employee_count("10k - 15k"))

    def test_normalize_source_employee_count_preserves_supported_formats(self):
        self.assertEqual(normalizers.normalize_source_employee_count("500k"), "500k")
        self.assertEqual(normalizers.normalize_source_employee_count("500-600"), "500-600")

    def test_normalize_source_employee_count_extracts_numeric_text(self):
        self.assertEqual(normalizers.normalize_source_employee_count("About 79,222 employees"), "79222")

    def test_normalize_source_employee_count_drops_na_values(self):
        self.assertEqual(normalizers.normalize_source_employee_count("NA"), "")


if __name__ == "__main__":
    unittest.main()