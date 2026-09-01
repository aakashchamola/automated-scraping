"""Retrying the Sheets writes that are worth retrying, and no others.

Sheets allows sixty write requests a minute. Batching makes that limit unlikely
to be met; retrying survives it when something else is writing the same sheet at
the same time. Both are needed — a run that fails on a limit which refills in
under a minute is a poor trade.

Two clients reach Sheets from this codebase, gspread for values and
googleapiclient for the formatting batchUpdate, and they report the status in
different places. Catching only one silently disables the retry for half the
writes, which is why both shapes are tested here.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import google_sheets_store as store                            # noqa: E402


class GspreadError(Exception):
    """Shaped like gspread.exceptions.APIError: status on .response.status_code."""
    def __init__(self, code):
        super().__init__(f"gspread {code}")
        self.response = type("R", (), {"status_code": code})()


class HttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: status on .resp.status."""
    def __init__(self, code):
        super().__init__(f"http {code}")
        self.resp = type("R", (), {"status": code})()


class Retrying(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(store.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def failing_then_ok(self, exc, times):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] <= times:
                raise exc
            return "ok"
        return fn, calls

    def test_a_quota_error_is_retried_for_both_clients(self):
        for name, error in [("gspread", GspreadError(429)), ("googleapi", HttpError(429))]:
            with self.subTest(client=name):
                fn, calls = self.failing_then_ok(error, 2)
                self.assertEqual(store.sheets_call(fn), "ok")
                self.assertEqual(calls["n"], 3)

    def test_transient_server_errors_are_retried(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                fn, calls = self.failing_then_ok(GspreadError(code), 1)
                self.assertEqual(store.sheets_call(fn), "ok")
                self.assertEqual(calls["n"], 2)

    def test_a_request_that_is_simply_wrong_is_not_retried(self):
        # Retrying a 403 or a 404 only delays an error that will not change.
        for name, error in [("gspread 403", GspreadError(403)),
                            ("googleapi 404", HttpError(404)),
                            ("gspread 400", GspreadError(400))]:
            with self.subTest(case=name):
                calls = {"n": 0}

                def fn():
                    calls["n"] += 1
                    raise error
                with self.assertRaises(Exception):
                    store.sheets_call(fn)
                self.assertEqual(calls["n"], 1)

    def test_it_gives_up_and_raises_the_last_error(self):
        calls = {"n": 0}

        def always_429():
            calls["n"] += 1
            raise GspreadError(429)
        with self.assertRaises(GspreadError):
            store.sheets_call(always_429, retries=3)
        self.assertEqual(calls["n"], 3)

    def test_the_last_attempt_does_not_sleep(self):
        # Waiting after the final try delays the failure for no reason.
        def always_429():
            raise GspreadError(429)
        with self.assertRaises(GspreadError):
            store.sheets_call(always_429, retries=3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_backoff_doubles(self):
        def always_429():
            raise GspreadError(429)
        with self.assertRaises(GspreadError):
            store.sheets_call(always_429, retries=4, backoff=8.0)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [8.0, 16.0, 32.0])

    def test_a_success_is_returned_untouched(self):
        self.assertEqual(store.sheets_call(lambda a, b=0: a + b, 1, b=2), 3)
        self.sleep.assert_not_called()

    def test_an_unrelated_exception_propagates(self):
        def boom():
            raise ValueError("not a Sheets problem")
        with self.assertRaises(ValueError):
            store.sheets_call(boom)


class BothAliasesShareOneImplementation(unittest.TestCase):
    """Two copies of this helper drifted apart once already."""

    def test_the_module_helpers_delegate(self):
        # Identity, not a patch: `from x import y` binds the function at import
        # time, so patching the source module would not reach these anyway —
        # and identity is the property that actually matters.
        import cleanup_validation
        import company_enricher
        for module in (company_enricher, cleanup_validation):
            with self.subTest(module=module.__name__):
                self.assertIs(module.sheets_call, store.sheets_call)

    def test_the_helpers_pass_everything_through(self):
        import cleanup_validation
        import company_enricher
        for module in (company_enricher, cleanup_validation):
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    module._sheets_call(lambda a, b=0: a + b, 2, b=3), 5)

    def test_neither_module_reimplements_the_retry(self):
        # Both once carried their own copy, and they had already drifted.
        import inspect
        import cleanup_validation
        import company_enricher
        for module in (company_enricher, cleanup_validation):
            with self.subTest(module=module.__name__):
                body = inspect.getsource(module._sheets_call)
                self.assertNotIn("for attempt in range", body,
                                 "this is a second implementation, not an alias")


if __name__ == "__main__":
    unittest.main()
