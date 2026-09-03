"""The pipeline's validation step must honour the same settings the CLI does.

Both paths validate the same sheet, and the dashboard offers the same two
switches for both. They diverged once: the pipeline called validate_jobs on its
bare defaults, so a full run re-checked every row that already had a status and
never removed a row however the setting was left.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_pipeline as pipeline                         # noqa: E402


def config(**validation):
    return {
        "google_sheets": {"enabled": True, "jobs_worksheet": "Jobs_Test"},
        "job_validation": validation,
    }


class ValidationStepReadsItsConfig(unittest.TestCase):

    def run_step(self, cfg):
        # store_for is the seam, not GoogleSheetsStore: which store the step
        # gets depends on whether the machine has the service-account key or
        # only a project password, and the step must not care.
        with mock.patch.object(pipeline.remote_store, "store_for") as store, \
             mock.patch.object(pipeline.job_validator, "validate_jobs") as validate, \
             mock.patch.object(pipeline.job_validator, "remove_rows_by_status") as remove:
            pipeline.run_validation(cfg)
        return store, validate, remove

    def test_the_step_does_not_choose_a_store_itself(self):
        """Whichever store store_for returns is the one that gets used.

        Building GoogleSheetsStore here is what made validation demand the
        service-account key however the machine was set up.
        """
        store, validate, _ = self.run_step(config())
        self.assertIs(validate.call_args.args[0], store.return_value)

    def test_re_validate_is_passed_through(self):
        _, validate, _ = self.run_step(config(re_validate=False))
        self.assertFalse(validate.call_args.kwargs["re_validate"])

    def test_re_validate_defaults_to_on(self):
        # Absent config must not silently start skipping rows.
        _, validate, _ = self.run_step(config())
        self.assertTrue(validate.call_args.kwargs["re_validate"])

    def test_it_validates_the_configured_worksheet(self):
        _, validate, _ = self.run_step(config())
        self.assertEqual(validate.call_args.kwargs["worksheet"], "Jobs_Test")

    def test_rows_are_removed_when_the_switch_is_on(self):
        _, _, remove = self.run_step(
            config(remove_rows=True, remove_statuses=["Expired", "Removed"]))
        remove.assert_called_once()
        self.assertEqual(remove.call_args[0][3], ["Expired", "Removed"])
        self.assertEqual(remove.call_args[0][1], "Jobs_Test")

    def test_nothing_is_removed_by_default(self):
        # Deleting rows must never be what happens when nobody asked.
        _, _, remove = self.run_step(config())
        remove.assert_not_called()

    def test_the_switch_alone_removes_nothing(self):
        # An empty status list with removal on is a misconfiguration, not an
        # instruction to remove everything.
        _, _, remove = self.run_step(config(remove_rows=True, remove_statuses=[]))
        remove.assert_not_called()

    def test_removal_off_with_statuses_listed_removes_nothing(self):
        _, _, remove = self.run_step(
            config(remove_rows=False, remove_statuses=["Expired"]))
        remove.assert_not_called()

    def test_validation_is_skipped_when_sheets_are_off(self):
        with mock.patch.object(pipeline.job_validator, "validate_jobs") as validate:
            pipeline.run_validation({"google_sheets": {"enabled": False}})
        validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
