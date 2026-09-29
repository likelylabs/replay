"""S37b/S37c — an RTHK small logo ("{id}_115.jpg" or an "_s" thumbnail)
publishes as a larger square artwork from its own directory — the 720 px
rendition first, the original only where that is absent and <= 500 KB:
crawl.logo_candidates derives the ordered candidates per family,
crawl.upgrade_small_logos HEADs them in order through the shared polite client
and memoises the answer — positive or "nothing larger" — in programmes.json
`logoFull` (re-checked at most every 30 days, <= 60 programmes a run, stops
after 3 failed HEADs, fails soft; "nothing larger" only with a control HEAD
on the small logo, a second run before a published upgrade is withdrawn, and
a stop after 5 all-miss programmes in a row), and build_catalog.published_logo
publishes it with the schema unchanged. Every response is mocked — nothing touches the
network (REPLAY.md §2.4)."""
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
from crawl import logo_candidates, logo_head_verdict  # noqa: E402

try:
    import jsonschema
except ImportError:          # the repo is stdlib-only; the Action never runs these
    jsonschema = None

TODAY = dt.date(2026, 9, 29)
BASE = "https://webstatic.rthk.hk/oldassets/images/rthk"
SMALL_RE = re.compile(r"(^|/)(\d+_115|[^/]+_s)\.jpe?g$", re.IGNORECASE)
INDEX_SCHEMA = json.loads((REPO / "schema" / "index.schema.json").read_text(encoding="utf-8"))


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
    """webstatic.rthk.hk for HEADs: every URL answers 200 image/jpeg of
    `length` bytes unless `answers` says otherwise — an int status (no body
    type), "missing" (403 application/xml: how the storage answers a missing
    object, measured 2026-09-29), "blocked" (403 text/html), "html" (a 200
    that is not an image), "redirect" (200 image from another URL), "nolen"
    (200 image without a Content-Length), ("bytes", n) (200 image of n bytes)
    or "reset" (a transport error)."""

    def __init__(self, answers=None, length=69_275):
        self.answers, self.length = answers or {}, length
        self.reqs = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.reqs.append(req)
        a = self.answers.get(url, 200)
        if a == "reset":
            raise ConnectionResetError("reset by peer")
        if a == "missing":
            raise urllib.error.HTTPError(url, 403, "Forbidden", headers(Content_Type="application/xml"), None)
        if a == "blocked":
            raise urllib.error.HTTPError(url, 403, "Forbidden", headers(Content_Type="text/html"), None)
        if a == "html":
            return FakeResp(200, headers(Content_Type="text/html; charset=utf-8"), url)
        if a == "redirect":
            return FakeResp(200, headers(Content_Type="image/jpeg", Content_Length="1234"),
                            BASE + "/default.jpg")
        if a == "nolen":
            return FakeResp(200, headers(Content_Type="image/jpeg"), url)
        if isinstance(a, tuple):
            return FakeResp(200, headers(Content_Type="image/jpeg", Content_Length=str(a[1])), url)
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


def d(ch, slug):
    return f"{BASE}/{ch}/{slug}"


def s115(ch, slug, pid="1234"):
    return f"{d(ch, slug)}/{pid}_115.jpg"


def small(ch, slug, name="programme_photo"):
    return f"{d(ch, slug)}/{name}_s.jpg"


def photo(ch, slug):
    return f"{d(ch, slug)}/programme_photo.jpg"


def photo_l(ch, slug):
    return f"{d(ch, slug)}/programme_photo_l.jpg"


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

    def upgrade(self, programmes, fake, only=None, cap=crawl.LOGO_PROGRAMMES_PER_RUN, today=TODAY, client=None):
        client = client or common.Client(pace_s=0, budget=500, log=lambda *_: None)
        run = {"warnings": []}
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch.object(crawl, "RTHK_DATA", self.rthk), \
                mock.patch.object(crawl, "today_hkt", lambda: today), \
                mock.patch.object(crawl, "log", lambda *_: None):
            crawl.upgrade_small_logos(client, programmes, only, run, cap)
        return client, run


class Derivation(unittest.TestCase):
    def test_the_115_family_tries_the_720_rendition_then_the_square_original(self):
        for logo in (f"{BASE}/radio1/whatsupbro/11215_115.jpg",
                     f"{BASE}/radio1/whatsupbro/11215_115.jpeg",
                     f"{BASE}/radio1/whatsupbro/11215_115.JPG"):
            with self.subTest(logo=logo):
                self.assertEqual(logo_candidates(logo), [f"{BASE}/radio1/whatsupbro/programme_photo_l.jpg",
                                                         f"{BASE}/radio1/whatsupbro/programme_photo.jpg"])

    def test_the_s_family_adds_its_s37b_sibling_last_unless_it_is_the_original(self):
        cases = (
            (f"{BASE}/pth/keepuco/programme_photo_s.jpg",
             [f"{BASE}/pth/keepuco/programme_photo_l.jpg", f"{BASE}/pth/keepuco/programme_photo.jpg"]),
            (f"{BASE}/radio3/dannylau/10622_1920_s.jpg",
             [f"{BASE}/radio3/dannylau/programme_photo_l.jpg", f"{BASE}/radio3/dannylau/programme_photo.jpg",
              f"{BASE}/radio3/dannylau/10622_1920.jpg"]),
            (f"{BASE}/radio1/x/programme_photo_s.jpeg",
             [f"{BASE}/radio1/x/programme_photo_l.jpg", f"{BASE}/radio1/x/programme_photo.jpg",
              f"{BASE}/radio1/x/programme_photo.jpeg"]),
            (f"{BASE}/radio1/x/PHOTO_S.JPG",
             [f"{BASE}/radio1/x/programme_photo_l.jpg", f"{BASE}/radio1/x/programme_photo.jpg",
              f"{BASE}/radio1/x/PHOTO.JPG"]),
            ("https://programme.rthk.hk/a/b/programme_photo_s.jpg",
             ["https://programme.rthk.hk/a/b/programme_photo_l.jpg",
              "https://programme.rthk.hk/a/b/programme_photo.jpg"]))
        for logo, cands in cases:
            with self.subTest(logo=logo):
                self.assertEqual(logo_candidates(logo), cands)

    def test_every_other_logo_is_left_alone(self):
        for url in (f"{BASE}/radio1/x/programme_photo.jpg",          # already the original
                    f"{BASE}/radio1/x/programme_photo.jpeg",
                    f"{BASE}/radio1/x/programme_photo_l.jpg",        # already 720 px
                    f"{BASE}/radio1/x/programme_photo_m.jpg",        # hktoday's 432 px medium
                    f"{BASE}/radio1/x/1234_1920.jpg",
                    "https://webstatic.rthk.hk/assets/images/programme/radio2/zturday/12523_programme_slider_570926.jpg",
                    f"{BASE}/radio1/x/1234_80.jpg",                  # another size, not in the catalog
                    f"{BASE}/radio1/x/abc_115.jpg",                  # not an RTHK id
                    f"{BASE}/radio1/x/1234_1150.jpg",
                    f"{BASE}/radio1/x/1234_115.png",                 # not a JPEG
                    f"{BASE}/radio1/x/programme_photo_s.png",
                    f"{BASE}/radio1/x/1234_115.jpg?v=2",             # query string
                    f"{BASE}/radio1/x/programme_photo_s.jpg#a",
                    f"{BASE}/radio1/x/_s.jpg",                       # nothing before "_s"
                    f"{BASE}/radio1/x_s.jpg/programme_photo.jpg",    # "_s" not in the file name
                    f"{BASE}/radio1/x/programme_photo_ss.jpg",
                    "http://webstatic.rthk.hk/oldassets/images/rthk/radio1/x/1234_115.jpg",
                    "https://example.com/radio1/x/1234_115.jpg",
                    "https://evilrthk.hk/radio1/x/programme_photo_s.jpg",
                    "", None, 42):
            with self.subTest(url=url):
                self.assertEqual(logo_candidates(url), [])

    def test_committed_registry_derives_candidates_for_exactly_the_small_logos(self):
        programmes = common.read_json(common.PROGRAMMES_PATH, {}) or {}
        if not programmes:
            self.skipTest("no committed data/rthk/programmes.json")
        logos = [p["logo"] for p in programmes.values() if p.get("logo")]
        smalls = [u for u in logos if SMALL_RE.search(u)]
        self.assertTrue(any("_115." in u for u in smalls) and any("_s." in u for u in smalls))
        for u in logos:
            cands = logo_candidates(u)
            if u in smalls:
                folder = u.rsplit("/", 1)[0]
                self.assertEqual(cands[:2], [f"{folder}/programme_photo_l.jpg", f"{folder}/programme_photo.jpg"], u)
                self.assertTrue(all(c.rsplit("/", 1)[0] == folder for c in cands), u)
                self.assertNotIn(u, cands)
            else:
                self.assertEqual(cands, [], u)                      # everything else: never requested

    def test_constants_match_the_ruling(self):
        self.assertEqual(crawl.LOGO_RECHECK_DAYS, 30)
        self.assertEqual(crawl.LOGO_PROGRAMMES_PER_RUN, 60)
        self.assertEqual(crawl.LOGO_PROBE_MAX_FAILURES, 3)
        self.assertEqual(crawl.LOGO_MAX_BYTES, 500_000)


class Verdict(unittest.TestCase):
    def test_one_heads_meaning(self):
        cap = crawl.LOGO_MAX_BYTES
        for args, want in (
                ((200, "image/jpeg", True, 185_340), "hit"),
                ((200, "image/jpeg", True, cap), "hit"),
                ((200, "IMAGE/JPEG; x=y", True, 1), "hit"),
                ((200, "image/jpeg", True, cap + 1), "miss"),      # too heavy for a tile
                ((200, "image/jpeg", True, 0), "miss"),            # size unknown
                ((200, "image/jpeg", False, 1000), "miss"),        # a redirect
                ((200, "text/html", True, 1000), "miss"),
                ((403, "application/xml", False, 0), "miss"),      # the storage's "missing"
                ((403, "application/xml; charset=utf-8", False, 0), "miss"),
                ((404, "", False, 0), "miss"),
                ((410, "", False, 0), "miss"),
                ((400, "", False, 0), "miss"),
                ((403, "text/html", False, 0), "none"),            # a block is not an answer
                ((403, "", False, 0), "none"),
                ((429, "", False, 0), "none"),
                ((500, "", False, 0), "none"),
                ((503, "", False, 0), "none"),
                ((-1, "ConnectionResetError()", False, 0), "none")):
            with self.subTest(args=args):
                self.assertEqual(logo_head_verdict(*args), want)


class Probe(Harness):
    def test_one_polite_head_per_programme_when_the_720_rendition_is_usable(self):
        a = prog("radio1", "whatsupbro", s115("radio1", "whatsupbro", "11215"))
        b = prog("pth", "keepuco", small("pth", "keepuco"))
        full = prog("radio3", "hongkongtoday", photo("radio3", "hongkongtoday"))
        self.cache(a, b, full)
        programmes = {"radio1/whatsupbro": a, "pth/keepuco": b, "radio3/hongkongtoday": full}
        fake = FakeStatic()
        client, run = self.upgrade(programmes, fake)
        self.assertEqual(fake.urls(), [photo_l("pth", "keepuco"), photo_l("radio1", "whatsupbro")])
        self.assertEqual(client.requests, 2)
        for req in fake.reqs:
            self.assertEqual(req.get_method(), "HEAD")
            self.assertEqual(req.get_header("User-agent"), common.UA)
            self.assertIn("zh-HK", req.get_header("Accept-language"))
        self.assertEqual(fake.reqs[1].get_header("Referer"), "https://www.rthk.hk/radio/radio1/programme/whatsupbro")
        self.assertEqual(a["logoFull"], {"from": s115("radio1", "whatsupbro", "11215"),
                                         "url": photo_l("radio1", "whatsupbro"),
                                         "status": 200, "bytes": 69_275, "checkedAt": "2026-09-29"})
        self.assertEqual(b["logoFull"]["url"], photo_l("pth", "keepuco"))
        self.assertEqual(a["logo"], s115("radio1", "whatsupbro", "11215"))   # the raw pick is never rewritten
        self.assertNotIn("logoFull", full)
        self.assertEqual((run["logo_small"], run["logo_checked"], run["logo_probes"],
                          run["logo_upgraded"], run["logo_deferred"]), (2, 2, 2, 2, 0))
        self.assertEqual(run["warnings"], [])

    def test_uses_the_shared_clients_pacing(self):
        a = prog("pth", "a", s115("pth", "a"))
        b = prog("pth", "b", small("pth", "b"))
        self.cache(a, b)
        client = common.Client(pace_s=1.0, budget=10, log=lambda *_: None)
        paced = []
        with mock.patch("urllib.request.urlopen", FakeStatic({photo_l("pth", "a"): "missing"})), \
                mock.patch.object(crawl, "RTHK_DATA", self.rthk), \
                mock.patch.object(crawl, "today_hkt", lambda: TODAY), \
                mock.patch.object(crawl, "log", lambda *_: None), \
                mock.patch.object(common.time, "sleep", paced.append):
            crawl.upgrade_small_logos(client, {"pth/a": a, "pth/b": b}, None, {"warnings": []})
        self.assertEqual(client.requests, 3)
        # Every HEAD after the first waited its turn. (The first may wait too:
        # the client's clock starts at 0 and monotonic() can be < 1 s into a
        # fresh process, so only the last two waits are pinned.)
        self.assertIn(len(paced), (2, 3))
        self.assertTrue(all(w > 0.9 for w in paced[-2:]))

    def test_without_a_usable_720_rendition_a_light_original_is_taken(self):
        cases = {"heavy": ("bytes", crawl.LOGO_MAX_BYTES + 1), "missing": "missing", "nolen": "nolen",
                 "html": "html", "redir": "redirect", "gone": 404}
        programmes = {f"radio2/{s}": prog("radio2", s, s115("radio2", s)) for s in cases}
        self.cache(*programmes.values())
        answers = {}
        for s, a in cases.items():
            answers[photo_l("radio2", s)] = a
            answers[photo("radio2", s)] = ("bytes", 185_340)
        fake = FakeStatic(answers)
        client, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 2 * len(cases))
        for s in cases:
            with self.subTest(case=s):
                self.assertEqual(programmes[f"radio2/{s}"]["logoFull"],
                                 {"from": s115("radio2", s), "url": photo("radio2", s), "status": 200,
                                  "bytes": 185_340, "checkedAt": "2026-09-29"})
        self.assertEqual((client.failures, run["logo_upgraded"], run["warnings"]), (0, len(cases), []))

    def test_an_original_over_the_cap_is_never_taken(self):
        # 18 of the 31 sampled originals are over 2 MB and they run to 19.4 MB:
        # decoded at full size a 3001 px original is ~36 MB per tile.
        for size in (crawl.LOGO_MAX_BYTES + 1, 2_000_000, 19_398_471):
            with self.subTest(size=size):
                a = prog("radio1", "big", s115("radio1", "big"))
                self.cache(a)
                fake = FakeStatic({photo_l("radio1", "big"): "missing", photo("radio1", "big"): ("bytes", size)})
                self.upgrade({"radio1/big": a}, fake)
                self.assertEqual(fake.urls(), [photo_l("radio1", "big"), photo("radio1", "big"),
                                               s115("radio1", "big")])            # + the control
                self.assertIsNone(a["logoFull"]["url"])
                self.assertEqual(published_logo(a), s115("radio1", "big"))

    def test_a_memo_an_earlier_rule_wrote_is_asked_again_first_and_never_published(self):
        # bfa1afb5 accepted originals up to 2 MB: such a memo is retired by
        # today's cap — not published, and due ahead of every other memo.
        old = {"from": s115("pth", "z_old"), "url": photo("pth", "z_old"), "status": 200,
               "bytes": 1_900_000, "checkedAt": TODAY.isoformat()}
        a = prog("pth", "z_old", s115("pth", "z_old"), dict(old))
        stale = prog("pth", "a_stale", s115("pth", "a_stale"),
                     {"from": s115("pth", "a_stale"), "url": None, "status": 403, "checkedAt": "2026-08-01"})
        self.cache(a, stale)
        self.assertEqual(published_logo(a), s115("pth", "z_old"))
        fake = FakeStatic()
        self.upgrade({"pth/z_old": a, "pth/a_stale": stale}, fake, cap=1)
        self.assertEqual(fake.urls(), [photo_l("pth", "z_old")])       # ahead of a 59-day-old memo
        self.assertEqual(a["logoFull"]["url"], photo_l("pth", "z_old"))
        self.assertEqual(published_logo(a), photo_l("pth", "z_old"))

    def test_a_1920_s_thumbnail_takes_its_banner_only_when_no_square_exists(self):
        ch, slug = "radio4", "Aubade"
        a = prog(ch, slug, small(ch, slug, "600_1920"))
        self.cache(a)
        fake = FakeStatic({photo(ch, slug): "missing", photo_l(ch, slug): "missing"})
        self.upgrade({f"{ch}/{slug}": a}, fake)
        self.assertEqual(fake.urls(), [photo_l(ch, slug), photo(ch, slug), f"{d(ch, slug)}/600_1920.jpg"])
        self.assertEqual(a["logoFull"]["url"], f"{d(ch, slug)}/600_1920.jpg")
        b = prog(ch, "JazzingUp", small(ch, "JazzingUp", "2628_1920"))
        self.cache(b)
        fake = FakeStatic()
        self.upgrade({f"{ch}/JazzingUp": b}, fake)
        self.assertEqual(fake.urls(), [photo_l(ch, "JazzingUp")])   # the 720 px square wins
        self.assertEqual(b["logoFull"]["url"], photo_l(ch, "JazzingUp"))

    def test_nothing_larger_is_a_negative_memo_asked_again_after_30_days(self):
        ch, slug = "radio3", "thisday"
        a = prog(ch, slug, s115(ch, slug, "11396"))
        self.cache(a)
        programmes = {f"{ch}/{slug}": a}
        missing = {photo(ch, slug): "missing", photo_l(ch, slug): "missing"}
        fake = FakeStatic(missing)
        client, run = self.upgrade(programmes, fake)
        self.assertEqual(fake.urls(), [photo_l(ch, slug), photo(ch, slug), s115(ch, slug, "11396")])
        self.assertEqual(a["logoFull"], {"from": s115(ch, slug, "11396"), "url": None, "status": 403,
                                         "checkedAt": "2026-09-29"})
        self.assertEqual((client.failures, run["logo_upgraded"], run["logo_deferred"]), (0, 0, 0))
        for days, heads in ((1, 0), (29, 0), (30, 3)):
            with self.subTest(days=days):
                fake = FakeStatic(missing)
                self.upgrade(programmes, fake, today=TODAY + dt.timedelta(days=days))
                self.assertEqual(len(fake.reqs), heads)
        self.assertEqual(a["logoFull"]["checkedAt"], "2026-10-29")
        self.assertIsNone(a["logoFull"]["url"])

    def test_every_definitive_miss_is_remembered(self):
        cases = {"n404": 404, "n410": 410, "n400": 400, "xml403": "missing", "html": "html",
                 "redir": "redirect", "heavy": ("bytes", crawl.LOGO_MAX_BYTES + 1)}
        for s, a in cases.items():
            with self.subTest(case=s):
                p = prog("pth", s, s115("pth", s))
                self.cache(p)
                programmes = {f"pth/{s}": p}
                fake = FakeStatic({photo_l("pth", s): a, photo("pth", s): a})
                client, run = self.upgrade(programmes, fake)
                self.assertEqual(fake.urls(), [photo_l("pth", s), photo("pth", s), s115("pth", s)])
                memo = p["logoFull"]
                self.assertIsNone(memo["url"])
                self.assertEqual(memo["status"], a if isinstance(a, int) else 403 if a == "missing" else 200)
                self.assertNotIn("bytes", memo)
                self.assertEqual(run["logo_upgraded"], 0)
                self.assertEqual(client.failures, 2 if a == 400 else 0)   # only a 400 counts against the run
                fake2 = FakeStatic()
                self.upgrade(programmes, fake2, today=TODAY + dt.timedelta(days=1))
                self.assertEqual(fake2.reqs, [])

    def test_memo_suppresses_daily_reprobes_until_30_days(self):
        a = prog("pth", "keepuco", s115("pth", "keepuco"))
        self.cache(a)
        programmes = {"pth/keepuco": a}
        self.upgrade(programmes, FakeStatic())
        for days, probed in ((1, False), (29, False), (30, True)):
            with self.subTest(days=days):
                fake = FakeStatic()
                _, run = self.upgrade(programmes, fake, today=TODAY + dt.timedelta(days=days))
                self.assertEqual(bool(fake.reqs), probed)
                self.assertEqual(run["logo_upgraded"], 1)
        self.assertEqual(a["logoFull"]["checkedAt"], "2026-10-29")

    def test_a_malformed_memo_date_is_due(self):
        a = prog("pth", "a", s115("pth", "a"), {"from": s115("pth", "a"), "url": None, "status": 403,
                                                 "checkedAt": "not-a-date"})
        self.cache(a)
        fake = FakeStatic()
        self.upgrade({"pth/a": a}, fake)
        self.assertEqual(fake.urls(), [photo_l("pth", "a")])

    def test_a_changed_logo_retires_the_memo(self):
        a = prog("pth", "keepuco", s115("pth", "keepuco", "9"),
                 {"from": s115("pth", "keepuco", "8"), "url": photo("pth", "keepuco"),
                  "status": 200, "checkedAt": "2026-09-28"})
        self.cache(a)
        fake = FakeStatic()
        self.upgrade({"pth/keepuco": a}, fake)
        self.assertEqual(fake.urls(), [photo_l("pth", "keepuco")])
        self.assertEqual(a["logoFull"]["from"], s115("pth", "keepuco", "9"))

    def test_logo_moved_off_a_small_variant_drops_the_memo_without_a_request(self):
        for logo in (photo("pth", "keepuco"), None):
            with self.subTest(logo=logo):
                a = prog("pth", "keepuco", logo,
                         {"from": s115("pth", "keepuco"), "url": photo("pth", "keepuco"),
                          "status": 200, "checkedAt": "2026-09-28"})
                self.cache(a)
                fake = FakeStatic()
                self.upgrade({"pth/keepuco": a}, fake)
                self.assertEqual(fake.reqs, [])
                self.assertNotIn("logoFull", a)
                self.assertEqual(a.get("logo"), logo)

    def test_no_answer_keeps_the_previous_memo_and_retries_next_run_without_retrying_now(self):
        prev = {"from": s115("pth", "a"), "url": photo_l("pth", "a"),
                "status": 200, "bytes": 69_275, "checkedAt": "2026-08-20"}     # 40 d old: due
        a = prog("pth", "a", s115("pth", "a"), dict(prev))
        b = prog("pth", "b", s115("pth", "b"))
        self.cache(a, b)
        fake = FakeStatic({photo_l("pth", "a"): 503,
                           photo_l("pth", "b"): ("bytes", 5_000_000),           # a miss...
                           photo("pth", "b"): "reset"})                          # ...then no answer
        client, run = self.upgrade({"pth/a": a, "pth/b": b}, fake)
        self.assertEqual(fake.urls(), [photo_l("pth", "b"), photo("pth", "b"), photo_l("pth", "a")])
        self.assertEqual(client.failures, 2)                       # ONE HEAD per candidate: tries=1
        self.assertEqual(a["logoFull"], prev)                      # the earlier answer stands
        self.assertNotIn("logoFull", b)                            # no half answer is written
        self.assertEqual((run["logo_upgraded"], run["logo_deferred"], run["warnings"]), (1, 2, []))
        fake2 = FakeStatic()
        self.upgrade({"pth/a": a, "pth/b": b}, fake2, today=TODAY + dt.timedelta(days=1))
        self.assertEqual(len(fake2.reqs), 2)
        self.assertEqual(b["logoFull"]["url"], photo_l("pth", "b"))

    def test_all_miss_is_believed_only_when_the_small_logo_itself_answers(self):
        # (a) The storage answers a denied request exactly as a missing
        # object, and a challenge page is a 200: the control HEAD on the
        # programme's own small logo tells a block from a real "nothing larger".
        for control in ("missing", "blocked", "html", "redirect", 503, "reset"):
            with self.subTest(control=control):
                prev = {"from": s115("pth", "a"), "url": None, "status": 403, "checkedAt": "2026-08-01"}
                a = prog("pth", "a", s115("pth", "a"), dict(prev))
                self.cache(a)
                fake = FakeStatic({photo_l("pth", "a"): "missing", photo("pth", "a"): "missing",
                                   s115("pth", "a"): control})
                client, run = self.upgrade({"pth/a": a}, fake)
                self.assertEqual(fake.urls(), [photo_l("pth", "a"), photo("pth", "a"), s115("pth", "a")])
                self.assertEqual(a["logoFull"], prev)                   # nothing written
                self.assertEqual(client.failures, 1)                    # counted once, by whoever saw it
                self.assertEqual(run["logo_deferred"], 1)
        # The control answering as it should makes the all-miss an answer.
        a = prog("pth", "b", s115("pth", "b"))
        self.cache(a)
        fake = FakeStatic({photo_l("pth", "b"): "missing", photo("pth", "b"): "missing"})
        client, _ = self.upgrade({"pth/b": a}, fake)
        self.assertEqual((a["logoFull"]["url"], a["logoFull"]["status"], client.failures), (None, 403, 0))

    def test_a_storage_that_denies_everything_costs_three_programmes_and_no_published_artwork(self):
        # The review's offline simulation: 70 due programmes holding positive
        # memos, every HEAD answers the storage's 403 application/xml.
        programmes = {}
        for i in range(70):
            k = f"radio2/p{i:02d}"
            programmes[k] = prog("radio2", f"p{i:02d}", s115("radio2", f"p{i:02d}"),
                                 {"from": s115("radio2", f"p{i:02d}"), "url": photo_l("radio2", f"p{i:02d}"),
                                  "status": 200, "bytes": 69_275, "checkedAt": "2026-08-20"})
        self.cache(*programmes.values())
        before = copy.deepcopy(programmes)
        denied = {}
        for i in range(70):
            for u in (photo_l("radio2", f"p{i:02d}"), photo("radio2", f"p{i:02d}"), s115("radio2", f"p{i:02d}")):
                denied[u] = "missing"
        fake = FakeStatic(denied)
        client, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 9)                             # 3 programmes x (2 + the control)
        self.assertEqual(client.failures, 3)
        self.assertEqual({k: p["logoFull"] for k, p in programmes.items()},
                         {k: p["logoFull"] for k, p in before.items()})   # every published upgrade stands
        self.assertIn("stopped after 3 failed HEADs", run["warnings"][-1])
        for p in programmes.values():
            self.assertEqual(published_logo(p), p["logoFull"]["url"])

    def test_a_published_upgrade_is_withdrawn_only_by_a_second_run_that_agrees(self):
        # (b) The first all-miss marks the memo; it keeps publishing. A
        # same-day rerun does not confirm; a later run does. A hit in
        # between clears the mark.
        prev = {"from": s115("pth", "a"), "url": photo_l("pth", "a"), "status": 200, "bytes": 69_275,
                "checkedAt": "2026-08-20"}                                # 40 d old: due
        a = prog("pth", "a", s115("pth", "a"), dict(prev))
        self.cache(a)
        gone = {photo_l("pth", "a"): "missing", photo("pth", "a"): "missing"}
        fake = FakeStatic(gone)
        _, run = self.upgrade({"pth/a": a}, fake)
        self.assertEqual(len(fake.reqs), 3)
        self.assertEqual(a["logoFull"], dict(prev, missAt="2026-09-29"))
        self.assertEqual(published_logo(a), photo_l("pth", "a"))
        self.assertEqual((run["logo_upgraded"], run["logo_deferred"]), (1, 1))
        self.upgrade({"pth/a": a}, FakeStatic(gone))                    # the same day: still marked only
        self.assertEqual(a["logoFull"], dict(prev, missAt="2026-09-29"))
        withdrawn = copy.deepcopy(a)
        self.upgrade({"pth/a": withdrawn}, FakeStatic(gone), today=TODAY + dt.timedelta(days=1))
        self.assertEqual(withdrawn["logoFull"], {"from": s115("pth", "a"), "url": None, "status": 403,
                                                 "checkedAt": "2026-09-30"})
        self.assertEqual(published_logo(withdrawn), s115("pth", "a"))
        back = copy.deepcopy(a)
        self.upgrade({"pth/a": back}, FakeStatic(), today=TODAY + dt.timedelta(days=1))
        self.assertEqual(back["logoFull"], {"from": s115("pth", "a"), "url": photo_l("pth", "a"),
                                            "status": 200, "bytes": 69_275, "checkedAt": "2026-09-30"})
        for bad in ("not-a-date", 7):                                   # a malformed mark restarts the wait
            with self.subTest(missAt=bad):
                m = prog("pth", "a", s115("pth", "a"), dict(prev, missAt=bad))
                self.upgrade({"pth/a": m}, FakeStatic(gone))
                self.assertEqual(m["logoFull"], dict(prev, missAt="2026-09-29"))

    def test_a_run_of_all_miss_programmes_stops_the_step_and_memoises_none_of_them(self):
        # (c) Five programmes in a row with nothing larger (the natural rate
        # is ~6 %) are a block until proven otherwise.
        n = crawl.LOGO_MISS_STREAK
        programmes = {f"radio1/p{i:02d}": prog("radio1", f"p{i:02d}", s115("radio1", f"p{i:02d}"))
                      for i in range(n + 3)}
        self.cache(*programmes.values())
        answers = {}
        for i in range(n + 3):
            answers[photo_l("radio1", f"p{i:02d}")] = "missing"
            answers[photo("radio1", f"p{i:02d}")] = "missing"
        fake = FakeStatic(answers)
        client, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 3 * n)                        # the sixth is never asked
        self.assertTrue(all("logoFull" not in p for p in programmes.values()))
        self.assertEqual(client.failures, 0)
        self.assertEqual((run["logo_checked"], run["logo_deferred"]), (n, n + 3))
        self.assertEqual(len(run["warnings"]), 1)
        self.assertIn(f"{n} programmes in a row had nothing larger", run["warnings"][0])

    def test_a_hit_vouches_for_the_misses_before_it(self):
        n = crawl.LOGO_MISS_STREAK
        names = [f"p{i:02d}" for i in range(2 * n - 1)]                # n-1 misses, a hit, n-1 misses
        programmes = {f"radio1/{s}": prog("radio1", s, s115("radio1", s)) for s in names}
        self.cache(*programmes.values())
        answers = {}
        for i, s in enumerate(names):
            if i != n - 1:
                answers[photo_l("radio1", s)] = "missing"
                answers[photo("radio1", s)] = "missing"
        _, run = self.upgrade(programmes, FakeStatic(answers))
        self.assertEqual(run["warnings"], [])
        self.assertEqual([programmes[f"radio1/{s}"]["logoFull"]["url"] for s in names],
                         [None] * (n - 1) + [photo_l("radio1", names[n - 1])] + [None] * (n - 1))
        self.assertEqual(run["logo_deferred"], 0)

    def test_programmes_that_keep_failing_rotate_behind_the_rest(self):
        # Three directories at the head of the queue that answer 503 every
        # day must not spend the 3-failure allowance at the start of every run.
        names = [f"p{i:02d}" for i in range(8)]
        programmes = {f"radio1/{s}": prog("radio1", s, s115("radio1", s)) for s in names}
        self.cache(*programmes.values())
        broken = {photo_l("radio1", s): 503 for s in names[:3]}
        fake = FakeStatic(broken)
        _, run = self.upgrade(programmes, fake)
        self.assertEqual(fake.urls(), [photo_l("radio1", s) for s in names[:3]])
        self.assertTrue(all(programmes[f"radio1/{s}"]["logoTriedAt"] == "2026-09-29" for s in names[:3]))
        fake = FakeStatic(broken)
        _, run = self.upgrade(programmes, fake, today=TODAY + dt.timedelta(days=1))
        self.assertEqual(fake.urls(), [photo_l("radio1", s) for s in names[3:] + names[:3]])
        for s in names[3:]:
            self.assertEqual(programmes[f"radio1/{s}"]["logoFull"]["url"], photo_l("radio1", s))
            self.assertNotIn("logoTriedAt", programmes[f"radio1/{s}"])
        self.assertEqual(programmes["radio1/p00"]["logoTriedAt"], "2026-09-30")
        # An answer clears the stamp; a logo that moves off a small variant drops it.
        self.upgrade(programmes, FakeStatic(), today=TODAY + dt.timedelta(days=2))
        self.assertNotIn("logoTriedAt", programmes["radio1/p00"])
        programmes["radio1/p01"]["logoTriedAt"] = "2026-10-01"
        programmes["radio1/p01"]["logo"] = photo("radio1", "p01")
        self.upgrade(programmes, FakeStatic(), today=TODAY + dt.timedelta(days=2))
        self.assertNotIn("logoTriedAt", programmes["radio1/p01"])

    def test_a_tripped_streak_rotates_its_programmes_back_too(self):
        n = crawl.LOGO_MISS_STREAK
        names = [f"p{i:02d}" for i in range(n + 2)]
        programmes = {f"radio1/{s}": prog("radio1", s, s115("radio1", s)) for s in names}
        self.cache(*programmes.values())
        answers = {}
        for s in names[:n]:
            answers[photo_l("radio1", s)] = "missing"
            answers[photo("radio1", s)] = "missing"
        _, run = self.upgrade(programmes, FakeStatic(answers))
        self.assertEqual(len(run["warnings"]), 1)
        fake = FakeStatic(answers)
        self.upgrade(programmes, fake, today=TODAY + dt.timedelta(days=1))
        self.assertEqual(fake.urls()[:2], [photo_l("radio1", s) for s in names[n:]])

    def test_a_marked_upgrade_is_asked_again_on_the_next_run(self):
        prev = {"from": s115("pth", "a"), "url": photo_l("pth", "a"), "status": 200, "bytes": 69_275,
                "checkedAt": "2026-08-20"}
        a = prog("pth", "z", s115("pth", "z"), dict(prev, **{"from": s115("pth", "z"), "url": photo_l("pth", "z")}))
        b = prog("pth", "b", s115("pth", "b"), {"from": s115("pth", "b"), "url": None, "status": 403,
                                                 "checkedAt": "2026-08-25"})             # due, newer
        self.cache(a, b)
        gone = {photo_l("pth", "z"): "missing", photo("pth", "z"): "missing"}
        self.upgrade({"pth/z": a}, FakeStatic(gone))
        self.assertEqual(a["logoFull"]["missAt"], "2026-09-29")
        self.assertNotIn("logoTriedAt", a)                  # marked, not failed: it keeps its place
        fake = FakeStatic(gone)
        self.upgrade({"pth/z": a, "pth/b": b}, fake, today=TODAY + dt.timedelta(days=1), cap=1)
        self.assertEqual(fake.urls(), [photo_l("pth", "z"), photo("pth", "z"), s115("pth", "z")])
        self.assertIsNone(a["logoFull"]["url"])

    def test_the_storages_missing_403_is_an_answer_not_a_failure(self):
        n = crawl.LOGO_MISS_STREAK - 1
        programmes = {f"radio1/p{i:02d}": prog("radio1", f"p{i:02d}", s115("radio1", f"p{i:02d}"))
                      for i in range(n)}
        self.cache(*programmes.values())
        answers = {}
        for i in range(n):
            answers[photo("radio1", f"p{i:02d}")] = "missing"
            answers[photo_l("radio1", f"p{i:02d}")] = "missing"
        fake = FakeStatic(answers)
        client, run = self.upgrade(programmes, fake)
        self.assertEqual((len(fake.reqs), client.failures, run["warnings"]), (3 * n, 0, []))
        self.assertTrue(all(p["logoFull"]["url"] is None for p in programmes.values()))

    def test_stops_after_three_failed_heads(self):
        for answer in ("blocked", 503, "reset", 429):
            with self.subTest(answer=answer):
                programmes = {f"radio1/p{i:02d}": prog("radio1", f"p{i:02d}", s115("radio1", f"p{i:02d}"))
                              for i in range(10)}
                self.cache(*programmes.values())
                fake = FakeStatic({photo_l("radio1", f"p{i:02d}"): answer for i in range(10)})
                client, run = self.upgrade(programmes, fake)
                self.assertEqual(len(fake.reqs), 3)
                self.assertEqual(client.failures, 3)       # a 403 block page too: the ledger + breaker see it
                self.assertEqual((run["logo_checked"], run["logo_probes"], run["logo_deferred"]), (3, 3, 10))
                self.assertTrue(all("logoFull" not in p for p in programmes.values()))
                self.assertEqual(len(run["warnings"]), 1)
                self.assertIn("stopped after 3 failed HEADs (10 due left", run["warnings"][0])

    def test_a_stop_mid_programme_writes_no_memo(self):
        programmes = {f"radio1/p{i}": prog("radio1", f"p{i}", s115("radio1", f"p{i}")) for i in range(3)}
        self.cache(*programmes.values())
        fake = FakeStatic({photo_l("radio1", "p0"): 503, photo_l("radio1", "p1"): 503,
                           photo_l("radio1", "p2"): 400})                        # a miss, but the 3rd failure
        _, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 3)                                      # p2's original is never asked
        self.assertTrue(all("logoFull" not in p for p in programmes.values()))
        self.assertEqual(run["logo_deferred"], 3)
        # The third failure on a programme's LAST candidate: no control HEAD
        # is sent after the stop, and the all-miss is not written.
        fake = FakeStatic({photo_l("radio1", "p0"): 503, photo_l("radio1", "p1"): 503,
                           photo_l("radio1", "p2"): "missing", photo("radio1", "p2"): 400})
        _, run = self.upgrade(programmes, fake)
        self.assertEqual(fake.urls(), [photo_l("radio1", "p0"), photo_l("radio1", "p1"),
                                       photo_l("radio1", "p2"), photo("radio1", "p2")])
        self.assertTrue(all("logoFull" not in p for p in programmes.values()))

    def test_never_more_than_60_programmes_a_run_never_checked_first(self):
        programmes = {}
        for i in range(70):
            p = prog("radio2", f"p{i:02d}", s115("radio2", f"p{i:02d}"))
            if i < 20:                                                 # stale answers (due)
                p["logoFull"] = {"from": p["logo"], "url": None, "status": 403,
                                 "checkedAt": (TODAY - dt.timedelta(days=60 - i)).isoformat()}
            programmes[f"radio2/p{i:02d}"] = p
        self.cache(*programmes.values())
        fake = FakeStatic()
        _, run = self.upgrade(programmes, fake)
        self.assertEqual(len(fake.reqs), 60)
        probed = [u.split("/")[-2] for u in fake.urls()]
        self.assertEqual(probed[:50], [f"p{i:02d}" for i in range(20, 70)])   # never-checked
        self.assertEqual(probed[50:], [f"p{i:02d}" for i in range(10)])      # then the oldest
        self.assertEqual((run["logo_checked"], run["logo_deferred"]), (60, 10))
        fake2 = FakeStatic()
        self.upgrade(programmes, fake2, today=TODAY + dt.timedelta(days=1))
        self.assertEqual(len(fake2.reqs), 10)

    def test_the_bound_counts_programmes_not_heads(self):
        programmes = {f"pth/p{i:02d}": prog("pth", f"p{i:02d}", small("pth", f"p{i:02d}", f"{i}_1920"))
                      for i in range(65)}
        self.cache(*programmes.values())
        answers = {}
        for i in range(65):
            answers[photo("pth", f"p{i:02d}")] = "missing"
            answers[photo_l("pth", f"p{i:02d}")] = "missing"
        fake = FakeStatic(answers)
        _, run = self.upgrade(programmes, fake)
        self.assertEqual((len(fake.reqs), run["logo_checked"], run["logo_deferred"]), (180, 60, 5))
        self.assertEqual(run["logo_upgraded"], 60)                  # every one took its 1920 banner

    def test_cap_argument_bounds_a_dev_run(self):
        programmes = {f"pth/p{i}": prog("pth", f"p{i}", s115("pth", f"p{i}")) for i in range(6)}
        self.cache(*programmes.values())
        fake = FakeStatic()
        self.upgrade(programmes, fake, cap=3)
        self.assertEqual(len(fake.reqs), 3)

    def test_dormant_and_filtered_programmes_are_not_requested(self):
        live = prog("radio1", "live", s115("radio1", "live"))
        dormant = prog("pth", "2024debate", small("pth", "2024debate"))
        empty = prog("radio3", "book_club", s115("radio3", "book_club"))
        other = prog("radio4", "Aubade", small("radio4", "Aubade", "600_1920"))
        self.cache(live, other)
        self.cache(empty, episodes=False)                           # cached month, zero episodes
        programmes = {"radio1/live": live, "pth/2024debate": dormant,
                      "radio3/book_club": empty, "radio4/Aubade": other}
        fake = FakeStatic()
        _, run = self.upgrade(programmes, fake, only={"radio1", "pth", "radio3"})
        self.assertEqual(fake.urls(), [photo_l("radio1", "live")])
        self.assertEqual(run["logo_small"], 1)
        self.assertNotIn("logoFull", other)

    def test_budget_is_respected(self):
        a = prog("pth", "a", s115("pth", "a"))
        self.cache(a)
        client = common.Client(pace_s=0, budget=0, log=lambda *_: None)
        with self.assertRaises(common.BudgetExhausted):
            self.upgrade({"pth/a": a}, FakeStatic(), client=client)
        self.assertNotIn("logoFull", a)

    def test_behind_the_shared_circuit_breaker(self):
        a = prog("pth", "a", s115("pth", "a"))
        self.cache(a)
        client = common.Client(pace_s=0, budget=500, log=lambda *_: None)
        client.requests, client.failures = 40, 10                    # a run already at the 25 % line
        with self.assertRaises(common.CircuitOpen):
            self.upgrade({"pth/a": a}, FakeStatic({photo_l("pth", "a"): 503}), client=client)
        self.assertNotIn("logoFull", a)


class ClientAnswers(unittest.TestCase):
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

    def test_the_logo_steps_answer_statuses_are_neither_retried_nor_counted(self):
        c = common.Client(pace_s=0, budget=10, log=lambda *_: None)
        fake = FakeStatic({"https://x.test/m.jpg": "missing", "https://x.test/g.jpg": 410})
        with mock.patch("urllib.request.urlopen", fake):
            st, ct, _ = c.get("https://x.test/m.jpg", method="HEAD", tries=3, ok=crawl.LOGO_HEAD_ANSWERS)
            self.assertEqual((st, ct), (403, "application/xml"))
            st, _, _ = c.get("https://x.test/g.jpg", method="HEAD", tries=3, ok=crawl.LOGO_HEAD_ANSWERS)
            self.assertEqual(st, 410)
        self.assertEqual((c.requests, c.failures, len(fake.reqs)), (2, 0, 2))


class ClientCountFailure(unittest.TestCase):
    def test_an_externally_judged_failure_reaches_the_ledger_and_the_breaker(self):
        c = common.Client(pace_s=0, budget=100, log=lambda *_: None)
        c.count_failure()
        self.assertEqual((c.requests, c.failures), (0, 1))
        c.requests, c.failures = 40, 10                                  # at the 25 % line
        with self.assertRaises(common.CircuitOpen):
            c.count_failure()


class Publish(unittest.TestCase):
    SMALL = f"{BASE}/pth/keepuco/programme_photo_s.jpg"
    FULL = f"{BASE}/pth/keepuco/programme_photo.jpg"
    S115 = f"{BASE}/radio1/whatsupbro/11215_115.jpg"
    L720 = f"{BASE}/radio1/whatsupbro/programme_photo_l.jpg"

    def test_verified_larger_artwork_wins(self):
        for logo, url in ((self.SMALL, self.FULL), (self.S115, self.L720)):
            with self.subTest(logo=logo):
                p = {"logo": logo, "logoFull": {"from": logo, "url": url, "status": 200, "bytes": 69_275}}
                self.assertEqual(published_logo(p), url)

    def test_falls_back_to_the_crawlers_pick(self):
        for memo in (None, {}, [], {"from": self.SMALL, "url": None, "status": 403},
                     {"from": "https://other/x_s.jpg", "url": self.FULL, "bytes": 69_275},   # logo moved on
                     {"from": self.SMALL, "url": "http://insecure/x.jpg", "bytes": 69_275},
                     {"from": self.SMALL, "url": 7, "bytes": 69_275},
                     # an https URL the derivation could never produce: off-host, another directory
                     {"from": self.SMALL, "url": "https://example.com/pth/keepuco/programme_photo.jpg",
                      "bytes": 69_275},
                     {"from": self.SMALL, "url": f"{BASE}/pth/other/programme_photo_l.jpg", "bytes": 69_275},
                     # the right URL, but no size or one over today's cap (an earlier rule's memo)
                     {"from": self.SMALL, "url": self.FULL},
                     {"from": self.SMALL, "url": self.FULL, "bytes": 0},
                     {"from": self.SMALL, "url": self.FULL, "bytes": "69275"},
                     {"from": self.SMALL, "url": self.FULL, "bytes": True},
                     {"from": self.SMALL, "url": self.FULL, "bytes": crawl.LOGO_MAX_BYTES + 1}):
            with self.subTest(memo=memo):
                p = {"logo": self.SMALL}
                if memo is not None:
                    p["logoFull"] = memo
                self.assertEqual(published_logo(p), self.SMALL)

    def test_no_logo_stays_none(self):
        self.assertIsNone(published_logo({}))
        self.assertIsNone(published_logo({"logoFull": {"from": None, "url": self.FULL, "bytes": 69_275}}))


class PublishedCatalog(unittest.TestCase):
    """Build the committed data/ into a temp dir with memos set: those logos
    publish larger, every other logo is byte-identical, and the index keeps
    its schema (logo stays one URL string, streamVersion unchanged)."""

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
        return json.loads((tmp / "index.json").read_text("utf-8"))

    @staticmethod
    def logos(index):
        return {(c["id"], p["slug"]): p.get("logo") for c in index["channels"]
                if c["source"] == "rthk" for p in c["programmes"]}

    @staticmethod
    def fields(index):
        return {(c["id"], p["slug"]): sorted(p) for c in index["channels"]
                if c["source"] == "rthk" for p in c["programmes"]}

    def committed(self):
        programmes = common.read_json(common.PROGRAMMES_PATH, {}) or {}
        if not programmes:
            self.skipTest("no committed data/rthk/programmes.json")
        return programmes

    def test_memos_publish_larger_and_nothing_else_moves(self):
        programmes = self.committed()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            before = self.logos(self.build(programmes, Path(a)))
            s115_key = next((k for k, v in sorted(before.items()) if v and "_115." in v), None)
            s_key = next((k for k, v in sorted(before.items()) if v and "_s." in v), None)
            if not (s115_key and s_key):
                self.skipTest("the committed data lacks a published _115 or _s logo")
            memoed = copy.deepcopy(programmes)
            want = {}
            for key, pick in ((s115_key, 1), (s_key, 0)):              # the original, the 720 rendition
                p = memoed[f"{key[0]}/{key[1]}"]
                want[key] = logo_candidates(p["logo"])[pick]
                p["logoFull"] = {"from": p["logo"], "url": want[key], "status": 200, "bytes": 69_275,
                                 "checkedAt": "2026-09-29"}
            after = self.logos(self.build(memoed, Path(b)))
        for key, url in want.items():
            self.assertEqual(after[key], url)
        self.assertEqual({k: v for k, v in after.items() if k not in want},
                         {k: v for k, v in before.items() if k not in want})

    def test_every_small_logo_upgraded_keeps_the_schema(self):
        programmes = self.committed()
        memoed = copy.deepcopy(programmes)
        n = 0
        for p in memoed.values():
            cands = logo_candidates(p.get("logo"))
            if cands:
                p["logoFull"] = {"from": p["logo"], "url": cands[0], "status": 200, "bytes": 69_275,
                                 "checkedAt": "2026-09-29"}
                n += 1
        self.assertGreater(n, 0)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            before = self.build(programmes, Path(a))
            after = self.build(memoed, Path(b))
        self.assertEqual((after["streamVersion"], before["streamVersion"]), (1, 1))
        self.assertEqual(self.fields(after), self.fields(before))   # same programmes, same fields
        for key, logo in self.logos(after).items():
            if logo is not None:
                self.assertIsInstance(logo, str)
                self.assertTrue(logo.startswith("https://"), key)
                self.assertEqual(logo_candidates(logo), [], key)    # no small logo left
        if jsonschema:
            jsonschema.validate(after, INDEX_SCHEMA)


if __name__ == "__main__":
    unittest.main()
