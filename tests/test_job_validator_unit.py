import unittest
from unittest import mock

import job_validator as jv


class StatusForCodeTest(unittest.TestCase):
    def test_2xx_and_3xx_are_active(self):
        self.assertEqual(jv._status_for_code(200), jv.STATUS_ACTIVE)
        self.assertEqual(jv._status_for_code(301), jv.STATUS_ACTIVE)

    def test_404_410_are_removed(self):
        self.assertEqual(jv._status_for_code(404), jv.STATUS_REMOVED)
        self.assertEqual(jv._status_for_code(410), jv.STATUS_REMOVED)

    def test_other_4xx_is_expired(self):
        self.assertEqual(jv._status_for_code(401), jv.STATUS_EXPIRED)
        self.assertEqual(jv._status_for_code(403), jv.STATUS_EXPIRED)

    def test_5xx_is_unknown(self):
        self.assertEqual(jv._status_for_code(500), jv.STATUS_UNKNOWN)
        self.assertEqual(jv._status_for_code(503), jv.STATUS_UNKNOWN)


class LinkedInJobIdTest(unittest.TestCase):
    def test_extracts_from_slug(self):
        self.assertEqual(
            jv._linkedin_job_id(
                "https://www.linkedin.com/jobs/view/laboratory-assistant-at-open-systems-inc-4400470409?refId=x"
            ),
            "4400470409",
        )

    def test_extracts_from_current_job_id(self):
        self.assertEqual(
            jv._linkedin_job_id("https://www.linkedin.com/jobs/search/?currentJobId=4304452256"),
            "4304452256",
        )

    def test_no_id_returns_empty(self):
        self.assertEqual(jv._linkedin_job_id("https://www.linkedin.com/company/acme/"), "")


class CheckLinkedInTest(unittest.TestCase):
    URL = "https://www.linkedin.com/jobs/view/role-at-co-4400470409"

    @mock.patch("job_validator.requests.get")
    def test_open_job_is_active(self, mock_get):
        mock_get.return_value = mock.Mock(status_code=200, text="<div>Apply now</div>")
        self.assertEqual(jv._check_linkedin(self.URL, 10), jv.STATUS_ACTIVE)

    @mock.patch("job_validator.requests.get")
    def test_closed_job_banner_is_expired(self, mock_get):
        mock_get.return_value = mock.Mock(
            status_code=200, text="<p>No longer accepting applications</p>"
        )
        self.assertEqual(jv._check_linkedin(self.URL, 10), jv.STATUS_EXPIRED)

    @mock.patch("job_validator.requests.get")
    def test_404_is_removed(self, mock_get):
        mock_get.return_value = mock.Mock(status_code=404, text="")
        self.assertEqual(jv._check_linkedin(self.URL, 10), jv.STATUS_REMOVED)

    @mock.patch("job_validator.requests.get")
    def test_rate_limited_is_unknown(self, mock_get):
        mock_get.return_value = mock.Mock(status_code=999, text="")
        self.assertEqual(jv._check_linkedin(self.URL, 10), jv.STATUS_UNKNOWN)

    def test_non_job_linkedin_url_falls_back(self):
        self.assertIsNone(jv._check_linkedin("https://www.linkedin.com/company/acme/", 10))


class CheckJobUrlTest(unittest.TestCase):
    def test_invalid_url_is_unknown(self):
        self.assertEqual(jv.check_job_url(""), jv.STATUS_UNKNOWN)
        self.assertEqual(jv.check_job_url("not-a-url"), jv.STATUS_UNKNOWN)

    @mock.patch("job_validator._check_linkedin", return_value=jv.STATUS_EXPIRED)
    def test_linkedin_url_uses_guest_check(self, _m):
        self.assertEqual(
            jv.check_job_url("https://www.linkedin.com/jobs/view/x-4400470409"),
            jv.STATUS_EXPIRED,
        )

    @mock.patch("job_validator.requests.head")
    def test_active_via_head(self, mock_head):
        mock_head.return_value = mock.Mock(status_code=200)
        self.assertEqual(jv.check_job_url("https://x.com/job/1"), jv.STATUS_ACTIVE)

    @mock.patch("job_validator.requests.get")
    @mock.patch("job_validator.requests.head")
    def test_head_405_falls_back_to_get(self, mock_head, mock_get):
        mock_head.return_value = mock.Mock(status_code=405)
        mock_get.return_value = mock.Mock(status_code=200)
        self.assertEqual(jv.check_job_url("https://x.com/job/1"), jv.STATUS_ACTIVE)
        mock_get.assert_called_once()

    @mock.patch("job_validator.requests.head")
    def test_404_is_removed(self, mock_head):
        mock_head.return_value = mock.Mock(status_code=404)
        self.assertEqual(jv.check_job_url("https://x.com/job/gone"), jv.STATUS_REMOVED)

    @mock.patch("job_validator.requests.head", side_effect=jv.requests.RequestException("boom"))
    def test_network_error_is_unknown(self, _mock_head):
        self.assertEqual(jv.check_job_url("https://x.com/job/1"), jv.STATUS_UNKNOWN)


class FakeStore:
    """Minimal stand-in for GoogleSheetsStore used to test validate_jobs flow."""

    def __init__(self, rows):
        self._rows = rows
        self.updates = []          # (row, col, value)
        self.row_colors = []       # (row_num, bg_color) recorded by batch_format_rows
        self._ensured = {}

    def load_all_rows(self, worksheet_name=None):
        return self._rows

    def ensure_column(self, header_name, worksheet_name=None):
        header = self._rows[0]
        if header_name in header:
            return header.index(header_name) + 1
        header.append(header_name)
        return len(header)

    def update_cell(self, row, col, value, worksheet_name=None):
        self.updates.append((row, col, value))

    def batch_format_rows(self, row_colors, num_cols=0, worksheet_name=None):
        self.row_colors.extend(row_colors)


class ValidateJobsFlowTest(unittest.TestCase):
    @mock.patch("job_validator.check_job_url")
    def test_creates_status_column_and_writes_statuses(self, mock_check):
        rows = [
            ["Company", "Role", "Job Link"],
            ["Acme", "Engineer", "https://acme.com/job/1"],
            ["Beta", "Designer", "https://beta.com/job/gone"],
            ["NoLink", "Analyst", ""],            # skipped (no URL)
        ]
        mock_check.side_effect = [jv.STATUS_ACTIVE, jv.STATUS_REMOVED]
        store = FakeStore(rows)

        summary = jv.validate_jobs(store, worksheet="Jobs_Test", delay_every=999)

        # Status column auto-added at position 4.
        self.assertEqual(store._rows[0][-1], "Job Status")
        # Two writes (one per row with a URL), at column 4.
        self.assertEqual(store.updates, [
            (2, 4, jv.STATUS_ACTIVE),
            (3, 4, jv.STATUS_REMOVED),
        ])
        self.assertEqual(summary[jv.STATUS_ACTIVE], 1)
        self.assertEqual(summary[jv.STATUS_REMOVED], 1)

    def test_missing_url_column_returns_empty(self):
        store = FakeStore([["Company", "Role"], ["Acme", "Eng"]])
        self.assertEqual(jv.validate_jobs(store, worksheet="Jobs_Test"), {})


if __name__ == "__main__":
    unittest.main()
