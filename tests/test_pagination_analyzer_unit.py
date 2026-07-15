import unittest
from unittest import mock

import pagination_analyzer as pa


class FakeParser:
    """Maps the (mocked) html — which is just the page index — to job dicts."""

    def __init__(self, pages):
        self.pages = pages   # list of lists of job-link strings

    def _parse(self, html, keyword):
        idx = int(html)
        if idx < len(self.pages):
            return [{"Job Link": u} for u in self.pages[idx]]
        return []


def _fake_get_html(page_size):
    def _inner(session, url, config, params=None):
        return str(params["start"] // page_size)
    return _inner


class ProbeKeywordTest(unittest.TestCase):
    def _run(self, pages, page_size=10, max_pages=20):
        parser = FakeParser(pages)
        with mock.patch.object(pa, "get_html", _fake_get_html(page_size)):
            return pa.probe_keyword(
                session=None, parser=parser, config={}, keyword="x",
                location="US", page_size=page_size, max_probe_pages=max_pages,
                page_delay=0,
            )

    def test_counts_unique_jobs_and_stops_on_empty(self):
        pages = [
            [f"a{i}" for i in range(10)],   # page 0: 10 unique
            [f"b{i}" for i in range(10)],   # page 1: 10 unique
            [],                              # page 2: empty → stop
        ]
        r = self._run(pages)
        self.assertEqual(r["Total Jobs"], 20)
        self.assertEqual(r["Page 1 Jobs"], 10)
        self.assertEqual(r["Beyond Page 1"], 10)
        self.assertEqual(r["Last Non-Empty Page"], 2)
        self.assertEqual(r["Hit Probe Cap"], "no")

    def test_stops_on_all_duplicate_page(self):
        pages = [
            [f"a{i}" for i in range(10)],   # page 0
            [f"a{i}" for i in range(10)],   # page 1: all duplicates → ceiling
        ]
        r = self._run(pages)
        self.assertEqual(r["Total Jobs"], 10)
        self.assertEqual(r["Beyond Page 1"], 0)
        self.assertEqual(r["Last Non-Empty Page"], 1)

    def test_hits_cap_flag(self):
        pages = [[f"p{p}_{i}" for i in range(10)] for p in range(5)]
        r = self._run(pages, max_pages=3)
        self.assertEqual(r["Hit Probe Cap"], "yes")
        self.assertEqual(r["Total Jobs"], 30)
        self.assertEqual(r["Recommended Max Pages"], 3)


class AnalysisCfgTest(unittest.TestCase):
    def test_defaults(self):
        cfg = pa._analysis_cfg({})
        self.assertEqual(cfg["page_size"], 25)   # generic default
        self.assertEqual(cfg["max_probe_pages"], 20)
        self.assertFalse(cfg["write_to_sheet"])

    def test_reads_config(self):
        cfg = pa._analysis_cfg({"pagination_analysis": {"page_size": 10, "max_probe_pages": 50}})
        self.assertEqual(cfg["page_size"], 10)
        self.assertEqual(cfg["max_probe_pages"], 50)


if __name__ == "__main__":
    unittest.main()
