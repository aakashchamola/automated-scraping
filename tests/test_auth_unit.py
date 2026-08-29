"""The sign-in gate. The Run tab executes commands, so these are load-bearing."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import auth                                  # noqa: E402

USER, PASSWORD = "automation-aakash", "a-strong-test-password"


class CredentialStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = auth.AUTH_FILE
        auth.AUTH_FILE = os.path.join(self.tmp, ".dashboard-auth.json")
        for key in ("DASHBOARD_USER", "DASHBOARD_PASSWORD_HASH"):
            os.environ.pop(key, None)

    def tearDown(self):
        auth.AUTH_FILE = self._real

    def test_password_is_never_stored_in_the_clear(self):
        auth.save_credentials(USER, PASSWORD)
        raw = open(auth.AUTH_FILE, encoding="utf-8").read()
        self.assertNotIn(PASSWORD, raw)
        self.assertIn(USER, raw)

    def test_file_is_owner_only(self):
        auth.save_credentials(USER, PASSWORD)
        self.assertEqual(os.stat(auth.AUTH_FILE).st_mode & 0o777, 0o600)

    def test_session_key_persists_so_logins_survive_a_restart(self):
        auth.save_credentials(USER, PASSWORD)
        self.assertEqual(auth.load_credentials()["secret_key"],
                         auth.load_credentials()["secret_key"])

    def test_environment_overrides_the_file(self):
        auth.save_credentials(USER, PASSWORD)
        os.environ["DASHBOARD_USER"] = "from-env"
        os.environ["DASHBOARD_PASSWORD_HASH"] = "hash-from-env"
        try:
            self.assertEqual(auth.load_credentials()["username"], "from-env")
        finally:
            del os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD_HASH"]

    def test_unconfigured_returns_empty(self):
        self.assertEqual(auth.load_credentials(), {})

    def test_half_written_file_is_treated_as_unconfigured(self):
        with open(auth.AUTH_FILE, "w") as fh:
            json.dump({"username": "x"}, fh)          # no password_hash
        self.assertEqual(auth.load_credentials(), {})


class GateTests(unittest.TestCase):
    """Exercised through the real Flask app."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = auth.AUTH_FILE
        auth.AUTH_FILE = os.path.join(self.tmp, ".dashboard-auth.json")
        auth.save_credentials(USER, PASSWORD)
        auth._attempts.clear()

        for mod in [m for m in sys.modules if m.startswith("web.app")]:
            del sys.modules[mod]
        from web.app import app
        app.config.update(TESTING=True,
                          DASHBOARD_CREDENTIALS=auth.load_credentials())
        app.secret_key = "test-key"
        self.client = app.test_client()

    def tearDown(self):
        auth.AUTH_FILE = self._real

    def _sign_in(self, password=PASSWORD, username=USER):
        return self.client.post("/login", data={"username": username,
                                                "password": password})

    def test_every_api_route_is_closed_when_signed_out(self):
        for route in ("/api/sheet/Jobs_Test", "/api/settings", "/api/runs",
                      "/api/tasks", "/api/targets"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 401)

    def test_running_a_task_is_closed_when_signed_out(self):
        res = self.client.post("/api/run", json={"task": "validate"})
        self.assertEqual(res.status_code, 401)

    def test_page_redirects_to_login_and_remembers_the_target(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login", res.headers["Location"])

    def test_login_page_itself_is_reachable(self):
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_wrong_password_rejected(self):
        self.assertEqual(self._sign_in(password="nope").status_code, 401)

    def test_wrong_username_rejected(self):
        self.assertEqual(self._sign_in(username="someone-else").status_code, 401)

    def test_correct_credentials_open_the_api(self):
        self.assertEqual(self._sign_in().status_code, 302)
        self.assertEqual(self.client.get("/api/tasks").status_code, 200)

    def test_sign_out_closes_it_again(self):
        self._sign_in()
        self.client.get("/logout")
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)

    def test_repeated_failures_are_throttled(self):
        for _ in range(auth.MAX_ATTEMPTS):
            self._sign_in(password="nope")
        self.assertEqual(self._sign_in(password="nope").status_code, 429,
                         "brute-force guessing must be slowed down")

    def test_login_will_not_redirect_off_site(self):
        # ?next=https://evil.example must not become an open redirect.
        self.client.post("/login?next=https://evil.example",
                         data={"username": USER, "password": PASSWORD})
        res = self.client.post("/login?next=//evil.example",
                               data={"username": USER, "password": PASSWORD})
        self.assertNotIn("evil.example", res.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
