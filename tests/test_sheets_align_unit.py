import unittest

from google_sheets_store import _align_record_to_header, _hyperlink_formula, _build_append_rows


class AlignRecordTest(unittest.TestCase):
    def test_maps_platform_to_application_platform_alias(self):
        header = ["Company", "Role", "Location", "Application Platform", "Sourced By", "Job Link", "Keyword"]
        record = {
            "Company": "Acme", "Role": "Engineer", "Location": "Remote",
            "Platform": "career_page", "Job Link": "https://x/job/1", "Keyword": "eng",
        }
        row = _align_record_to_header(record, header)
        self.assertEqual(
            row,
            ["Acme", "Engineer", "Remote", "career_page", "", "https://x/job/1", "eng"],
        )

    def test_job_link_lands_in_correct_column_not_sourced_by(self):
        header = ["Company", "Role", "Location", "Application Platform", "Sourced By", "Job Link", "Keyword"]
        record = {"Company": "A", "Job Link": "https://link"}
        row = _align_record_to_header(record, header)
        self.assertEqual(row[header.index("Job Link")], "https://link")
        self.assertEqual(row[header.index("Sourced By")], "")

    def test_canonical_header_direct_match(self):
        header = ["Company", "Role", "Location", "Platform", "Job Link", "Keyword"]
        record = {"Company": "A", "Platform": "linkedin", "Job Link": "u"}
        row = _align_record_to_header(record, header)
        self.assertEqual(row[header.index("Platform")], "linkedin")

    def test_unknown_columns_become_blank(self):
        header = ["Company", "Mystery Column", "Job Link"]
        record = {"Company": "A", "Job Link": "u"}
        row = _align_record_to_header(record, header)
        self.assertEqual(row, ["A", "", "u"])


class HyperlinkFormulaTest(unittest.TestCase):
    def test_basic_formula(self):
        self.assertEqual(
            _hyperlink_formula("https://linkedin.com/company/x", "Acme"),
            '=HYPERLINK("https://linkedin.com/company/x","Acme")',
        )

    def test_escapes_quotes_in_label(self):
        self.assertEqual(
            _hyperlink_formula("https://u", 'A "B" Co'),
            '=HYPERLINK("https://u","A ""B"" Co")',
        )

    def test_sanitizes_quotes_in_url(self):
        self.assertNotIn('""', _hyperlink_formula('https://u?x="1"', "L").split('","')[0])


class BuildAppendRowsTest(unittest.TestCase):
    HEADER = ["Company", "Role", "Location", "Application Platform", "Sourced By", "Job Link", "Keyword"]

    def test_company_cell_becomes_hyperlink_when_known(self):
        records = [{"Company": "Acme", "Role": "Engineer", "Platform": "career_page",
                    "Job Link": "u1", "Keyword": "eng"}]
        rows = _build_append_rows(records, self.HEADER, {"acme": "https://linkedin.com/company/acme"})
        self.assertEqual(rows[0][0], '=HYPERLINK("https://linkedin.com/company/acme","Acme")')
        # Other columns unaffected
        self.assertEqual(rows[0][self.HEADER.index("Job Link")], "u1")
        self.assertEqual(rows[0][self.HEADER.index("Application Platform")], "career_page")

    def test_plain_name_when_company_unknown(self):
        records = [{"Company": "Unknown Co", "Job Link": "u"}]
        rows = _build_append_rows(records, self.HEADER, {"acme": "https://x"})
        self.assertEqual(rows[0][0], "Unknown Co")

    def test_no_map_leaves_plain_names(self):
        records = [{"Company": "Acme", "Job Link": "u"}]
        rows = _build_append_rows(records, self.HEADER, None)
        self.assertEqual(rows[0][0], "Acme")

    def test_case_insensitive_company_match(self):
        records = [{"Company": "ACME", "Job Link": "u"}]
        rows = _build_append_rows(records, self.HEADER, {"acme": "https://li/acme"})
        self.assertTrue(rows[0][0].startswith("=HYPERLINK("))


if __name__ == "__main__":
    unittest.main()
