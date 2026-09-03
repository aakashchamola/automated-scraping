"""The two stores must stay interchangeable.

The pipeline is handed one of two things: GoogleSheetsStore when the machine
has the service-account key, RemoteSheetsStore when it has only a project
password. Every module between them and the sheet is written against "the
store" and cannot tell which it got.

That only holds while the surfaces match, and it silently stopped holding once
before: four of the eight run modes built GoogleSheetsStore directly, so they
demanded the key however the machine was set up, and "run it on your own
machine" was true for the scrape alone. These tests are what makes that a
failing build rather than a discovery halfway through a run.
"""

import ast
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import remote_store                                           # noqa: E402
from google_sheets_store import GoogleSheetsStore              # noqa: E402

REMOTE = remote_store.RemoteSheetsStore
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def public(cls):
    return {name for name, _ in inspect.getmembers(cls, callable)
            if not name.startswith("_")}


class SurfaceParityTest(unittest.TestCase):

    def test_neither_store_has_a_method_the_other_lacks(self):
        remote, sheets = public(REMOTE), public(GoogleSheetsStore)
        self.assertEqual(
            remote - sheets, set(),
            "RemoteSheetsStore has methods GoogleSheetsStore does not, so code "
            "written against the remote store would break on the owner's machine")
        self.assertEqual(
            sheets - remote, set(),
            "GoogleSheetsStore has methods RemoteSheetsStore does not, so those "
            "callers silently require the service-account key")

    def test_the_signatures_agree(self):
        """Same parameter names in the same order.

        A caller passing positionally is the normal case, so a store that took
        (values, col) instead of (col, values) would write the wrong cells
        rather than raise.
        """
        for name in sorted(public(REMOTE)):
            with self.subTest(method=name):
                mine = inspect.signature(getattr(REMOTE, name))
                theirs = inspect.signature(getattr(GoogleSheetsStore, name))
                self.assertEqual(
                    [p.name for p in mine.parameters.values()],
                    [p.name for p in theirs.parameters.values()],
                    f"{name} takes different parameters on the two stores")

    def test_defaults_agree(self):
        for name in sorted(public(REMOTE)):
            with self.subTest(method=name):
                def defaults(cls):
                    return {p.name: p.default
                            for p in inspect.signature(getattr(cls, name)).parameters.values()
                            if p.default is not inspect.Parameter.empty}
                self.assertEqual(defaults(REMOTE), defaults(GoogleSheetsStore),
                                 f"{name} defaults differently on the two stores")


class NoBypassTest(unittest.TestCase):
    """Nothing may construct GoogleSheetsStore behind store_for's back."""

    # store_for is the one place allowed to: it is what chooses.
    ALLOWED = {"remote_store.py"}

    def _python_files(self):
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs
                       if d not in {".git", ".venv", "tests", "__pycache__",
                                    "node_modules", "site", "logs", "data"}]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(folder, name)

    def test_no_module_builds_the_credentialled_store_itself(self):
        offenders = []
        for path in self._python_files():
            if os.path.basename(path) in self.ALLOWED:
                continue
            with open(path, encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read(), filename=path)
                except SyntaxError:
                    continue          # old scratch files in the repo root
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "GoogleSheetsStore"):
                    offenders.append(
                        f"{os.path.relpath(path, ROOT)}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "these build GoogleSheetsStore directly, so they need the "
            "service-account key even when a project password was supplied — "
            "use remote_store.store_for(config): " + ", ".join(offenders))

    def test_nothing_calls_open_worksheet_any_more(self):
        """A live worksheet handle is the one thing the remote store cannot give.

        Both uses replaced a whole tab, which is now replace_tab on both stores.
        """
        offenders = []
        for path in self._python_files():
            if os.path.basename(path) in {"google_sheets_store.py", "remote_store.py"}:
                continue
            with open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, start=1):
                    if ".open_worksheet(" in line:
                        offenders.append(f"{os.path.relpath(path, ROOT)}:{number}")
        self.assertEqual(
            offenders, [],
            "open_worksheet returns a gspread handle and cannot work through "
            "the Web App; use load_all_rows / write_column_values / "
            "ensure_column / delete_rows / replace_tab: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)
