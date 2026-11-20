"""Crawler hardening: a non-200 sitemap is a warning and the run continues from the
registry; a non-200 now-airing XML is a warning; a non-ok answer logs the
Server header and the first bytes of the body; a failed backlog month gets a
marker with a growing retry-after (1 → 7 → 30 days) and is counted deferred,
not failed, until then. Every response is stubbed — nothing touches the
network."""
import ast
import datetime as dt
import http.client
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import common  # noqa: E402
import crawl  # noqa: E402

TODAY = dt.date(2026, 11, 12)
WWW = common.WWW
NOW_AIRING = (b"<?xml version='1.0'?><root><scheduleItem><channel>radio1</channel>"
              b"<programmeFolder>Free_as_the_wind</programmeFolder>"
              b"<programmeTitleChi>\xe8\xac\x9b\xe6\x9d\xb1\xe8\xac\x9b\xe8\xa5\xbf</programmeTitleChi>"
              b"</scheduleItem></root>")


class StubClient:
    def __init__(self, answers, months=None):
        self.answers = answers            # url -> (status, ctype, body)
        self.months = months or {}        # (ch, slug, m) -> True (ok) / False (500)
        self.requests = 0
        self.failures = 0
        self.urls = []

    def get(self, url, headers=None, method="GET", timeout=40, tries=3, ok=(200,)):
        self.requests += 1
        self.urls.append(url)
        st, ct, body = self.answers.get(url, (200, "text/html", b""))
        if st != 404 and st not in ok:
            self.failures += 1
        return st, ct, body

    def count_failure(self):
        self.failures += 1

    def get_json(self, url, headers=None):
        self.requests += 1
        self.urls.append(url)
        q = dict(kv.split("=", 1) for kv in url.split("?", 1)[1].split("&"))
        if self.months.get((q["c"], q["p"], q["m"])) is False:
            self.failures += 1
            return 500, None
        return 200, {"status": "0"}


def registry(n=4):
    today = TODAY.isoformat()
    out = {}
    for i in range(n):
        ch = ("radio1", "radio2", "radio3", "pth")[i % 4]
        out[f"{ch}/prog{i}"] = {"channel": ch, "slug": f"prog{i}", "title_zh": f"節目{i}",
                                "lastMetaRefresh": today}
    return out


class Sandboxed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = []
        for p in (mock.patch.object(crawl, "RTHK_DATA", Path(self.tmp.name)),
                  mock.patch.object(crawl, "today_hkt", lambda: TODAY),
                  mock.patch.object(crawl, "log", self.logs.append)):
            p.start()
            self.addCleanup(p.stop)


class SitemapSoftFail(Sandboxed):
    def answers(self, sitemap=502, airing=200):
        a = {f"{WWW}/sitemap.xml": (sitemap, "text/html", b"<html>Bad Gateway</html>")}
        a[f"{WWW}/radio/get_radio_programme_info?c=radio1&p=x"] = (airing, "text/xml", NOW_AIRING)
        return a

    def test_sitemap_502_warns_and_enumerates_the_whole_registry(self):
        programmes = registry()
        before = set(programmes)
        run = {"warnings": []}
        client = StubClient(self.answers())
        crawl.discover(client, programmes, None, run)          # no RuntimeError
        self.assertEqual(run["sitemap_programmes"], 0)
        self.assertTrue(any(w.startswith("sitemap.xml 502") for w in run["warnings"]), run["warnings"])
        self.assertTrue(before <= set(programmes))             # the registry never shrinks
        self.assertIn("radio1/Free_as_the_wind", programmes)   # the other sources still ran
        crawl.enumerate_programmes(client, programmes, None, 0, False, run)
        self.assertEqual(run["enumerated"], len(programmes))
        self.assertEqual(run["month_failures"], 0)

    def test_now_airing_non_200_is_a_warning(self):
        run = {"warnings": []}
        crawl.discover(StubClient(self.answers(sitemap=200, airing=503)), registry(), None, run)
        self.assertIn("now-airing xml 503", run["warnings"])

    def test_healthy_sources_add_no_warning(self):
        run = {"warnings": []}
        crawl.discover(StubClient(self.answers(sitemap=200)), registry(), None, run)
        self.assertEqual(run["warnings"], [])


class FailedMonthMarker(Sandboxed):
    def test_backoff_schedule_1_7_30_30(self):
        m, seen = None, []
        day = TODAY
        for _ in range(4):
            m = crawl.backlog_marker(m, day)
            seen.append((dt.date.fromisoformat(m["retry_after"]) - day).days)
            day = dt.date.fromisoformat(m["retry_after"])
        self.assertEqual(seen, [1, 7, 30, 30])
        self.assertEqual(m["first_failed"], TODAY.isoformat())
        self.assertEqual(m["failures"], 4)

    def test_deferred_until_retry_after(self):
        m = crawl.backlog_marker(None, TODAY)
        self.assertTrue(crawl.backlog_deferred(m, TODAY))
        self.assertFalse(crawl.backlog_deferred(m, TODAY + dt.timedelta(days=1)))
        self.assertFalse(crawl.backlog_deferred(None, TODAY))
        self.assertFalse(crawl.backlog_deferred({"retry_after": "junk"}, TODAY))

    def backlog_run(self, failed_months, months, today=TODAY):
        programmes = {"radio3/backchat": {"channel": "radio3", "slug": "backchat"}}
        window = common.months_back(common.RETENTION_MONTHS, today)
        # every old month cached except 202601 (the chronic one)
        for m in window[common.INCREMENTAL_MONTHS:]:
            if m != "202601":
                common.write_json(crawl.month_path("radio3", "backchat", m), {"episodes": []})
        client = StubClient({}, months)
        run = {"warnings": []}
        with mock.patch.object(crawl, "today_hkt", lambda: today):
            crawl.enumerate_programmes(client, programmes, None, 0, True, run, failed_months)
        asked = [u for u in client.urls if "m=202601" in u]
        return run, asked

    def test_chronic_month_is_marked_then_deferred_then_cleared(self):
        failed = {}
        bad = {("radio3", "backchat", "202601"): False}
        run, asked = self.backlog_run(failed, bad)
        self.assertEqual(len(asked), 1)
        self.assertEqual(run["month_failures"], 1)
        self.assertEqual(failed["radio3/backchat/202601"]["retry_after"], "2026-11-13")
        # same day again: deferred, no request, not a failure
        run, asked = self.backlog_run(failed, bad)
        self.assertEqual(asked, [])
        self.assertEqual(run["month_failures"], 0)
        self.assertEqual(run["backlog_deferred"], 1)
        self.assertEqual(run["backlog_months"], 1)
        # retry day: fails again → 7 days
        run, asked = self.backlog_run(failed, bad, TODAY + dt.timedelta(days=1))
        self.assertEqual(len(asked), 1)
        self.assertEqual(failed["radio3/backchat/202601"]["retry_after"], "2026-11-20")
        self.assertEqual(failed["radio3/backchat/202601"]["first_failed"], "2026-11-12")
        # it answers on the next retry: cached, marker gone
        run, asked = self.backlog_run(failed, {}, TODAY + dt.timedelta(days=8))
        self.assertEqual(len(asked), 1)
        self.assertEqual(failed, {})
        self.assertTrue(crawl.month_path("radio3", "backchat", "202601").exists())

    def test_markers_outside_the_window_are_pruned(self):
        failed = {"radio3/backchat/202401": crawl.backlog_marker(None, TODAY)}
        self.backlog_run(failed, {})
        self.assertNotIn("radio3/backchat/202401", failed)


def headers(**kv):
    m = http.client.HTTPMessage()
    for k, v in kv.items():
        m[k.replace("_", "-")] = v
    return m


class NonOkObservability(unittest.TestCase):
    def test_non_ok_logs_server_and_body_snippet(self):
        logs = []

        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", headers(Server="AkamaiGHost",
                                         Content_Type="text/html"), io.BytesIO(b"<html>" + b"x" * 500))
        client = common.Client(pace_s=0, budget=5, log=logs.append)
        with mock.patch("urllib.request.urlopen", fake):
            st, _, _ = client.get("https://example.test/sitemap.xml", tries=1)
        self.assertEqual(st, 502)
        line = next(x for x in logs if "non-ok 502" in x)
        self.assertIn("server=AkamaiGHost", line)
        self.assertIn("<html>xxx", line)
        body = line.split("body=", 1)[1]
        self.assertLessEqual(len(ast.literal_eval(body)), common.SNIPPET_BYTES)

    def test_404_is_an_answer_and_logs_nothing(self):
        logs = []

        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", headers(), None)
        client = common.Client(pace_s=0, budget=5, log=logs.append)
        with mock.patch("urllib.request.urlopen", fake):
            client.get("https://example.test/x", tries=1)
        self.assertEqual(logs, [])
        self.assertEqual(client.failures, 0)


class EgressIp(unittest.TestCase):
    def test_ip_is_logged_or_unknown(self):
        ok = StubClient({"https://api.ipify.org": (200, "text/plain", b"20.1.2.3\n")})
        self.assertEqual(crawl.egress_ip(ok), "20.1.2.3")
        bad = StubClient({"https://api.ipify.org": (503, "text/html", b"<html>")})
        self.assertEqual(crawl.egress_ip(bad), "unknown (503)")
        junk = StubClient({"https://api.ipify.org": (200, "text/html", b"<html>hi</html>")})
        self.assertEqual(crawl.egress_ip(junk), "unknown (200)")


if __name__ == "__main__":
    unittest.main()
