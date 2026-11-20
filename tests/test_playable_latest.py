"""SR-01 — the catalog never lists an episode without an archive file as the
latest. Built from the real listing shape of the two programmes that did it
(2026-11): radio1/50YearsAfter's newest row is a pre-empted broadcast
(2026-11-07 → 404, 2026-10-31 → 200), and radio5/hktoday is the Radio 5 copy
of a Radio 1 simulcast whose audio is archived under radio1/hktoday only.

The raw catchUp pages go through crawl.fetch_month, the playlist probes
through crawl.check_playable with a stub client (nothing touches the
network), and the result through build_catalog.main into a temp dir, so the
whole fetch → probe → publish path is exercised."""
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import build_catalog  # noqa: E402
import common  # noqa: E402
import crawl  # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None

TODAY = dt.date(2026, 11, 12)
PLAYLIST = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nindex.m3u8\n"

# Raw catchUp answers in RTHK's shape (status / content[{id,title,date dd/mm/yyyy,part}] / nextPage),
# rows copied from the 2026-11-11 crawl.
CATCHUP = {
    ("radio1", "50YearsAfter", "202611"): [
        {"id": "1138162", "date": "07/11/2026", "part": [],
         "title": "轉播『通訊事務管理局聲音廣播牌照續期公眾資詢會』,節目暫停播放一集，敬請留意。"}],
    ("radio1", "50YearsAfter", "202610"): [
        {"id": "1136682", "date": "31/10/2026", "part": ["足本 Full (HKT 14:00 - 15:00)"],
         "title": "盛世再臨系列：長征與現代長征"},
        {"id": "1135316", "date": "24/10/2026", "part": ["足本 Full (HKT 14:00 - 15:00)"],
         "title": "盛世再臨系列：中共二十屆五中全會(下)"},
        {"id": "1133881", "date": "17/10/2026", "part": ["足本 Full (HKT 14:00 - 15:00)"],
         "title": "盛世再臨系列：中共二十屆五中全會(上)"}],
    ("radio5", "hktoday", "202611"): [
        {"id": "1138996", "date": "11/11/2026", "title": "晨早新聞天地（與第一台聯播）"},
        {"id": "1138759", "date": "10/11/2026", "title": "晨早新聞天地（與第一台聯播）"},
        {"id": "1138482", "date": "09/11/2026", "title": "晨早新聞天地（與第一台聯播）"},
        {"id": "1138204", "date": "07/11/2026", "title": "晨早新聞天地（與第一台聯播）"}],
    ("radio1", "hktoday", "202611"): [
        {"id": "1138950", "date": "11/11/2026", "part": ["第一部份 Part 1 (HKT 06:30 - 07:00)"],
         "title": "晨早新聞天地"},
        {"id": "1138711", "date": "10/11/2026", "title": "晨早新聞天地",
         "part": ["足本 Full (HKT 06:30 - 08:00)", "第一部份 Part 1 (HKT 06:30 - 07:00)",
                  "第二部份 Part 2 (HKT 07:00 - 08:00)"]}],
}
PROGRAMMES = {
    "radio1/50YearsAfter": {"channel": "radio1", "slug": "50YearsAfter", "title_zh": "五十年後",
                            "lastSeenActive": "2026-11-11"},
    "radio5/hktoday": {"channel": "radio5", "slug": "hktoday", "title_zh": "晨早新聞天地",
                       "lastSeenActive": "2026-11-11"},
    "radio1/hktoday": {"channel": "radio1", "slug": "hktoday", "title_zh": "晨早新聞天地",
                       "lastSeenActive": "2026-11-11"},
}


def cdn(ch, slug, date):
    return common.STREAM_TEMPLATE.format(channel=ch, slug=slug, date=date.replace("-", ""))


# What the archive answered (probed 2026-11-11): the pre-empted 50YearsAfter
# day and every radio5/hktoday day are missing; the rest exist.
ARCHIVE = {
    cdn("radio1", "50YearsAfter", "2026-11-07"): (404, "", b""),
    cdn("radio1", "50YearsAfter", "2026-10-31"): (200, "application/vnd.apple.mpegurl", PLAYLIST),
    cdn("radio1", "50YearsAfter", "2026-10-24"): (200, "application/vnd.apple.mpegurl", PLAYLIST),
    cdn("radio1", "hktoday", "2026-11-11"): (200, "application/vnd.apple.mpegurl", PLAYLIST),
}


class StubClient:
    """The crawler's Client contract, offline: an answer per URL (default 404
    for a CDN playlist), the 404-is-an-answer rule, failure counting."""

    def __init__(self, answers=None, catchup=None):
        self.answers = dict(answers or {})
        self.catchup = catchup or {}
        self.requests = 0
        self.failures = 0
        self.urls = []

    def get(self, url, headers=None, method="GET", timeout=40, tries=3, ok=(200,)):
        self.requests += 1
        self.urls.append(url)
        st, ct, body = self.answers.get(url, (404, "", b""))
        if st != 404 and st not in ok:
            self.failures += 1
        return st, ct, body

    def count_failure(self):
        self.failures += 1

    def get_json(self, url, headers=None):
        self.requests += 1
        self.urls.append(url)
        q = dict(kv.split("=", 1) for kv in url.split("?", 1)[1].split("&"))
        rows = self.catchup.get((q["c"], q["p"], q["m"]))
        if rows is None:
            return 200, {"status": "0"}
        return 200, {"status": "1", "content": rows, "nextPage": -1}


class Sandbox:
    """data/ + the published tree in a temp dir; crawl and build both pointed at it."""

    def __init__(self, testcase):
        self.tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.rthk = root / "data" / "rthk"
        self.paths = {"RTHK_DATA": self.rthk,
                      "PROGRAMMES_PATH": self.rthk / "programmes.json",
                      "OVERRIDES_PATH": self.rthk / "overrides.json",
                      "PLAYABLE_PATH": self.rthk / "playable.json",
                      "METRO_WINDOW_PATH": root / "data" / "metro" / "window.json",
                      "LAST_RUN_PATH": root / "data" / "last-run.json",
                      "INDEX_PATH": root / "index.json",
                      "PROG_DIR": root / "prog"}
        self.root = root
        patches = [mock.patch.object(crawl, "RTHK_DATA", self.rthk),
                   mock.patch.object(crawl, "today_hkt", lambda: TODAY),
                   mock.patch.object(crawl, "log", lambda *a, **k: None)]
        for p in patches:
            p.start()
            testcase.addCleanup(p.stop)
        self.programmes = json.loads(json.dumps(PROGRAMMES))
        self.overrides = {}
        self.cache = {}

    def fetch_months(self):
        """Cache every month of the fixture listing the way enumeration does."""
        client = StubClient(catchup=CATCHUP)
        for (ch, slug, m) in CATCHUP:
            ok, eps = crawl.fetch_month(client, ch, slug, m)
            assert ok
            common.write_json(crawl.month_path(ch, slug, m), {"fetchedAt": TODAY.isoformat(), "episodes": eps})

    def probe(self, answers=ARCHIVE):
        client = StubClient(answers)
        run = {"warnings": []}
        crawl.check_playable(client, self.programmes, self.overrides, self.cache, None, run)
        return client, run

    def build(self):
        common.write_json(self.paths["PROGRAMMES_PATH"], self.programmes)
        common.write_json(self.paths["OVERRIDES_PATH"], self.overrides)
        common.write_json(self.paths["PLAYABLE_PATH"], self.cache)
        common.write_json(self.paths["LAST_RUN_PATH"], {})
        out = io.StringIO()
        with contextlib.ExitStack() as st:
            for name, path in self.paths.items():
                st.enter_context(mock.patch.object(build_catalog, name, path))
            st.enter_context(mock.patch.object(build_catalog, "today_hkt", lambda: TODAY))
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(out))
            rc = build_catalog.main()
        self.log = out.getvalue()
        assert rc == 0, self.log
        index = common.read_json(self.paths["INDEX_PATH"])
        progs = {p.relative_to(self.paths["PROG_DIR"]).with_suffix("").as_posix():
                 json.loads(p.read_text(encoding="utf-8"))
                 for p in sorted(self.paths["PROG_DIR"].glob("*/*.json"))}
        entries = {f"{c['id']}/{p['slug']}": p for c in index["channels"] if c["source"] == "rthk"
                   for p in c["programmes"]}
        return index, progs, entries


class PlayableLatest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.box.fetch_months()

    def test_pre_empted_newest_episode_is_dropped_and_latest_is_the_next(self):
        client, run = self.box.probe()
        # 50YearsAfter: 11-07 (404) then 10-31 (200) — two GETs, then stop.
        fifty = [u for u in client.urls if "/50YearsAfter/" in u]
        self.assertEqual(fifty, [cdn("radio1", "50YearsAfter", "2026-11-07"),
                                 cdn("radio1", "50YearsAfter", "2026-10-31")])
        index, progs, entries = self.box.build()
        e = entries["radio1/50YearsAfter"]
        self.assertEqual(e["latestDate"], "2026-10-31")
        self.assertEqual(e["episodeCount"], 3)
        rows = progs["radio1/50YearsAfter"]["episodes"]
        self.assertEqual([r["date"] for r in rows], ["2026-10-31", "2026-10-24", "2026-10-17"])
        self.assertNotIn("1138162", [r["id"] for r in rows])

    def test_three_unplayable_newest_dates_omit_the_programme_and_warn(self):
        client, run = self.box.probe()
        r5 = [u for u in client.urls if "/radio5/hktoday/" in u]
        self.assertEqual(len(r5), crawl.PLAYABLE_HEAD_DATES)
        self.assertEqual(run["unplayable_programmes"], ["radio5/hktoday"])
        self.assertTrue(any("radio5/hktoday" in w for w in run["warnings"]))
        index, progs, entries = self.box.build()
        self.assertNotIn("radio5/hktoday", entries)
        self.assertNotIn("radio5/hktoday", progs)
        self.assertIn("radio1/hktoday", entries)            # the simulcast's real home stays
        self.assertEqual(entries["radio1/hktoday"]["latestDate"], "2026-11-11")
        self.assertIn("1 programmes unplayable (radio5/hktoday)", self.box.log)

    def test_no_answer_keeps_every_episode_and_stops_the_programme(self):
        answers = dict(ARCHIVE)
        answers[cdn("radio1", "50YearsAfter", "2026-11-07")] = (503, "text/html", b"busy")
        client, run = self.box.probe(answers)
        fifty = [u for u in client.urls if "/50YearsAfter/" in u]
        self.assertEqual(len(fifty), 1)                     # stopped after the no-answer
        self.assertNotIn(crawl.playable_key("radio1", "50YearsAfter", "2026-11-07"), self.box.cache)
        self.assertEqual(run["playable_failures"], 1)
        _, progs, entries = self.box.build()
        self.assertEqual(entries["radio1/50YearsAfter"]["latestDate"], "2026-11-07")
        self.assertEqual(entries["radio1/50YearsAfter"]["episodeCount"], 4)

    def test_a_200_that_is_not_a_playlist_is_no_answer_and_counted(self):
        answers = dict(ARCHIVE)
        answers[cdn("radio1", "50YearsAfter", "2026-11-07")] = (200, "text/html", b"<html>blocked")
        client, run = self.box.probe(answers)
        self.assertNotIn(crawl.playable_key("radio1", "50YearsAfter", "2026-11-07"), self.box.cache)
        self.assertGreaterEqual(client.failures, 1)

    def test_cached_playable_date_is_never_asked_again(self):
        self.box.probe()
        client, run = self.box.probe()
        self.assertFalse([u for u in client.urls if "/radio1/hktoday/" in u])
        # 50YearsAfter's 11-07 miss was answered today: not re-asked; 10-31 is cached True.
        self.assertFalse([u for u in client.urls if "/50YearsAfter/" in u])
        self.assertEqual(run["playable_probes"], 0)

    def test_unplayable_date_is_rechecked_for_seven_days_then_accepted(self):
        key = crawl.playable_key("radio1", "50YearsAfter", "2026-11-07")
        url = cdn("radio1", "50YearsAfter", "2026-11-07")
        self.box.cache[crawl.playable_key("radio1", "50YearsAfter", "2026-10-31")] = {
            "ok": True, "checkedAt": "2026-11-01"}
        for first, asked in ((TODAY - dt.timedelta(days=6), True), (TODAY - dt.timedelta(days=7), False)):
            with self.subTest(first_miss=first.isoformat()):
                self.box.cache[key] = {"ok": False, "checkedAt": (TODAY - dt.timedelta(days=1)).isoformat(),
                                       "firstMissAt": first.isoformat()}
                client, _ = self.box.probe()
                self.assertEqual(url in client.urls, asked)
                self.assertIs(self.box.cache[key]["ok"], False)
                self.assertEqual(self.box.cache[key]["firstMissAt"], first.isoformat())

    def test_audio_that_appears_later_returns_the_episode(self):
        self.box.probe()
        key = crawl.playable_key("radio1", "50YearsAfter", "2026-11-07")
        self.box.cache[key]["checkedAt"] = (TODAY - dt.timedelta(days=1)).isoformat()
        answers = dict(ARCHIVE)
        answers[cdn("radio1", "50YearsAfter", "2026-11-07")] = (200, "", PLAYLIST)
        self.box.probe(answers)
        self.assertIs(self.box.cache[key]["ok"], True)
        _, _, entries = self.box.build()
        self.assertEqual(entries["radio1/50YearsAfter"]["latestDate"], "2026-11-07")

    # The 11-11 crawl shape: radio1/hktoday listed the same-day episode (only
    # "Part 1" up) before its archive existed. An early crawl records it as
    # "no file"; the next (late) crawl finds the NEXT day's episode already
    # archived. The older date must still be re-asked daily until it answers.
    def _late_upload_days(self, appears_on, days):
        path = crawl.month_path("radio1", "hktoday", "202611")
        late = cdn("radio1", "hktoday", "2026-11-11")
        asked = []
        for n in range(days):
            day = dt.date(2026, 11, 11) + dt.timedelta(days=n)
            if n:                                            # a new daily episode is listed and archived
                doc = common.read_json(path)
                doc["episodes"].insert(0, {"id": str(1139000 + n), "date": day.isoformat(),
                                           "title": "晨早新聞天地"})
                common.write_json(path, doc)
            answers = {cdn("radio1", "hktoday", (dt.date(2026, 11, 11) + dt.timedelta(days=i)).isoformat()):
                       (200, "", PLAYLIST) for i in range(1, n + 1)}
            answers[cdn("radio1", "hktoday", "2026-11-10")] = (200, "", PLAYLIST)
            answers[late] = (200, "", PLAYLIST) if appears_on is not None and n >= appears_on else (404, "", b"")
            with mock.patch.object(crawl, "today_hkt", lambda d=day: d):
                for _ in range(2):                           # a second run the same day asks nothing new
                    client, _ = self.box.probe(answers)
                    asked.append((n, client.urls.count(late)))
        return late, asked

    def test_late_archive_returns_even_after_a_newer_episode_is_playable(self):
        key = crawl.playable_key("radio1", "hktoday", "2026-11-11")
        late, asked = self._late_upload_days(appears_on=3, days=8)
        # day 0: probed (404); days 1-2: re-asked once each although 11-12 / 11-13 answer 200;
        # day 3: re-asked, now 200 (11-11 is already the 4th date by then); never again after.
        per_day = {}
        for n, c in asked:
            per_day[n] = per_day.get(n, 0) + c
        self.assertEqual(per_day, {0: 1, 1: 1, 2: 1, 3: 1, 4: 0, 5: 0, 6: 0, 7: 0})
        self.assertEqual([c for _, c in asked[1::2]], [0] * 8)      # second run of each day
        self.assertEqual(self.box.cache[key], {"ok": True, "checkedAt": "2026-11-14"})
        _, progs, entries = self.box.build()
        rows = progs["radio1/hktoday"]["episodes"]
        self.assertIn("1138950", [r["id"] for r in rows])          # the 11-11 episode is back
        self.assertEqual(entries["radio1/hktoday"]["latestDate"], "2026-11-18")

    def test_archive_that_never_appears_is_asked_for_seven_days_then_accepted(self):
        key = crawl.playable_key("radio1", "hktoday", "2026-11-11")
        _, asked = self._late_upload_days(appears_on=None, days=10)
        per_day = {}
        for n, c in asked:
            per_day[n] = per_day.get(n, 0) + c
        self.assertEqual([per_day[n] for n in range(10)], [1] * crawl.PLAYABLE_RECHECK_DAYS + [0] * 3)
        self.assertIs(self.box.cache[key]["ok"], False)
        self.assertEqual(self.box.cache[key]["firstMissAt"], "2026-11-11")
        _, progs, _ = self.box.build()
        self.assertNotIn("1138950", [r["id"] for r in progs["radio1/hktoday"]["episodes"]])

    def test_an_override_episode_is_playable_without_a_request(self):
        self.box.overrides["radio1/50YearsAfter/1138162"] = "https://example.test/a.m3u8"
        client, _ = self.box.probe()
        self.assertFalse([u for u in client.urls if "/50YearsAfter/" in u])
        _, _, entries = self.box.build()
        self.assertEqual(entries["radio1/50YearsAfter"]["latestDate"], "2026-11-07")

    def test_probe_cap_stops_the_step_with_a_warning(self):
        client = StubClient(ARCHIVE)
        run = {"warnings": []}
        crawl.check_playable(client, self.box.programmes, {}, self.box.cache, None, run, cap=1)
        self.assertEqual(client.requests, 1)
        self.assertTrue(any("playable check stopped" in w for w in run["warnings"]))

    def test_a_run_of_missing_playlists_records_nothing(self):
        # A moved archive answers 404 everywhere: 6 dead programmes in a row
        # (18 answers >= the streak) stop the step and record none of them.
        for i in range(6):
            slug = f"dead{i}"
            self.box.programmes[f"radio2/{slug}"] = {"channel": "radio2", "slug": slug}
            common.write_json(crawl.month_path("radio2", slug, "202611"), {"episodes": [
                {"id": str(100 + d), "date": f"2026-11-{d:02d}", "title": ""} for d in (3, 4, 5, 6)]})
        client = StubClient({})                                  # every playlist 404
        run = {"warnings": []}
        crawl.check_playable(client, self.box.programmes, {}, self.box.cache, None, run)
        self.assertEqual(self.box.cache, {})
        self.assertTrue(any("in a row had no archive file" in w for w in run["warnings"]))
        self.assertEqual(client.requests, crawl.PLAYABLE_GONE_STREAK)

    def test_cache_prunes_dates_outside_the_window(self):
        self.box.cache["radio1/x/2025-01-01"] = {"ok": True, "checkedAt": "2025-01-02"}
        self.box.probe()
        self.assertNotIn("radio1/x/2025-01-01", self.box.cache)

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_both_schemas_still_validate(self):
        self.box.probe()
        index, progs, _ = self.box.build()
        schema = lambda n: json.loads((REPO / "schema" / n).read_text(encoding="utf-8"))  # noqa: E731
        jsonschema.validate(index, schema("index.schema.json"))
        for doc in progs.values():
            jsonschema.validate(doc, schema("programme.schema.json"))


class PlayableFilter(unittest.TestCase):
    def eps(self, *dates):
        return [{"id": str(i), "date": d} for i, d in enumerate(dates)]

    def test_unknown_dates_are_kept(self):
        kept, omit = crawl.playable_filter("radio1", "x", self.eps("2026-11-03", "2026-11-02"), {})
        self.assertEqual(len(kept), 2)
        self.assertFalse(omit)

    def test_two_false_then_unknown_is_not_omitted(self):
        cache = {crawl.playable_key("radio1", "x", d): {"ok": False} for d in ("2026-11-03", "2026-11-02")}
        kept, omit = crawl.playable_filter("radio1", "x", self.eps("2026-11-03", "2026-11-02", "2026-11-01"), cache)
        self.assertEqual([e["date"] for e in kept], ["2026-11-01"])
        self.assertFalse(omit)

    def test_same_date_rows_count_as_one_date(self):
        cache = {crawl.playable_key("radio1", "x", d): {"ok": False}
                 for d in ("2026-11-03", "2026-11-02", "2026-11-01")}
        eps = self.eps("2026-11-03", "2026-11-03", "2026-11-02", "2026-11-01", "2026-10-31")
        kept, omit = crawl.playable_filter("radio1", "x", eps, cache)
        self.assertTrue(omit)
        self.assertEqual([e["date"] for e in kept], ["2026-10-31"])


if __name__ == "__main__":
    unittest.main()
