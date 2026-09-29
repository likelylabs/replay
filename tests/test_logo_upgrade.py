"""S37b — an RTHK "_s" small-thumbnail logo publishes as its full-size photo:
crawl.full_logo_candidate derives the sibling, crawl.upgrade_small_logos
verifies it with ONE HEAD through the shared polite client and memoises the
answer (programmes.json `logoFull`, re-checked at most every 30 days, <= 41
HEADs a run), and build_catalog.published_logo publishes it. Every response is
mocked — nothing touches the network (REPLAY.md §2.4)."""
import contextlib
import copy
import datetime as dt
import http.client
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import build_catalog  # noqa: E402
import common  # noqa: E402
import crawl  # noqa: E402
from build_catalog import published_logo  # noqa: E402
from crawl import full_logo_candidate  # noqa: E402

TODAY = dt.date(2026, 9, 29)
BASE = "https://webstatic.rthk.hk/oldassets/images/rthk"
SMALL_RE = re.compile(r"_s\.jpe?g$", re.IGNORECASE)


def headers(**kv):
    m = http.client.HTTPMessage()
    for k, v in kv.items():
        m[k.replace("_", "-")] = v
    return m


class FakeResp:
    def __init__(self, status, hdrs, url):
        self.status, self.headers, self.url = status, hdrs, url

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeStatic:
    """webstatic.rthk.hk for HEADs: every full-size candidate answers 200
    image/jpeg unless `answers` says otherwise — an int status, "html" (a 200
    that is not an image), "redirect" (200 image from another URL) or "reset"
    (a transport error)."""

    def __init__(self, answers=None, length=423_679):
        self.answers, self.length = answers or {}, length
        self.reqs = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.reqs.append(req)
        a = self.answers.get(url, 200)
        if a == "reset":
            raise ConnectionResetError("reset by peer")
        if a == "html":
            return FakeResp(200, headers(Content_Type="text/html; charset=utf-8"), url)
        if a == "redirect":
            return FakeResp(200, headers(Content_Type="image/jpeg", Content_Length="1234"),
                            BASE + "/default.jpg")
        if a != 200:
            raise urllib.error.HTTPError(url, a, "x", headers(), None)
        return FakeResp(200, headers(Content_Type="image/jpeg", Content_Length=str(self.length)), url)

    def urls(self):
        return [r.full_url for r in self.reqs]


def prog(ch, slug, logo=None, memo=None):
    p = {"channel": ch, "slug": slug, "discoveredAt": "2026-09-02"}
    if logo:
        p["logo"] = logo
    if memo:
        p["logoFull"] = memo
    return p


def small(ch, slug, name="programme_photo"):
    return f"{BASE}/{ch}/{slug}/{name}_s.jpg"


class Harness(unittest.TestCase):
    """A temp data/rthk with one cached month per publishing programme."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rthk = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cache(self, *programmes, episodes=True):
        for p in programmes:
            common.write_json(self.rthk / p["channel"] / p["slug"] / "202609.json",
                              {"fetchedAt": "2026-09-29",
                               "episodes": [{"id": "1", "title": "", "date": "2026-09-28"}] if episodes else []})

    def upgrade(self, programmes, fake, only=None, cap=crawl.LOGO_PROBE_CAP, today=TODAY):
        client = common.Client(pace_s=0, budget=500, log=lambda *_: None)
        run = {"warnings": []}
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch.object(crawl, "RTHK_DATA", self.rthk), \
                mock.patch.object(crawl, "today_hkt", lambda: today), \
                mock.patch.object(crawl, "log", lambda *_: None):
            crawl.upgrade_small_logos(client, programmes, only, run, cap)
        return client, run


class Derivation(unittest.TestCase):
    def test_observed_patterns_drop_only_the_trailing_s_and_keep_the_extension(self):
        for small_url, full in (
                (f"{BASE}/pth/keepuco/programme_photo_s.jpg", f"{BASE}/pth/keepuco/programme_photo.jpg"),
                (f"{BASE}/radio3/dannylau/10622_1920_s.jpg", f"{BASE}/radio3/dannylau/10622_1920.jpg"),
                (f"{BASE}/radio4/Aubade/600_1920_s.jpg", f"{BASE}/radio4/Aubade/600_1920.jpg"),
                (f"{BASE}/radio1/x/programme_photo_s.jpeg", f"{BASE}/radio1/x/programme_photo.jpeg"),
                (f"{BASE}/radio1/x/PHOTO_S.JPG", f"{BASE}/radio1/x/PHOTO.JPG"),
                ("https://programme.rthk.hk/a/b/programme_photo_s.jpg",
                 "https://programme.rthk.hk/a/b/programme_photo.jpg")):
            with self.subTest(small=small_url):
                self.assertEqual(full_logo_candidate(small_url), full)

    def test_every_other_logo_is_left_alone(self):
        for url in (f"{BASE}/radio1/x/1234_115.jpg",                 # the 206 "{id}_115"
                    f"{BASE}/radio1/x/programme_photo.jpg",          # already full
                    f"{BASE}/radio1/x/programme_photo.jpeg",
                    f"{BASE}/radio1/x/programme_photo_m.jpg",        # hktoday's medium
                    f"{BASE}/radio1/x/x_programme_slider_1.jpg",
                    f"{BASE}/radio1/x/programme_photo_s.png",        # not a JPEG
                    f"{BASE}/radio1/x/programme_photo_s.jpg?v=2",    # query string
                    f"{BASE}/radio1/x/programme_photo_s.jpg#a",
                    f"{BASE}/radio1/x/_s.jpg",                       # nothing before "_s"
                    f"{BASE}/radio1/x_s.jpg/programme_photo.jpg",    # "_s" not at the end
                    f"{BASE}/radio1/x/programme_photo_ss.jpg",
                    "http://webstatic.rthk.hk/oldassets/images/rthk/radio1/x/programme_photo_s.jpg",
                    "https://example.com/radio1/x/programme_photo_s.jpg",
                    "https://evilrthk.hk/radio1/x/programme_photo_s.jpg",
                    "", None, 42):
            with self.subTest(url=url):
                self.assertIsNone(full_logo_candidate(url))

    def test_committed_registry_upgrades_exactly_the_s_logos_on_their_own_path(self):
        programmes = common.read_json(common.PROGRAMMES_PATH, {}) or {}
        if not programmes:
            self.skipTest("no committed data/rthk/programmes.json")
        logos = [p["logo"] for p in programmes.values() if p.get("logo")]
        smalls = [u for u in logos if SMALL_RE.search(u)]
        self.assertTrue(smalls)
        for u in logos:
            cand = full_logo_candidate(u)
            if u in smalls:
                self.assertIsNotNone(cand, u)
                self.assertEqual(cand.rsplit("/", 1)[0], u.rsplit("/", 1)[0])
                self.assertEqual(cand.rsplit(".", 1)[1], u.rsplit(".", 1)[1])
            else:
                self.assertIsNone(cand, u)                          # the full ones: never requested

    def test_constants_match_the_ruling(self):
        self.assertEqual(crawl.LOGO_RECHECK_DAYS, 30)
        self.assertEqual(crawl.LOGO_PROBE_CAP, 41)
        self.assertEqual(crawl.LOGO_PROBE_MAX_FAILURES, 3)


class Probe(Harness):
    def test_one_polite_head_per_small_logo_and_the_memo(self):
        a = prog("pth", "keepuco", small("pth", "keepuco"))
        b = prog("radio3", "dannylau", small("radio3", "dannylau", "10622_1920"))
        full = prog("radio1", "Free_as_the_wind", f"{BASE}/radio1/Free_as_the_wind/1_115.jpg")
        self.cache(a, b, full)
        programmes = {"pth/keepuco": a, "radio3/dannylau": b, "radio1/Free_as_the_wind": full}
        fake = FakeStatic()
        client, run = self.upgrade(programmes, fake)
        self.assertEqual(fake.urls(), [f"{BASE}/pth/keepuco/programme_photo.jpg",
                                       f"{BASE}/radio3/dannylau/10622_1920.jpg"])
        self.assertEqual(client.requests, 2)
        for req in fake.reqs:
            self.assertEqual(req.get_method(), "HEAD")
            self.assertEqual(req.get_header("User-agent"), common.UA)
            self.assertIn("zh-HK", req.get_header("Accept-language"))
        self.assertEqual(fake.reqs[0].get_header("Referer"), "https://www.rthk.hk/radio/pth/programme/keepuco")
        self.assertEqual(a["logoFull"], {"from": small("pth", "keepuco"),
                                         "url": f"{BASE}/pth/keepuco/programme_photo.jpg",
                                         "status": 200, "bytes": 423_679, "checkedAt": "2026-09-29"})
        self.assertEqual(a["logo"], small("pth", "keepuco"))       # the raw pick is never rewritten
        self.assertNotIn("logoFull", full)
        self.assertEqual(full["logo"], f"{BASE}/radio1/Free_as_the_wind/1_115.jpg")
        self.assertEqual((run["logo_small"], run["logo_probes"], run["logo_full"], run["logo_deferred"]),
                         (2, 2, 2, 0))
        self.assertEqual(run["warnings"], [])

    def test_uses_the_shared_clients_pacing(self):
        a = prog("pth", "a", small("pth", "a"))
        b = prog("pth", "b", small("pth", "b"))
        self.cache(a, b)
        client = common.Client(pace_s=1.0, budget=10, log=lambda *_: None)
        paced = []
        with mock.patch("urllib.request.urlopen", FakeStatic()), \
                mock.patch.object(crawl, "RTHK_DATA", self.rthk), \
                mock.patch.object(crawl, "today_hkt", lambda: TODAY), \
                mock.patch.object(crawl, "log", lambda *_: None), \
                mock.patch.object(common.time, "sleep", paced.append):
            crawl.upgrade_small_logos(client, {"pth/a": a, "pth/b": b}, None, {"warnings": []})
        self.assertEqual(client.requests, 2)
        self.assertEqual(len(paced), 1)                             # the 2nd HEAD waited its turn
        self.assertGreater(paced[0], 0.9)

    def test_memo_suppresses_daily_reprobes_until_30_days(self):
        a = prog("pth", "keepuco", small("pth", "keepuco"))
        self.cache(a)
        programmes = {"pth/keepuco": a}
        self.upgrade(programmes, FakeStatic())
        for days, probed in ((1, False), (29, False), (30, True)):
            with self.subTest(days=days):
                fake = FakeStatic()
                _, run = self.upgrade(programmes, fake, today=TODAY + dt.timedelta(days=days))
                self.assertEqual(bool(fake.reqs), probed)
                self.assertEqual(run["logo_full"], 1)
        self.assertEqual(a["logoFull"]["checkedAt"], "2026-10-29")

    def test_a_changed_logo_retires_the_memo(self):
        a = prog("pth", "keepuco", small("pth", "keepuco"),
                 {"from": small("pth", "old"), "url": f"{BASE}/pth/old/programme_photo.jpg",
                  "status": 200, "checkedAt": "2026-09-28"})
        self.cache(a)
        fake = FakeStatic()
        self.upgrade({"pth/keepuco": a}, fake)
        self.assertEqual(fake.urls(), [f"{BASE}/pth/keepuco/programme_photo.jpg"])
        self.assertEqual(a["logoFull"]["from"], small("pth", "keepuco"))

    def test_logo_moved_off_s_drops_the_memo_without_a_request(self):
        a = prog("pth", "keepuco", f"{BASE}/pth/keepuco/9_115.jpg",
                 {"from": small("pth", "keepuco"), "url": f"{BASE}/pth/keepuco/programme_photo.jpg",
                  "status": 200, "checkedAt": "2026-09-28"})
        self.cache(a)
        fake = FakeStatic()
        self.upgrade({"pth/keepuco": a}, fake)
        self.assertEqual(fake.reqs, [])
        self.assertNotIn("logoFull", a)
        self.assertEqual(a["logo"], f"{BASE}/pth/keepuco/9_115.jpg")

    def test_definitive_misses_are_memoised_and_keep_the_small_logo(self):
        cases = {"n404": 404, "n403": 403, "n410": 410, "html": "html", "redir": "redirect"}
        programmes = {f"pth/{s}": prog("pth", s, small("pth", s)) for s in cases}
        self.cache(*programmes.values())
        fake = FakeStatic({f"{BASE}/pth/{s}/programme_photo.jpg": a for s, a in cases.items()})
        _, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 5)
        for s, a in cases.items():
            with self.subTest(case=s):
                memo = programmes[f"pth/{s}"]["logoFull"]
                self.assertIsNone(memo["url"])
                self.assertEqual(memo["status"], a if isinstance(a, int) else 200)
                self.assertNotIn("bytes", memo)
        self.assertEqual(run["logo_full"], 0)
        # ...and those answers are not asked again tomorrow.
        fake2 = FakeStatic()
        self.upgrade(programmes, fake2, today=TODAY + dt.timedelta(days=1))
        self.assertEqual(fake2.reqs, [])

    def test_no_answer_keeps_the_previous_memo_and_retries_next_run_without_retrying_now(self):
        prev = {"from": small("pth", "a"), "url": f"{BASE}/pth/a/programme_photo.jpg",
                "status": 200, "bytes": 5, "checkedAt": "2026-08-20"}          # 40 d old: due
        a = prog("pth", "a", small("pth", "a"), dict(prev))
        b = prog("pth", "b", small("pth", "b"))
        self.cache(a, b)
        fake = FakeStatic({f"{BASE}/pth/a/programme_photo.jpg": 503,
                           f"{BASE}/pth/b/programme_photo.jpg": "reset"})
        client, run = self.upgrade({"pth/a": a, "pth/b": b}, fake)
        self.assertEqual(len(fake.reqs), 2)                        # ONE HEAD each: tries=1
        self.assertEqual(client.failures, 2)
        self.assertEqual(a["logoFull"], prev)                      # the full-size answer stands
        self.assertNotIn("logoFull", b)
        self.assertEqual(run["logo_full"], 1)
        fake2 = FakeStatic()
        self.upgrade({"pth/a": a, "pth/b": b}, fake2, today=TODAY + dt.timedelta(days=1))
        self.assertEqual(len(fake2.reqs), 2)

    def test_stops_after_three_failed_heads(self):
        programmes = {f"radio1/p{i:02d}": prog("radio1", f"p{i:02d}", small("radio1", f"p{i:02d}"))
                      for i in range(10)}
        self.cache(*programmes.values())
        fake = FakeStatic({f"{BASE}/radio1/p{i:02d}/programme_photo.jpg": 403 for i in range(10)})
        client, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 3)
        self.assertEqual(client.failures, 3)
        self.assertEqual(run["logo_probes"], 3)
        self.assertEqual(run["logo_deferred"], 7)
        self.assertEqual(len(run["warnings"]), 1)
        self.assertIn("stopped after 3 failed HEADs", run["warnings"][0])

    def test_404s_are_answers_not_failures(self):
        programmes = {f"radio1/p{i:02d}": prog("radio1", f"p{i:02d}", small("radio1", f"p{i:02d}"))
                      for i in range(10)}
        self.cache(*programmes.values())
        fake = FakeStatic({f"{BASE}/radio1/p{i:02d}/programme_photo.jpg": 404 for i in range(10)})
        client, run = self.upgrade(programmes, fake)
        self.assertEqual((len(fake.reqs), client.failures, run["warnings"]), (10, 0, []))

    def test_never_more_than_41_heads_a_run_never_checked_first(self):
        programmes = {}
        for i in range(50):
            p = prog("radio2", f"p{i:02d}", small("radio2", f"p{i:02d}"))
            if i < 20:                                                 # stale answers (due)
                p["logoFull"] = {"from": p["logo"], "url": None, "status": 404,
                                 "checkedAt": (TODAY - dt.timedelta(days=60 - i)).isoformat()}
            programmes[f"radio2/p{i:02d}"] = p
        self.cache(*programmes.values())
        fake = FakeStatic()
        _, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 41)
        probed = [u.split("/")[-2] for u in fake.urls()]
        self.assertEqual(probed[:30], [f"p{i:02d}" for i in range(20, 50)])   # never-checked
        self.assertEqual(probed[30:], [f"p{i:02d}" for i in range(11)])      # then the oldest
        self.assertEqual((run["logo_probes"], run["logo_deferred"]), (41, 9))
        fake2 = FakeStatic()
        self.upgrade(programmes, fake2, today=TODAY + dt.timedelta(days=1))
        self.assertEqual(len(fake2.reqs), 9)

    def test_cap_argument_bounds_a_dev_run(self):
        programmes = {f"pth/p{i}": prog("pth", f"p{i}", small("pth", f"p{i}")) for i in range(6)}
        self.cache(*programmes.values())
        fake = FakeStatic()
        self.upgrade(programmes, fake, cap=3)
        self.assertEqual(len(fake.reqs), 3)

    def test_dormant_and_filtered_programmes_are_not_requested(self):
        live = prog("radio1", "live", small("radio1", "live"))
        dormant = prog("pth", "2024debate", small("pth", "2024debate"))
        empty = prog("radio3", "book_club", small("radio3", "book_club"))
        other = prog("radio4", "Aubade", small("radio4", "Aubade", "600_1920"))
        self.cache(live, other)
        self.cache(empty, episodes=False)                           # cached month, zero episodes
        programmes = {"radio1/live": live, "pth/2024debate": dormant,
                      "radio3/book_club": empty, "radio4/Aubade": other}
        fake = FakeStatic()
        _, run = self.upgrade(programmes, fake, only={"radio1", "pth", "radio3"})
        self.assertEqual(fake.urls(), [f"{BASE}/radio1/live/programme_photo.jpg"])
        self.assertEqual(run["logo_small"], 1)
        self.assertNotIn("logoFull", other)

    def test_budget_is_respected(self):
        a = prog("pth", "a", small("pth", "a"))
        self.cache(a)
        client = common.Client(pace_s=0, budget=0, log=lambda *_: None)
        with mock.patch("urllib.request.urlopen", FakeStatic()), \
                mock.patch.object(crawl, "RTHK_DATA", self.rthk), \
                mock.patch.object(crawl, "today_hkt", lambda: TODAY), \
                self.assertRaises(common.BudgetExhausted):
            crawl.upgrade_small_logos(client, {"pth/a": a}, None, {"warnings": []})


class ClientLastUrl(unittest.TestCase):
    def test_final_url_recorded_and_reset(self):
        c = common.Client(pace_s=0, budget=10, log=lambda *_: None)
        with mock.patch("urllib.request.urlopen", FakeStatic({"https://x.test/r.jpg": "redirect"})):
            c.get("https://x.test/a.jpg", method="HEAD")
            self.assertEqual(c.last_url, "https://x.test/a.jpg")
            c.get("https://x.test/r.jpg", method="HEAD")
            self.assertEqual(c.last_url, BASE + "/default.jpg")
        with mock.patch("urllib.request.urlopen", FakeStatic({"https://x.test/n.jpg": 404})):
            c.get("https://x.test/n.jpg", method="HEAD")
        self.assertEqual(c.last_url, "")


class Publish(unittest.TestCase):
    SMALL = f"{BASE}/pth/keepuco/programme_photo_s.jpg"
    FULL = f"{BASE}/pth/keepuco/programme_photo.jpg"

    def test_verified_full_size_wins(self):
        p = {"logo": self.SMALL, "logoFull": {"from": self.SMALL, "url": self.FULL, "status": 200}}
        self.assertEqual(published_logo(p), self.FULL)

    def test_falls_back_to_the_crawlers_pick(self):
        for memo in (None, {}, {"from": self.SMALL, "url": None, "status": 404},
                     {"from": "https://other/x_s.jpg", "url": self.FULL},     # logo moved on
                     {"from": self.SMALL, "url": "http://insecure/x.jpg"},
                     {"from": self.SMALL, "url": 7}):
            with self.subTest(memo=memo):
                p = {"logo": self.SMALL}
                if memo is not None:
                    p["logoFull"] = memo
                self.assertEqual(published_logo(p), self.SMALL)

    def test_no_logo_stays_none(self):
        self.assertIsNone(published_logo({}))
        self.assertIsNone(published_logo({"logoFull": {"from": None, "url": self.FULL}}))


class PublishedCatalog(unittest.TestCase):
    """Build the committed data/ into a temp dir with one "_s" programme's memo
    set: its logo publishes full-size, every other logo is byte-identical."""

    def build(self, programmes, tmp):
        (tmp / "last-run.json").write_text("{}", encoding="utf-8")
        (tmp / "programmes.json").write_text(json.dumps(programmes, ensure_ascii=False), encoding="utf-8")
        run = common.read_json(common.LAST_RUN_PATH, {}) or {}
        today = dt.date.fromisoformat((run.get("startedAt") or TODAY.isoformat())[:10])
        out = io.StringIO()
        with mock.patch.object(build_catalog, "INDEX_PATH", tmp / "index.json"), \
                mock.patch.object(build_catalog, "PROG_DIR", tmp / "prog"), \
                mock.patch.object(build_catalog, "LAST_RUN_PATH", tmp / "last-run.json"), \
                mock.patch.object(build_catalog, "PROGRAMMES_PATH", tmp / "programmes.json"), \
                mock.patch.object(build_catalog, "today_hkt", lambda: today), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = build_catalog.main()
        self.assertEqual(rc, 0, out.getvalue())
        return {(c["id"], p["slug"]): p.get("logo") for c in json.loads((tmp / "index.json").read_text("utf-8"))["channels"]
                if c["source"] == "rthk" for p in c["programmes"]}

    def test_memo_publishes_full_size_and_nothing_else_moves(self):
        programmes = common.read_json(common.PROGRAMMES_PATH, {}) or {}
        if not programmes:
            self.skipTest("no committed data/rthk/programmes.json")
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            before = self.build(programmes, Path(a))
            target = next((k for k, v in sorted(before.items()) if v and SMALL_RE.search(v)), None)
            if not target:
                self.skipTest("no published _s logo in the committed data")
            memoed = copy.deepcopy(programmes)
            p = memoed[f"{target[0]}/{target[1]}"]
            full = full_logo_candidate(p["logo"])
            p["logoFull"] = {"from": p["logo"], "url": full, "status": 200, "checkedAt": "2026-09-29"}
            after = self.build(memoed, Path(b))
        self.assertEqual(after[target], full)
        self.assertEqual({k: v for k, v in after.items() if k != target},
                         {k: v for k, v in before.items() if k != target})


if __name__ == "__main__":
    unittest.main()
