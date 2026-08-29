"""Offline tests for the dashboard layer (no network, no Google Sheets)."""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import settings                                    # noqa: E402
from web.runner import RunManager                           # noqa: E402
from web.tasks import TASKS, TASKS_BY_KEY, build_command    # noqa: E402


class TaskCommandTests(unittest.TestCase):
    def test_every_task_points_at_a_real_script(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for task in TASKS:
            with self.subTest(task=task["key"]):
                self.assertTrue(os.path.isfile(os.path.join(root, task["script"])),
                                f"{task['key']} -> missing {task['script']}")

    def test_bool_option_only_appears_when_checked(self):
        on = build_command("py", TASKS_BY_KEY["classify"], {"--dry-run": True})
        off = build_command("py", TASKS_BY_KEY["classify"], {"--dry-run": False})
        self.assertIn("--dry-run", on)
        self.assertNotIn("--dry-run", off)

    def test_zero_means_use_the_config_default(self):
        # A blank numeric box must not become "--limit 0" and silently override
        # the value the operator set in Settings.
        cmd = build_command("py", TASKS_BY_KEY["validate"], {"--limit": 0})
        self.assertNotIn("--limit", cmd)
        cmd = build_command("py", TASKS_BY_KEY["validate"], {"--limit": 25})
        self.assertEqual(cmd[-2:], ["--limit", "25"])

    def test_blank_text_option_is_dropped(self):
        cmd = build_command("py", TASKS_BY_KEY["pagination"], {"--keywords": "   "})
        self.assertNotIn("--keywords", cmd)

    def test_every_task_runs_against_the_shared_config(self):
        for task in TASKS:
            cmd = build_command("py", task, {})
            self.assertEqual(cmd[3:5], ["--config", "config.yaml"], task["key"])


class RunManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = RunManager()

    def _wait(self, run, timeout=10):
        deadline = time.time() + timeout
        while run.status in ("starting", "running") and time.time() < deadline:
            time.sleep(0.05)

    def test_output_is_captured_and_run_succeeds(self):
        run, refusal = self.manager.start(
            "t", "echo", [sys.executable, "-c", "print('hello from the tool')"])
        self.assertIsNone(refusal)
        self._wait(run)
        self.assertEqual(run.status, "done")
        self.assertEqual(run.exit_code, 0)
        self.assertTrue(any("hello from the tool" in l["text"] for l in run.lines))

    def test_failure_is_reported_not_swallowed(self):
        run, _ = self.manager.start("t", "boom", [sys.executable, "-c", "raise SystemExit(3)"])
        self._wait(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.exit_code, 3)

    def test_second_run_is_refused_while_one_is_live(self):
        # Two tools writing the same sheet tabs at once would interleave writes.
        first, _ = self.manager.start("t", "sleeper", [sys.executable, "-c", "import time;time.sleep(3)"])
        second, refusal = self.manager.start("t", "other", [sys.executable, "-c", "pass"])
        self.assertIsNone(second)
        self.assertIn("still running", refusal)
        first.stop()
        self._wait(first)

    def test_stop_kills_the_process(self):
        run, _ = self.manager.start("t", "sleeper", [sys.executable, "-c", "import time;time.sleep(30)"])
        time.sleep(0.4)
        self.assertTrue(run.stop())
        self._wait(run)
        self.assertEqual(run.status, "stopped")

    def test_subscriber_gets_the_backlog_then_the_end_marker(self):
        run, _ = self.manager.start("t", "echo", [sys.executable, "-c", "print('one');print('two')"])
        self._wait(run)
        q = run.subscribe()
        drained = []
        while True:
            item = q.get(timeout=2)
            if item is None:
                break
            drained.append(item["text"])
        self.assertTrue(any("one" in t for t in drained))
        self.assertTrue(any("two" in t for t in drained))


class SettingsTests(unittest.TestCase):
    """Saving from the browser must never damage config.yaml."""

    def setUp(self):
        self.backup = tempfile.mktemp(suffix=".yaml")
        shutil.copy2(settings.CONFIG_PATH, self.backup)

    def tearDown(self):
        shutil.copy2(self.backup, settings.CONFIG_PATH)
        os.unlink(self.backup)

    def test_schema_paths_all_resolve_in_the_real_config(self):
        cfg = settings.load_raw()
        for group in settings.SCHEMA:
            for field in group["fields"]:
                with self.subTest(path=field["path"]):
                    self.assertIsNotNone(
                        settings.get_path(cfg, field["path"]),
                        f"{field['path']} is not present in config.yaml")

    def test_round_trip_preserves_comments_and_formatting(self):
        original = open(settings.CONFIG_PATH, encoding="utf-8").read()
        before = settings.get_path(settings.load_raw(), "http.max_retries")
        settings.save({"http.max_retries": before + 1})
        settings.save({"http.max_retries": before})
        self.assertEqual(open(settings.CONFIG_PATH, encoding="utf-8").read(), original,
                         "an edit-and-revert must leave config.yaml byte-identical")

    def test_unknown_path_is_rejected(self):
        with self.assertRaises(ValueError):
            settings.save({"totally.made.up": 1})

    def test_values_are_coerced_to_the_configured_type(self):
        settings.save({"http.max_retries": "7", "classification.color_rows": 0})
        cfg = settings.load_raw()
        self.assertEqual(settings.get_path(cfg, "http.max_retries"), 7)
        self.assertIs(settings.get_path(cfg, "classification.color_rows"), False)

    def test_a_save_leaves_a_backup(self):
        result = settings.save({"http.max_retries": 9})
        self.assertTrue(os.path.isfile(os.path.join(settings.PROJECT_ROOT, result["backup"])))

    def test_no_change_means_no_write(self):
        current = settings.get_path(settings.load_raw(), "http.max_retries")
        result = settings.save({"http.max_retries": current})
        self.assertEqual(result["changed"], {})
        self.assertIsNone(result["backup"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
