import unittest

import company_validator as cv


class NormalizeTest(unittest.TestCase):
    def test_employee_count_strips_commas_and_suffix(self):
        # Same number, different formatting → equal after normalize.
        self.assertEqual(
            cv._normalize("104,832", "Avg. Employee-Count"),
            cv._normalize("104832", "Avg. Employee-Count"),
        )
        self.assertEqual(cv._normalize("1,000+", "Employee-Count"), "1000")

    def test_employee_count_real_difference_preserved(self):
        self.assertNotEqual(
            cv._normalize("95100", "Avg. Employee-Count"),
            cv._normalize("79246", "Avg. Employee-Count"),
        )

    def test_url_strips_scheme_www_and_slash(self):
        a = cv._normalize("https://www.benchling.com/careers/", "Career-Page")
        b = cv._normalize("benchling.com/careers", "Career-Page")
        self.assertEqual(a, b)

    def test_url_real_difference_preserved(self):
        a = cv._normalize("careers.astrazeneca.com", "Career-Page")
        b = cv._normalize("astrazeneca.com/careers", "Career-Page")
        self.assertNotEqual(a, b)

    def test_linkedin_url_normalized(self):
        a = cv._normalize("https://www.linkedin.com/company/x/", "Linkedin-Url")
        b = cv._normalize("linkedin.com/company/x", "Linkedin-Url")
        self.assertEqual(a, b)

    def test_plain_text_collapses_space_and_case(self):
        self.assertEqual(cv._normalize("  Foo   Bar ", "Name"), "foo bar")


class BuildFieldMapTest(unittest.TestCase):
    def test_reads_columns_from_config(self):
        gs = {
            "company_sheet": {
                "employee_count_column": "Avg. Employee-Count",
                "career_page_column": "Career-Page",
                "linkedin_url_column": "Linkedin-Url",
            },
            "enrichment_output_columns": {
                "employee_count": "Employee Count",
                "career_page": "Career Page",
                "linkedin_url": "LinkedIn URL",
            },
        }
        self.assertEqual(
            cv.build_field_map(gs),
            [
                ("Avg. Employee-Count", "Employee Count"),
                ("Career-Page", "Career Page"),
                ("Linkedin-Url", "LinkedIn URL"),
            ],
        )

    def test_falls_back_when_config_empty(self):
        self.assertEqual(cv.build_field_map({}), list(cv._FIELD_MAP))


class IsNaTest(unittest.TestCase):
    def test_na_variants(self):
        for v in ["", "N/A", "na", "none", "-", "  null "]:
            self.assertTrue(cv._is_na(v))

    def test_real_value_not_na(self):
        self.assertFalse(cv._is_na("123"))


if __name__ == "__main__":
    unittest.main()
