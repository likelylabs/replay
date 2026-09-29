#!/usr/bin/env python3
"""
PROCESS leg: turn data/ into the published catalog — index.json + prog/{ch}/
{slug}.json (REPLAY.md §3) — behind the publish gate (§2.5).

This is the SANCTIONED WRITE PATH for index.json and prog/ (tenet #5). It
never touches the network. If the gate fails, nothing is written: the
committed last-good catalog keeps serving from Pages.

Gate:
  - the last crawl must not have tripped the circuit breaker
  - every programme/episode validates against the shape below
  - programmes with 0 episodes in the retention window are dropped
  - uninformative episode titles (the programme name, a fixed label) are
    blanked to "" so the app falls back to the programme title (S31b)
  - a programme's "_s" small-thumbnail logo publishes as its full-size photo
    once crawl.py has verified it (S37b; `logo` stays a URL string)
  - safety floor vs last-good: >= 90% of programmes and >= 80% of episodes,
    else refuse (a bad crawl must never nuke the section)

Stdlib only. Run from the repo root:  python3 tools/build_catalog.py
"""
import datetime as dt
import json
import re
import sys
import unicodedata
from collections import Counter

from common import (CRAWLER_VERSION, HKT, INDEX_PATH, LAST_RUN_PATH, METRO_CHANNELS,
                    METRO_SEGMENT_MINUTES, METRO_TEMPLATE, METRO_WINDOW_PATH,
                    OVERRIDES_PATH, PROGRAMMES_PATH, PROG_DIR, RETENTION_MONTHS,
                    RTHK_CHANNELS, RTHK_DATA, STREAM_TEMPLATE, STREAM_VERSION,
                    log, months_back, read_json, today_hkt)

ACTIVE_WINDOW_DAYS = 8          # seen on a channel schedule within this many days ⇒ active
FLOOR_PROGRAMMES = 0.90
FLOOR_EPISODES = 0.80
SLUG_RE = re.compile(r"^[A-Za-z0-9_]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^\d+$")
NON_WORD_RE = re.compile(r"[\W_]+")
LABEL_SHARE = 0.80              # a title on >= 80 % of a programme's episodes is a label...
LABEL_MIN_EPISODES = 4          # ...once the programme has at least this many episodes


def fail(msg):
    print(f"GATE FAILED — catalog NOT written: {msg}", file=sys.stderr)
    return 1


def load_programme(p, window_months, overrides, today):
    """Merge the cached months for one programme into a sorted episode list."""
    ch, slug = p["channel"], p["slug"]
    by_id = {}
    for m in window_months:
        cache = read_json(RTHK_DATA / ch / slug / f"{m}.json")
        if not cache:
            continue
        for ep in cache.get("episodes", []):
            by_id.setdefault(ep["id"], ep)
    episodes = sorted(by_id.values(), key=lambda e: (e["date"], e["id"]), reverse=True)
    out = []
    for ep in episodes:
        rec = {"id": ep["id"], "title": ep.get("title") or "", "date": ep["date"]}
        if ep.get("parts"):
            rec["parts"] = ep["parts"]
        ov = overrides.get(f"{ch}/{slug}/{ep['id']}")
        if ov:
            rec["streamUrl"] = ov
        out.append(rec)
    return out


def _tkey(s):
    """Title comparison key: NFKC → drop punctuation / spaces / underscores →
    casefold. '《講東講西》' / '講東講西 ' / 'ＦＲＥＥ as_the-Wind' fold alike."""
    return NON_WORD_RE.sub("", unicodedata.normalize("NFKC", s or "")).casefold()


def blank_non_titles(programme, episodes):
    """S31b: RTHK often sends the programme name or a fixed label (節目內容,
    歌曲選播, a host's show name) as the episode `title`. Blank those to "" —
    the app then falls back to the programme title — and keep real titles.
    An episode title is blanked when its key is empty, is contained in the
    programme's title_zh / title_en key, or (programme has >= 4 episodes) is
    carried by >= 80 % of them. Mutates `episodes`; returns how many changed."""
    names = [k for k in (_tkey(programme.get("title_zh")), _tkey(programme.get("title_en"))) if k]
    keys = [_tkey(ep.get("title")) for ep in episodes]
    n = len(episodes)
    labels = set()
    if n >= LABEL_MIN_EPISODES:
        labels = {k for k, c in Counter(k for k in keys if k).items() if c >= LABEL_SHARE * n}
    blanked = 0
    for ep, k in zip(episodes, keys):
        if not k or k in labels or any(k in name for name in names):
            if ep.get("title"):
                blanked += 1
            ep["title"] = ""
    return blanked


def published_logo(p):
    """S37b: the full-size photo crawl.py verified for this exact "_s"
    thumbnail (memo `logoFull`, still `from` the current logo), else the
    crawler's own pick. Always a URL string or None — the schema is unchanged."""
    logo = p.get("logo")
    memo = p.get("logoFull") or {}
    full = memo.get("url")
    if logo and memo.get("from") == logo and isinstance(full, str) and full.startswith("https://"):
        return full
    return logo


def validate_programme(p, episodes):
    problems = []
    if p["channel"] not in RTHK_CHANNELS:
        problems.append("unknown channel")
    if not SLUG_RE.match(p["slug"]):
        problems.append("bad slug")
    for ep in episodes:
        if not ID_RE.match(ep["id"]):
            problems.append(f"bad episode id {ep['id']!r}")
        if not DATE_RE.match(ep["date"]):
            problems.append(f"bad episode date {ep['date']!r}")
        if "streamUrl" in ep and not ep["streamUrl"].startswith("https://"):
            problems.append(f"bad streamUrl on {ep['id']}")
    return problems


def main():
    today = today_hkt()
    run = read_json(LAST_RUN_PATH, {})
    if run.get("aborted", "") and str(run["aborted"]).startswith("CircuitOpen"):
        return fail(f"last crawl tripped the circuit breaker: {run['aborted']}")

    programmes = read_json(PROGRAMMES_PATH, {})
    overrides = read_json(OVERRIDES_PATH, {})
    metro = read_json(METRO_WINDOW_PATH, {})
    window_months = months_back(RETENTION_MONTHS, today)
    active_cutoff = today - dt.timedelta(days=ACTIVE_WINDOW_DAYS)

    channels = []
    prog_files = {}
    dropped = []          # malformed programmes: dropped with a warning, not fatal
    candidates = 0
    total_eps = 0
    blanked_titles = 0
    for ch, meta in RTHK_CHANNELS.items():
        entries = []
        for p in sorted(programmes.values(), key=lambda p: p["slug"]):
            if p["channel"] != ch:
                continue
            episodes = load_programme(p, window_months, overrides, today)
            if not episodes:
                continue                                   # dormant: dropped (§2.5)
            # §2.1 fallbacks: no Chinese title anywhere → humanised slug.
            title_zh = (p.get("title_zh") or "").strip() or p["slug"].replace("_", " ")
            title_en = (p.get("title_en") or "").strip() or title_zh   # §2.1 fallback
            # Against the PUBLISHED titles — the ones the app falls back to.
            blanked_titles += blank_non_titles({"title_zh": title_zh, "title_en": title_en}, episodes)
            probs = validate_programme(p, episodes)
            if probs:
                dropped += [f"{ch}/{p['slug']}: {x}" for x in probs]
                continue
            candidates += 1
            seen = p.get("lastSeenActive")
            active = bool(seen) and dt.date.fromisoformat(seen) >= active_cutoff
            entry = {"slug": p["slug"], "title_zh": title_zh, "title_en": title_en,
                     "active": active, "latestDate": episodes[0]["date"],
                     "episodeCount": len(episodes)}
            logo = published_logo(p)
            if logo:
                entry["logo"] = logo
            if p.get("streamDrift"):
                entry["streamOverride"] = True
            entries.append(entry)
            prog_files[(ch, p["slug"])] = {"channel": ch, "slug": p["slug"],
                                           "streamVersion": STREAM_VERSION,
                                           "episodes": episodes}
            total_eps += len(episodes)
        entries.sort(key=lambda e: (not e["active"], e["title_zh"]))
        channels.append({"id": ch, "source": "rthk", "browse": "programme",
                         "name_zh": meta["name_zh"], "name_en": meta["name_en"],
                         "programmes": entries})

    for freq, meta in METRO_CHANNELS.items():
        w = metro.get(freq)
        if not w:
            log(f"metro {freq}: no window yet — channel omitted this build")
            continue
        channels.append({"id": meta["id"], "source": "metro", "browse": "bytime",
                         "name_zh": meta["name_zh"], "name_en": meta["name_en"],
                         "freq": freq, "earliestDate": w["earliestDate"],
                         "latestDate": w["latestDate"],
                         "streamTemplate": METRO_TEMPLATE,
                         "segmentMinutes": METRO_SEGMENT_MINUTES})

    if dropped:
        for x in dropped:
            print("  - dropped: " + x, file=sys.stderr)
        if len(dropped) > max(3, 0.10 * (candidates + len(dropped))):
            return fail(f"{len(dropped)} programme(s) malformed — more than 10%, refusing")

    n_prog = len(prog_files)
    if n_prog == 0:
        return fail("no RTHK programme has any episode — refusing to publish an empty catalog")

    # Safety floor against last-good (the committed index.json).
    last = read_json(INDEX_PATH)
    if last:
        last_prog = sum(len(c.get("programmes", [])) for c in last.get("channels", []))
        last_eps = sum(p.get("episodeCount", 0) for c in last.get("channels", [])
                       for p in c.get("programmes", []))
        if last_prog and n_prog < FLOOR_PROGRAMMES * last_prog:
            return fail(f"programmes {n_prog} < {FLOOR_PROGRAMMES:.0%} of last-good {last_prog}")
        if last_eps and total_eps < FLOOR_EPISODES * last_eps:
            return fail(f"episodes {total_eps} < {FLOOR_EPISODES:.0%} of last-good {last_eps}")

    index = {"generatedAt": dt.datetime.now(HKT).isoformat(timespec="seconds"),
             "crawlerVersion": CRAWLER_VERSION, "streamVersion": STREAM_VERSION,
             "retentionMonths": RETENTION_MONTHS,
             "streamTemplate": STREAM_TEMPLATE, "channels": channels}

    # Write. prog/ is fully regenerated: stale files (programmes that aged
    # out) are removed so the published tree equals the catalog.
    existing = set(PROG_DIR.glob("*/*.json"))
    written = set()
    for (ch, slug), doc in prog_files.items():
        path = PROG_DIR / ch / f"{slug}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        written.add(path)
    for stale in existing - written:
        stale.unlink()
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    size_kb = INDEX_PATH.stat().st_size // 1024
    log(f"index.json written: {n_prog} programmes / {total_eps} episodes across "
        f"{sum(1 for c in channels if c['source']=='rthk')} RTHK + "
        f"{sum(1 for c in channels if c['source']=='metro')} Metro channels, {size_kb} KB; "
        f"{len(written)} prog files, {len(existing - written)} stale removed; "
        f"{blanked_titles} uninformative episode titles blanked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
