"""The control spreadsheet: one registry, many projects, one password each."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import projects_registry as pr                                 # noqa: E402


class FakeWorksheet:
    """Just enough gspread worksheet to exercise the registry."""

    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.frozen = 0

    def get_all_values(self):
        width = max((len(r) for r in self.rows), default=0)
        return [r + [""] * (width - len(r)) for r in self.rows]

    def row_values(self, n):
        return list(self.rows[n - 1]) if n <= len(self.rows) else []

    def append_row(self, values, value_input_option=None):
        self.rows.append(list(values))

    def update(self, cell, values):
        self.rows[0] = list(values[0])

    def update_cell(self, row, col, value):
        while len(self.rows) < row:
            self.rows.append([])
        while len(self.rows[row - 1]) < col:
            self.rows[row - 1].append("")
        self.rows[row - 1][col - 1] = value

    def freeze(self, rows=0):
        self.frozen = rows


class FakeRegistry(pr.ProjectRegistry):
    """A registry backed by a list of rows instead of Google Sheets."""

    def __init__(self, rows=None):
        super().__init__(spreadsheet_id="ctrl", credentials_file="key.json")
        self._worksheet = FakeWorksheet(rows if rows is not None else [pr.HEADER])

    def _open(self):
        return self._worksheet


def project_row(project_id, name, sheet_id, password, status="active",
                data_key="dk-" + "x" * 8):
    salt = "salt-" + project_id
    return [project_id, name, sheet_id, status, data_key, salt,
            pr.hash_password(password, salt), "2026-01-01T00:00:00Z", ""]


def registry_with_two():
    return FakeRegistry([
        pr.HEADER,
        project_row("alpha", "Alpha Ltd", "sheet-a", "alpha-password"),
        project_row("beta", "Beta Corp", "sheet-b", "beta-password"),
        project_row("gone", "Archived", "sheet-c", "gone-password", status="archived"),
    ])


class Hashing(unittest.TestCase):
    def test_same_input_same_hash(self):
        self.assertEqual(pr.hash_password("pw", "salt"), pr.hash_password("pw", "salt"))

    def test_the_salt_changes_the_hash(self):
        self.assertNotEqual(pr.hash_password("pw", "a"), pr.hash_password("pw", "b"))

    def test_it_is_hex_of_a_sha256(self):
        digest = pr.hash_password("pw", "salt")
        self.assertEqual(len(digest), 64)
        int(digest, 16)                      # raises if it is not hex

    def test_verify_accepts_the_right_password(self):
        row = dict(zip(pr.HEADER, project_row("a", "A", "s", "correct-horse")))
        self.assertTrue(pr.verify_password(row, "correct-horse"))
        self.assertFalse(pr.verify_password(row, "Correct-Horse"))

    def test_a_row_with_no_hash_never_verifies(self):
        # A half-filled row must not be a way in.
        self.assertFalse(pr.verify_password({"pw_hash": "", "pw_salt": "s"}, ""))
        self.assertFalse(pr.verify_password({}, "anything"))
        self.assertFalse(pr.verify_password(None, "anything"))

    def test_keys_and_salts_are_not_reused(self):
        self.assertNotEqual(pr.new_data_key(), pr.new_data_key())
        self.assertNotEqual(pr.new_salt(), pr.new_salt())
        self.assertGreaterEqual(len(pr.new_data_key()), 32)


class Identifiers(unittest.TestCase):
    def test_ids_are_normalised(self):
        self.assertEqual(pr.validate_id("  MAIN  "), "main")

    def test_bad_ids_are_refused(self):
        for bad in ["", "-leading", "has space", "sym#bol", "x" * 33]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    pr.validate_id(bad)

    def test_slugify_matches_the_apps_script(self):
        self.assertEqual(pr.slugify("Biotech Jobs"), "biotech-jobs")
        self.assertEqual(pr.slugify("  Acme & Co!  "), "acme-co")
        self.assertEqual(pr.slugify("!!!"), "project")
        self.assertLessEqual(len(pr.slugify("x" * 60)), 32)


class Reading(unittest.TestCase):
    def test_archived_projects_are_hidden_by_default(self):
        reg = registry_with_two()
        self.assertEqual([p["id"] for p in reg.list()], ["alpha", "beta"])
        self.assertIn("gone", [p["id"] for p in reg.list(include_archived=True)])

    def test_get_is_case_insensitive(self):
        self.assertEqual(registry_with_two().get("ALPHA")["name"], "Alpha Ltd")

    def test_get_returns_none_for_a_stranger(self):
        self.assertIsNone(registry_with_two().get("nope"))

    def test_blank_rows_are_skipped(self):
        reg = FakeRegistry([pr.HEADER, [""] * len(pr.HEADER),
                            project_row("only", "Only", "s", "only-password")])
        self.assertEqual([p["id"] for p in reg.list()], ["only"])

    def test_the_password_selects_the_project(self):
        reg = registry_with_two()
        self.assertEqual(reg.find_by_password("beta-password")["id"], "beta")
        self.assertIsNone(reg.find_by_password("alpha-password!"))
        self.assertIsNone(reg.find_by_password(""))

    def test_an_archived_project_cannot_be_signed_into(self):
        self.assertIsNone(registry_with_two().find_by_password("gone-password"))


class Writing(unittest.TestCase):
    def test_create_registers_a_usable_project(self):
        reg = FakeRegistry()
        record = reg.create(name="Biotech Jobs", spreadsheet_id="sheet-x",
                            password="biotech-password")
        self.assertEqual(record["id"], "biotech-jobs")
        self.assertEqual(reg.get("biotech-jobs")["spreadsheet_id"], "sheet-x")
        self.assertEqual(reg.find_by_password("biotech-password")["id"], "biotech-jobs")

    def test_the_password_is_never_stored_as_typed(self):
        reg = FakeRegistry()
        reg.create(name="Alpha", spreadsheet_id="s", password="super-secret")
        self.assertNotIn("super-secret", str(reg._worksheet.rows))

    def test_each_project_gets_its_own_data_key(self):
        reg = FakeRegistry()
        a = reg.create(name="A", spreadsheet_id="s1", password="a-password")
        b = reg.create(name="B", spreadsheet_id="s2", password="b-password")
        self.assertNotEqual(a["data_key"], b["data_key"])

    def test_a_pinned_data_key_is_kept(self):
        # Migrating an existing project must not strand what it already published.
        reg = FakeRegistry()
        made = reg.create(name="Old", spreadsheet_id="s", password="old-password",
                          data_key="the-existing-key")
        self.assertEqual(made["data_key"], "the-existing-key")

    def test_duplicate_ids_are_refused(self):
        reg = FakeRegistry()
        reg.create(name="Alpha", spreadsheet_id="s", password="a-password")
        with self.assertRaises(ValueError):
            reg.create(name="Alpha", spreadsheet_id="s2", password="b-password")

    def test_incomplete_projects_are_refused(self):
        reg = FakeRegistry()
        with self.assertRaises(ValueError):
            reg.create(name="No Sheet", spreadsheet_id="", password="a-password")
        with self.assertRaises(ValueError):
            reg.create(name="No Password", spreadsheet_id="s", password="")

    def test_changing_the_password_leaves_the_data_key_alone(self):
        reg = registry_with_two()
        before = reg.get("alpha")["data_key"]
        reg.set_password("alpha", "a-new-password")
        self.assertEqual(reg.get("alpha")["data_key"], before)
        self.assertEqual(reg.find_by_password("a-new-password")["id"], "alpha")
        self.assertIsNone(reg.find_by_password("alpha-password"))

    def test_archiving_hides_a_project_without_deleting_it(self):
        reg = registry_with_two()
        reg.archive("alpha")
        self.assertNotIn("alpha", [p["id"] for p in reg.list()])
        self.assertIsNotNone(reg.get("alpha"))

    def test_unknown_columns_are_refused(self):
        reg = registry_with_two()
        with self.assertRaises(ValueError):
            reg.update("alpha", nonsense="1")


class Wiring(unittest.TestCase):
    def test_apply_project_moves_only_the_spreadsheet(self):
        config = {"google_sheets": {"spreadsheet_id": "old", "jobs_worksheet": "Jobs"},
                  "scraping": {"platforms": ["linkedin"]}}
        pr.apply_project(config, {"id": "beta", "name": "Beta", "spreadsheet_id": "new"})
        self.assertEqual(config["google_sheets"]["spreadsheet_id"], "new")
        # Everything else about how a project runs lives in its own Settings tab.
        self.assertEqual(config["google_sheets"]["jobs_worksheet"], "Jobs")
        self.assertEqual(config["scraping"]["platforms"], ["linkedin"])
        self.assertEqual(config["active_project"]["id"], "beta")

    def test_without_a_control_sheet_nothing_changes(self):
        config = {"google_sheets": {"spreadsheet_id": "only-one"}}
        self.assertFalse(pr.is_enabled(config))
        self.assertIsNone(pr.resolve(config, None))
        self.assertEqual(config["google_sheets"]["spreadsheet_id"], "only-one")

    def test_asking_for_a_project_without_a_registry_is_an_error(self):
        # Silently ignoring --project would run against the wrong spreadsheet.
        with self.assertRaises(RuntimeError):
            pr.resolve({"google_sheets": {"spreadsheet_id": "one"}}, "beta")

    def test_the_env_var_enables_the_registry(self):
        config = {"control": {}, "google_sheets": {}}
        self.assertFalse(pr.is_enabled(config))
        os.environ["CONTROL_SPREADSHEET_ID"] = "from-env"
        try:
            self.assertTrue(pr.is_enabled(config))
            self.assertEqual(pr.registry_from_config(config).spreadsheet_id, "from-env")
        finally:
            del os.environ["CONTROL_SPREADSHEET_ID"]

    def test_it_can_be_switched_off_explicitly(self):
        self.assertFalse(pr.is_enabled({"control": {"enabled": False,
                                                    "spreadsheet_id": "ctrl"}}))


if __name__ == "__main__":
    unittest.main()
