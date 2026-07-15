import unittest

import company_classifier as cc


class ClassifyOrganizationTest(unittest.TestCase):
    def test_edu_domain_university(self):
        cat, _ = cc.classify_organization("Stanford University", "https://careers.stanford.edu")
        self.assertEqual(cat, cc.UNIVERSITY)

    def test_edu_domain_without_university_keyword_is_educational(self):
        cat, _ = cc.classify_organization("Salk Institute", "https://salk.edu")
        self.assertEqual(cat, cc.EDUCATIONAL)

    def test_gov_domain(self):
        cat, _ = cc.classify_organization("Dept of Energy", "https://energy.gov")
        self.assertEqual(cat, cc.GOVERNMENT)

    def test_university_keyword_without_domain(self):
        cat, _ = cc.classify_organization("Hong Kong Baptist University")
        self.assertEqual(cat, cc.UNIVERSITY)

    def test_hospital_keyword(self):
        cat, _ = cc.classify_organization("Mayo Clinic", "https://mayoclinic.org")
        self.assertEqual(cat, cc.HOSPITAL)

    def test_nonprofit_keyword(self):
        cat, _ = cc.classify_organization("American Red Cross")
        self.assertEqual(cat, cc.NONPROFIT)

    def test_research_keyword(self):
        cat, _ = cc.classify_organization("Lawrence Berkeley National Laboratory")
        self.assertEqual(cat, cc.RESEARCH)

    def test_for_profit_suffix_is_company(self):
        self.assertEqual(cc.classify_organization("Genentech, Inc.")[0], cc.COMPANY)
        self.assertEqual(cc.classify_organization("Acme Biosciences")[0], cc.COMPANY)

    def test_laboratories_in_name_is_not_research(self):
        # Regression: "Abbott Laboratories" is a for-profit company, not a lab.
        self.assertEqual(cc.classify_organization("Abbott Laboratories")[0], cc.COMPANY)
        self.assertEqual(cc.classify_organization("Bio-Rad Laboratories")[0], cc.COMPANY)

    def test_default_is_company(self):
        self.assertEqual(cc.classify_organization("Pfizer")[0], cc.COMPANY)

    def test_industry_hint_used_when_no_other_signal(self):
        cat, _ = cc.classify_organization("Some Org", industry="Higher Education")
        self.assertEqual(cat, cc.UNIVERSITY)

    def test_org_tld_weak_nonprofit(self):
        cat, reason = cc.classify_organization("Open Data Group", "https://opendata.org")
        self.assertEqual(cat, cc.NONPROFIT)
        self.assertIn("weak", reason)


class DomainTest(unittest.TestCase):
    def test_strips_scheme_and_www(self):
        self.assertEqual(cc._domain("https://www.example.com/careers?x=1"), "example.com")

    def test_bare_domain(self):
        self.assertEqual(cc._domain("careers.stanford.edu"), "careers.stanford.edu")

    def test_empty(self):
        self.assertEqual(cc._domain(""), "")


class ClassifySheetTest(unittest.TestCase):
    """Drive classify_sheet against an in-memory fake store."""

    class FakeStore:
        def __init__(self, rows):
            self._rows = rows
            self.written = None     # (col, values, start_row)
            self.colors = []

        def load_all_rows(self, worksheet_name=None):
            return self._rows

        def ensure_column(self, header_name, worksheet_name=None):
            header = self._rows[0]
            if header_name in header:
                return header.index(header_name) + 1
            header.append(header_name)
            return len(header)

        def write_column_values(self, col, values, worksheet_name=None, start_row=2):
            self.written = (col, values, start_row)

        def batch_format_rows(self, row_colors, num_cols=0, worksheet_name=None):
            self.colors.extend(row_colors)

    def test_classifies_and_writes_column(self):
        rows = [
            ["Company", "Career Page", "LinkedIn URL"],
            ["Stanford University", "https://stanford.edu", ""],
            ["Genentech, Inc.", "https://gene.com", ""],
            ["", "", ""],                               # blank name → skipped value
        ]
        store = self.FakeStore(rows)
        summary = cc.classify_sheet(
            store, worksheet="CompaniesTest", name_col="Company",
            output_col="Organization Type", career_col="Career Page",
            linkedin_col="LinkedIn URL", write=True, color_rows=True,
        )
        self.assertEqual(summary.get(cc.UNIVERSITY), 1)
        self.assertEqual(summary.get(cc.COMPANY), 1)
        # Column written at position 4 (auto-created), with one row per data row.
        col, values, start = store.written
        self.assertEqual(col, 4)
        self.assertEqual(start, 2)
        self.assertEqual(values[0], [cc.UNIVERSITY])
        self.assertEqual(values[1], [cc.COMPANY])


if __name__ == "__main__":
    unittest.main()
