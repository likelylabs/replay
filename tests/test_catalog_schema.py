"""S31 — the built catalog: Metro publishes segmentMinutes 30 (no streamVersion
bump) and validates against schema/*.json; no published episode title is the
programme name or a >= 80 % label. Builds from the committed data/ into a temp
dir — the committed index.json / prog/ are never touched (the crawl Action owns
them). Full JSON-schema validation needs `jsonschema` (skipped without it)."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import build_catalog  # noqa: E402
import common  # noqa: E402

try:
    import jsonschema
except ImportError:          # the repo is stdlib-only; the Action never runs these
    jsonschema = None

INDEX_SCHEMA = json.loads((REPO / "schema" / "index.schema.json").read_text(encoding="utf-8"))
PROG_SCHEMA = json.loads((REPO / "schema" / "programme.schema.json").read_text(encoding="utf-8"))


def metro_schema():
    for branch in INDEX_SCHEMA["properties"]["channels"]["items"]["oneOf"]:
        if branch["properties"]["source"].get("const") == "metro":
            return branch
    raise AssertionError("no metro branch in index.schema.json")


def minimal_index(segment_minutes):
    return {"generatedAt": "2026-09-24T03:50:00+08:00", "crawlerVersion": 1, "streamVersion": 1,
            "retentionMonths": 12, "streamTemplate": common.STREAM_TEMPLATE,
            "channels": [
                {"id": "radio1", "source": "rthk", "browse": "programme", "name_zh": "第一台",
                 "name_en": "Radio 1", "programmes": [
                     {"slug": "Free_as_the_wind", "title_zh": "講東講西", "title_en": "講東講西",
                      "active": True, "latestDate": "2026-09-23", "episodeCount": 1}]},
                {"id": "metro_mf", "source": "metro", "browse": "bytime", "name_zh": "新城財經台",
                 "name_en": "Metro Finance", "freq": "104", "earliestDate": "2026-06-01",
                 "latestDate": "2026-09-23", "streamTemplate": common.METRO_TEMPLATE,
                 "segmentMinutes": segment_minutes}]}


class SchemaShape(unittest.TestCase):
    def test_segment_minutes_enum(self):
        seg = metro_schema()["properties"]["segmentMinutes"]
        self.assertEqual(seg["enum"], [30, 60])
        self.assertIn(common.METRO_SEGMENT_MINUTES, seg["enum"])

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_minimal_index_30_and_60_valid_other_values_not(self):
        jsonschema.validate(minimal_index(30), INDEX_SCHEMA)
        jsonschema.validate(minimal_index(60), INDEX_SCHEMA)   # older catalogs stay valid
        for bad in (45, 15, "30"):
            with self.subTest(bad=bad), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(minimal_index(bad), INDEX_SCHEMA)

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_empty_episode_title_is_valid(self):
        jsonschema.validate({"channel": "radio1", "slug": "x", "streamVersion": 1,
                             "episodes": [{"id": "1", "title": "", "date": "2026-09-23"}]},
                            PROG_SCHEMA)


class BuiltCatalog(unittest.TestCase):
    """One build of the committed data/ into a temp dir, shared by the checks."""

    @classmethod
    def setUpClass(cls):
        run = common.read_json(common.LAST_RUN_PATH, {}) or {}
        started = (run.get("startedAt") or "")[:10]
        if not started:
            raise unittest.SkipTest("no data/last-run.json to date the build")
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "last-run.json").write_text("{}", encoding="utf-8")
        today = common.dt.date.fromisoformat(started)
        out = io.StringIO()
        with mock.patch.object(build_catalog, "INDEX_PATH", tmp / "index.json"), \
                mock.patch.object(build_catalog, "PROG_DIR", tmp / "prog"), \
                mock.patch.object(build_catalog, "LAST_RUN_PATH", tmp / "last-run.json"), \
                mock.patch.object(build_catalog, "today_hkt", lambda: today), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cls.rc = build_catalog.main()
        cls.log = out.getvalue()
        cls.index = common.read_json(tmp / "index.json")
        cls.progs = {p.relative_to(tmp / "prog").with_suffix("").as_posix(): json.loads(p.read_text(encoding="utf-8"))
                     for p in sorted((tmp / "prog").glob("*/*.json"))}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_build_passes_the_gate(self):
        self.assertEqual(self.rc, 0, self.log)
        self.assertTrue(self.progs)

    def test_metro_publishes_30_minute_slots_and_stream_version_1(self):
        metro = [c for c in self.index["channels"] if c["source"] == "metro"]
        self.assertEqual(len(metro), 3)
        self.assertEqual({c["segmentMinutes"] for c in metro}, {30})
        self.assertEqual({c["streamTemplate"] for c in metro}, {common.METRO_TEMPLATE})
        self.assertEqual(self.index["streamVersion"], 1)
        self.assertEqual({d["streamVersion"] for d in self.progs.values()}, {1})

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_index_validates(self):
        jsonschema.validate(self.index, INDEX_SCHEMA)

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_every_prog_file_validates(self):
        v = jsonschema.Draft7Validator(PROG_SCHEMA)
        bad = {k: next(iter(v.iter_errors(d))).message for k, d in self.progs.items() if not v.is_valid(d)}
        self.assertEqual(bad, {})

    def test_no_published_title_is_the_programme_name_or_a_label(self):
        names = {(c["id"], p["slug"]): [build_catalog._tkey(p["title_zh"]), build_catalog._tkey(p["title_en"])]
                 for c in self.index["channels"] if c["source"] == "rthk" for p in c["programmes"]}
        offenders = []
        for key, doc in self.progs.items():
            ch, slug = key.split("/", 1)
            eps = doc["episodes"]
            keys = [build_catalog._tkey(e["title"]) for e in eps]
            counts = Counter(k for k in keys if k)
            for e, k in zip(eps, keys):
                if not e["title"]:
                    continue
                if not k or any(k in n for n in names[(ch, slug)] if n) or \
                        (len(eps) >= 4 and counts[k] >= 0.8 * len(eps)):
                    offenders.append((key, e["id"], e["title"]))
        self.assertEqual(offenders[:10], [])

    def test_real_titles_survive(self):
        titled = sum(1 for d in self.progs.values() for e in d["episodes"] if e["title"])
        total = sum(len(d["episodes"]) for d in self.progs.values())
        self.assertGreater(titled, 0)
        self.assertLess(titled, total)
        self.assertIn("uninformative episode titles blanked", self.log)


if __name__ == "__main__":
    unittest.main()
