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
        self.column_writes = []    # (col, start_row, values) — one per batched write
        self.write_raises = None   # set to a message to simulate a failing write
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

    def write_column_values(self, col, values, worksheet_name=None, start_row=2):
        if self.write_raises:
            raise RuntimeError(self.write_raises)
        self.column_writes.append((col, start_row, [v[0] for v in values]))

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
        # ONE batched write for the whole column, not one per row. Sheets allows
        # sixty writes a minute, so per-row writes 429 on a few hundred rows and
        # leave the rest blank.
        self.assertEqual(store.updates, [], "no per-row writes should be made")
        self.assertEqual(len(store.column_writes), 1)
        col, start_row, values = store.column_writes[0]
        self.assertEqual((col, start_row), (4, 2))
        self.assertEqual(values, [jv.STATUS_ACTIVE, jv.STATUS_REMOVED])
        self.assertEqual(summary[jv.STATUS_ACTIVE], 1)
        self.assertEqual(summary[jv.STATUS_REMOVED], 1)

    @mock.patch("job_validator.check_job_url")
    def test_one_write_however_many_rows(self, mock_check):
        rows = [["Company", "Job Link", "Job Status"]]
        rows += [[f"C{n}", f"https://x.test/{n}", ""] for n in range(300)]
        mock_check.return_value = jv.STATUS_ACTIVE
        store = FakeStore(rows)

        jv.validate_jobs(store, worksheet="Jobs_Test", delay_every=999)

        self.assertEqual(len(store.column_writes), 1,
                         "300 rows must still cost exactly one column write")
        self.assertEqual(len(store.column_writes[0][2]), 300)

    @mock.patch("job_validator.check_job_url")
    def test_rows_that_were_skipped_keep_their_value(self, mock_check):
        # The column is rewritten wholesale, so anything not re-checked has to
        # be written back as it was rather than blanked.
        rows = [
            ["Company", "Job Link", "Job Status"],
            ["Done", "https://x.test/1", jv.STATUS_EXPIRED],   # skipped
            ["New", "https://x.test/2", ""],                   # checked
        ]
        mock_check.side_effect = [jv.STATUS_ACTIVE]
        store = FakeStore(rows)

        jv.validate_jobs(store, worksheet="Jobs_Test", delay_every=999,
                         re_validate=False)

        self.assertEqual(store.column_writes[0][2],
                         [jv.STATUS_EXPIRED, jv.STATUS_ACTIVE])

    @mock.patch("job_validator.check_job_url")
    def test_a_failed_write_is_not_reported_as_success(self, mock_check):
        # A quota error used to be logged per row while the summary still read
        # like a clean run, so a half-validated sheet looked finished.
        rows = [["Company", "Job Link"], ["Acme", "https://acme.test/1"]]
        mock_check.return_value = jv.STATUS_ACTIVE
        store = FakeStore(rows)
        store.write_raises = "[429]: Quota exceeded for 'Write requests'"

        with self.assertRaises(RuntimeError) as caught:
            jv.validate_jobs(store, worksheet="Jobs_Test", delay_every=999)
        self.assertIn("429", str(caught.exception))

    def test_missing_url_column_returns_empty(self):
        store = FakeStore([["Company", "Role"], ["Acme", "Eng"]])
        self.assertEqual(jv.validate_jobs(store, worksheet="Jobs_Test"), {})


if __name__ == "__main__":
    unittest.main()
