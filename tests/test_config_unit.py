import unittest

from enricher import config as config_helpers


class ConfigHelpersTest(unittest.TestCase):
    def test_required_company_columns_uses_defaults(self):
        self.assertEqual(
            config_helpers.required_company_columns({}),
            {
                "company": "Company",
                "employee_count": "Employee Count",
                "career_page": "Career Page",
                "linkedin_url": "LinkedIn URL",
            },
        )

    def test_required_company_columns_respects_overrides(self):
        gs_config = {
            "companies_columns": {
                "company": "Org",
                "employee_count": "Employees",
                "career_page": "Careers",
                "linkedin_url": "LinkedIn",
            }
        }

        self.assertEqual(
            config_helpers.required_company_columns(gs_config),
            {
                "company": "Org",
                "employee_count": "Employees",
                "career_page": "Careers",
                "linkedin_url": "LinkedIn",
            },
        )

    def test_validation_controls_falls_back_to_enrichment_retry_na(self):
        gs_config = {
            "validation": {"enabled": False},
            "enrichment_controls": {"retry_na_fields": True},
        }

        self.assertEqual(
            config_helpers.validation_controls(gs_config),
            {"enabled": False, "retry_na_fields": True},
        )

    def test_source_sheet_controls_supports_new_headers(self):
        gs_config = {
            "source_sheet": {
                "enabled": True,
                "worksheet": "Company",
                "company_header": "Company Name",
                "employee_count_header": "Employee-Count",
                "career_page_header": "Career-Page",
                "linkedin_url_header": "Linkedin-Url",
                "job_link_header": "Job Link",
                "use_for_company_sync": False,
                "use_for_linkedin_fallback": True,
                "use_for_career_fallback": False,
            }
        }

        self.assertEqual(
            config_helpers.source_sheet_controls(gs_config),
            {
                "enabled": True,
                "worksheet": "Company",
                "company_header": "Company Name",
                "employee_count_header": "Employee-Count",
                "career_page_header": "Career-Page",
                "linkedin_url_header": "Linkedin-Url",
                "job_link_header": "Job Link",
                "use_for_company_sync": False,
                "use_for_linkedin_fallback": True,
                "use_for_career_fallback": False,
            },
        )

    def test_source_sheet_controls_job_link_header_default(self):
        self.assertEqual(
            config_helpers.source_sheet_controls({"source_sheet": {}})["job_link_header"],
            "Job Link",
        )


if __name__ == "__main__":
    unittest.main()