"""The Settings worksheet: the published dashboard's only way to be configured."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings_sheet as ss                                    # noqa: E402
from scrapers.career_page import match_keyword                 # noqa: E402
from web.settings import SCHEMA                                # noqa: E402


class FakeStore:
    """Just enough GoogleSheetsStore to exercise read_overrides."""

    def __init__(self, rows):
        self.rows = rows

    def load_all_rows(self, worksheet):
        if self.rows is None:
            raise RuntimeError("worksheet not found")
        return self.rows


HEADER = ["Group", "Setting", "Value", "Type", "Options", "Description"]


def sheet(*rows):
    return FakeStore([HEADER] + [list(r) for r in rows])


class ParseValueTests(unittest.TestCase):
    def field(self, path):
        for group in SCHEMA:
            for f in group["fields"]:
                if f["path"] == path:
                    return f
        raise AssertionError(path)

    def test_blank_means_keep_the_default(self):
        self.assertIsNone(ss.parse_value("", self.field("http.max_retries")))
        self.assertIsNone(ss.parse_value("   ", self.field("http.max_retries")))

    def test_booleans_accept_what_a_spreadsheet_produces(self):
        f = self.field("job_validation.remove_rows")
        for text in ("TRUE", "true", "Yes", "1", "on"):
            self.assertIs(ss.parse_value(text, f), True, text)
        for text in ("FALSE", "false", "No", "0", "off"):
            self.assertIs(ss.parse_value(text, f), False, text)

    def test_bad_boolean_is_rejected_with_a_readable_message(self):
        with self.assertRaises(ValueError) as ctx:
            ss.parse_value("maybe", self.field("job_validation.remove_rows"))
        self.assertIn("TRUE", str(ctx.exception))

    def test_numbers_respect_the_schema_bounds(self):
        f = self.field("scraping.platform_settings.linkedin.max_pages")
        self.assertEqual(ss.parse_value("7", f), 7)
        with self.assertRaises(ValueError):
            ss.parse_value("0", f)          # below min
        with self.assertRaises(ValueError):
            ss.parse_value("999", f)        # above max
        with self.assertRaises(ValueError):
            ss.parse_value("seven", f)

    def test_select_must_be_one_of_the_options(self):
        f = self.field("google_sheets.jobs_worksheet")
        self.assertEqual(ss.parse_value("Jobs", f), "Jobs")
        with self.assertRaises(ValueError):
            ss.parse_value("NotATab", f)

    def test_multiselect_splits_on_commas_and_validates(self):
        f = self.field("job_validation.remove_statuses")
        self.assertEqual(ss.parse_value("Expired, Removed", f), ["Expired", "Removed"])
        with self.assertRaises(ValueError):
            ss.parse_value("Expired, Nonsense", f)


class ReadOverridesTests(unittest.TestCase):
    def test_missing_worksheet_is_not_an_error(self):
        overrides, problems = ss.read_overrides(FakeStore(None))
        self.assertEqual((overrides, problems), ({}, []))

    def test_only_non_blank_cells_override(self):
        overrides, problems = ss.read_overrides(sheet(
            ["Network", "http.max_retries", "5", "int", "", ""],
            ["Network", "http.timeout_seconds", "", "int", "", ""],
        ))
        self.assertEqual(overrides, {"http.max_retries": 5})
        self.assertEqual(problems, [])

    def test_unknown_setting_is_reported_not_applied(self):
        overrides, problems = ss.read_overrides(sheet(
            ["X", "made.up.path", "9", "int", "", ""]))
        self.assertEqual(overrides, {})
        self.assertIn("unknown setting", problems[0])

    def test_a_bad_value_never_takes_the_run_down(self):
        # A typo in one cell must not stop the other settings applying.
        overrides, problems = ss.read_overrides(sheet(
            ["Network", "http.max_retries", "not-a-number", "int", "", ""],
            ["Network", "http.timeout_seconds", "30", "int", "", ""],
        ))
        self.assertEqual(overrides, {"http.timeout_seconds": 30})
        self.assertEqual(len(problems), 1)
        self.assertIn("keeping the current value", problems[0])

    def test_apply_only_reports_real_changes(self):
        config = {"http": {"max_retries": 3}}
        self.assertEqual(ss.apply_overrides(config, {"http.max_retries": 3}), [])
        changed = ss.apply_overrides(config, {"http.max_retries": 8})
        self.assertEqual(config["http"]["max_retries"], 8)
        self.assertEqual(len(changed), 1)


class KeywordStrictnessTests(unittest.TestCase):
    """career_pages.keyword_match_mode — why a 325-company run kept 9 postings."""

    KEYWORDS = ["Research Assistant Biology", "Microbiologist"]

    def test_all_requires_every_word(self):
        self.assertEqual(
            match_keyword("Research Assistant, Molecular Biology", self.KEYWORDS, "all"),
            "Research Assistant Biology")
        self.assertEqual(match_keyword("Research Associate II", self.KEYWORDS, "all"), "")

    def test_most_needs_more_than_half(self):
        self.assertEqual(match_keyword("Research Associate II", self.KEYWORDS, "most"), "")
        self.assertEqual(
            match_keyword("Research Assistant, Chemistry", self.KEYWORDS, "most"),
            "Research Assistant Biology")

    def test_any_matches_on_one_word(self):
        self.assertEqual(
            match_keyword("Research Associate II", self.KEYWORDS, "any"),
            "Research Assistant Biology")

    def test_unrelated_titles_never_match_in_any_mode(self):
        for mode in ("all", "most", "any"):
            self.assertEqual(match_keyword("Software Engineer", self.KEYWORDS, mode), "", mode)

    def test_default_mode_is_the_strict_one(self):
        self.assertEqual(match_keyword("Research Associate II", self.KEYWORDS), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
