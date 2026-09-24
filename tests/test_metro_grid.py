"""S31a — Metro is a half-hour grid: slot URLs carry HHMM, Client records
Content-Length, and crawl.metro_window runs ONE :30-slot HEAD per frequency as
a warn-only grid canary. Every response is mocked — nothing touches the
network (REPLAY.md §2.4)."""
import datetime as dt
import http.client
import re
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import common  # noqa: E402
import crawl  # noqa: E402

TODAY = dt.date(2026, 9, 24)
EARLIEST = dt.date(2026, 6, 1)
SLOT_BYTES = 14_332_141          # a real :30 file (probed 2026-09-24): ≈ 1791.5 s at 64 kbps
URL_RE = re.compile(r"/(\d+)/(\d{8})/\1_(\d{8})(\d{4})\.mp3$")


def headers(**kv):
    m = http.client.HTTPMessage()
    for k, v in kv.items():
        m[k.replace("_", "-")] = v
    return m


class FakeResp:
    def __init__(self, status, hdrs):
        self.status, self.headers = status, hdrs

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeMetro:
    """Answers Metro HEADs like the archive: 200 inside [earliest, today],
    404 outside; the :30 slot answer is configurable per scenario."""

    def __init__(self, slot30_bytes=SLOT_BYTES, slot30_status=200, today_open=True,
                 missing_days=()):
        self.slot30_bytes, self.slot30_status = slot30_bytes, slot30_status
        self.today_open, self.missing_days = today_open, set(missing_days)
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append((req.get_method(), url))
        m = URL_RE.search(url)
        assert m, url
        day = dt.datetime.strptime(m.group(2), "%Y%m%d").date()
        hhmm = m.group(4)
        live = EARLIEST <= day <= TODAY and day not in self.missing_days
        if day == TODAY and not self.today_open:
            live = False
        if live and hhmm.endswith("30"):
            if self.slot30_status != 200:
                live = False
            elif self.slot30_bytes is None:
                return FakeResp(200, headers(Content_Type="audio/x-mpg"))
            else:
                return FakeResp(200, headers(Content_Type="audio/x-mpg",
                                             Content_Length=str(self.slot30_bytes)))
        if not live:
            raise urllib.error.HTTPError(url, 404, "Not Found", headers(), None)
        return FakeResp(200, headers(Content_Type="audio/x-mpg", Content_Length=str(SLOT_BYTES)))

    def slot30(self):
        return [u for _, u in self.urls if re.search(r"30\.mp3$", u)]


def run_window(fake, window=None):
    client = common.Client(pace_s=0, budget=500, log=lambda *_: None)
    run = {"warnings": []}
    window = {} if window is None else window
    with mock.patch("urllib.request.urlopen", fake), \
            mock.patch.object(crawl, "today_hkt", lambda: TODAY):
        crawl.metro_window(client, window, run)
    return client, run, window


class Constants(unittest.TestCase):
    def test_half_hour_grid(self):
        self.assertEqual(common.METRO_SEGMENT_MINUTES, 30)
        self.assertEqual(common.METRO_BITRATE_BPS, 64_000)
        self.assertEqual(common.STREAM_VERSION, 1)      # S31a: NO streamVersion bump

    def test_real_slot_size_is_thirty_minutes_not_sixty(self):
        secs = SLOT_BYTES * 8 / common.METRO_BITRATE_BPS
        self.assertAlmostEqual(secs, 1791.5, delta=0.5)
        self.assertLess(abs(secs - 30 * 60), 0.05 * 30 * 60)


class SlotUrls(unittest.TestCase):
    def test_datetime_is_slot_start_hhmm(self):
        d = dt.date(2026, 9, 23)
        self.assertEqual(crawl.metro_url("104", d, "08", "30"),
                         "https://arch.metroradio.hk/104/20260923/104_202609230830.mp3")
        self.assertEqual(crawl.metro_url("1044", dt.date(2026, 7, 15), "14", "30"),
                         "https://arch.metroradio.hk/1044/20260715/1044_202607151430.mp3")

    def test_defaults_keep_the_0800_slot(self):
        self.assertTrue(crawl.metro_url("997", dt.date(2026, 9, 23)).endswith("/997_202609230800.mp3"))


class ClientContentLength(unittest.TestCase):
    def client(self):
        return common.Client(pace_s=0, budget=50, log=lambda *_: None)

    def test_records_content_length_and_resets_per_call(self):
        c = self.client()
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp(200, headers(Content_Length="14332141"))):
            st, _, body = c.get("https://x.test/a.mp3", method="HEAD")
        self.assertEqual((st, body, c.last_length), (200, b"", 14_332_141))
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp(200, headers())):
            c.get("https://x.test/b.mp3", method="HEAD")
        self.assertEqual(c.last_length, 0)             # absent → 0, not the previous value

    def test_unparseable_content_length_is_zero(self):
        c = self.client()
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp(200, headers(Content_Length="n/a"))):
            c.get("https://x.test/a.mp3", method="HEAD")
        self.assertEqual(c.last_length, 0)

    def test_404_is_zero(self):
        c = self.client()
        c.last_length = 99

        def nf(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", headers(Content_Length="9"), None)
        with mock.patch("urllib.request.urlopen", nf):
            st, _, _ = c.get("https://x.test/a.mp3", method="HEAD")
        self.assertEqual((st, c.last_length), (404, 0))

    def test_default_is_zero(self):
        self.assertEqual(self.client().last_length, 0)


class GridCanary(unittest.TestCase):
    def test_one_head_per_frequency_on_a_complete_days_0830(self):
        fake = FakeMetro()
        client, run, window = run_window(fake)
        self.assertEqual(sorted(fake.slot30()), sorted(
            f"https://arch.metroradio.hk/{f}/20260923/{f}_202609230830.mp3" for f in ("104", "997", "1044")))
        self.assertTrue(all(m == "HEAD" for m, _ in fake.urls))
        self.assertEqual(run["warnings"], [])
        self.assertEqual(run["metro_segment_sec"], {"104": 1791.5, "997": 1791.5, "1044": 1791.5})
        for f in ("104", "997", "1044"):
            self.assertEqual(window[f]["earliestDate"], "2026-06-01")
            self.assertEqual(window[f]["latestDate"], "2026-09-24")

    def test_canary_uses_latest_when_latest_is_already_a_past_day(self):
        fake = FakeMetro(today_open=False, missing_days={TODAY - dt.timedelta(days=1)})
        _, run, window = run_window(fake)
        self.assertEqual(window["104"]["latestDate"], "2026-09-22")
        self.assertIn("https://arch.metroradio.hk/104/20260922/104_202609220830.mp3", fake.slot30())
        self.assertEqual(len(fake.slot30()), 3)

    def test_length_off_by_more_than_5_percent_warns_but_never_blocks(self):
        fake = FakeMetro(slot30_bytes=7_166_070)              # ~15 min — the grid changed
        _, run, window = run_window(fake)
        self.assertEqual(run["metro_segment_sec"]["104"], 895.8)
        drift = [w for w in run["warnings"] if "grid drift?" in w]
        self.assertEqual(len(drift), 3)
        self.assertIn("~896s", drift[0])
        self.assertIn("expected 1800s", drift[0])
        self.assertEqual(set(window), {"104", "997", "1044"})  # window still recorded

    def test_tolerance_boundary(self):
        # 1890 s (exactly +5 %) passes; 1900 s (+5.6 %) warns; 1710 s (−5 %) passes.
        for nbytes, warns in ((15_120_000, False), (15_200_000, True), (13_680_000, False),
                              (13_600_000, True)):
            with self.subTest(nbytes=nbytes):
                _, run, _ = run_window(FakeMetro(slot30_bytes=nbytes))
                self.assertEqual(bool(run["warnings"]), warns, run["warnings"])

    def test_missing_30_slot_warns_grid_drift(self):
        fake = FakeMetro(slot30_status=404)
        _, run, window = run_window(fake)
        self.assertEqual(run["metro_segment_sec"], {"104": None, "997": None, "1044": None})
        self.assertEqual(len(run["warnings"]), 3)
        self.assertTrue(all("08:30 slot missing — grid drift?" in w for w in run["warnings"]))
        self.assertEqual(window["997"]["latestDate"], "2026-09-24")
        self.assertEqual(len(fake.slot30()), 3)                # a 404 is an answer: no retry

    def test_missing_content_length_warns_blind(self):
        _, run, _ = run_window(FakeMetro(slot30_bytes=None))
        self.assertEqual(len(run["warnings"]), 3)
        self.assertTrue(all("canary blind" in w for w in run["warnings"]))
        self.assertEqual(run["metro_segment_sec"]["1044"], None)

    def test_unreachable_frequency_runs_no_canary(self):
        fake = FakeMetro(today_open=False, missing_days={TODAY - dt.timedelta(days=d) for d in (1, 2)})
        _, run, window = run_window(fake)
        self.assertEqual(fake.slot30(), [])
        self.assertEqual(run["metro"]["104"], "unreachable")
        self.assertEqual(run["metro_segment_sec"], {})
        self.assertEqual(window, {})

    def test_canary_adds_exactly_three_heads_per_run(self):
        base = FakeMetro()
        client, _, _ = run_window(base)
        self.assertEqual(len(base.urls), client.requests)
        self.assertEqual(len(base.slot30()), 3)
        self.assertEqual(sum(1 for _, u in base.urls if not u.endswith("30.mp3")), client.requests - 3)


if __name__ == "__main__":
    unittest.main()
