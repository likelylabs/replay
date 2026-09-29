#!/usr/bin/env python3
"""
FETCH leg of the replay pipeline (REPLAY.md §2.1–2.4). Talks to the
broadcasters; writes only under data/. Never writes index.json or prog/ —
that is tools/build_catalog.py's job (one sanctioned write path, tenet #5).

Daily run (all bounded by --budget requests, serial ~1 req/s):
  1. RTHK discovery — sitemap slugs ∪ channel-page slugs (news bulletins are
     schedule-only), today's schedule XML for English titles, programme pages
     for logo + canonical title (new slugs first, then a rolling 30-day
     refresh, capped per run).
  1b. Logo upgrade (S37b/S37c) — a publishing programme whose logo is one of
     RTHK's small variants ("{id}_115.jpg" or an "_s" thumbnail) gets a HEAD
     on each larger artwork in the same directory, in order, until one is a
     usable image: the 720 px square "programme_photo_l.jpg", the square
     original "programme_photo.jpg" (<= 500 KB), and for "_s" the S37b sibling
     ("_s" dropped). The answer — or "none larger" — is memoised in programmes.json
     `logoFull` and re-checked at most every 30 days, so a normal day sends
     few; at most 60 programmes a run, and the step stops itself after 3
     failed HEADs (it can never trip the breaker on its own). "Nothing
     larger" needs evidence — a control HEAD on the small logo itself, a
     second run before a published upgrade is withdrawn, and a stop after 5
     all-miss programmes in a row — because the storage answers a denied
     request exactly as a missing object. `logo` stays the crawler's raw
     pick; build_catalog publishes the verified URL.
  2. RTHK enumeration — for every programme: current + previous month always
     (catchUp?m=&page= walked to nextPage=-1); older months inside the
     12-month window only when missing from the cache (backfill drains over
     runs until the budget is spent). Months are cached immutably at
     data/rthk/{ch}/{slug}/{YYYYMM}.json.
  3. Stream spot-check — a sample of recent episodes per channel against the
     derived Akamai template; on a miss, getEpisode's authoritative `file:`
     is stored as a per-episode override (data/rthk/overrides.json).
  4. Metro window — binary-search the earliest day still served per
     frequency (probing its 08:00 slot); confirm the latest day; one HEAD on a
     :30 slot per frequency as the half-hour grid canary (S31, warn-only).
     (data/metro/window.json)
Writes data/last-run.json so the build gate can refuse to publish a run that
aborted (circuit breaker) or drifted.

Usage: python3 tools/crawl.py [--budget N] [--pace S] [--only radio1,pth]
                              [--limit N] [--no-backfill] [--skip-metro]
"""
import argparse
import datetime as dt
import html
import random
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

from common import (BudgetExhausted, CircuitOpen, Client, INCREMENTAL_MONTHS,
                    METRO_BITRATE_BPS, METRO_CHANNELS, METRO_DATA, METRO_SEGMENT_MINUTES,
                    METRO_TEMPLATE, METRO_WINDOW_PATH,
                    OVERRIDES_PATH, PROGRAMMES_PATH, RETENTION_MONTHS, RTHK_CHANNELS,
                    RTHK_DATA, STREAM_TEMPLATE, WWW, HKT, LAST_RUN_PATH, iso_from_ddmmyyyy,
                    log, month_key, months_back, read_json, today_hkt, write_json)

META_REFRESH_DAYS = 30      # re-read a programme page this often
META_REFRESH_CAP = 25       # ...but at most this many refreshes per run (new slugs exempt)
SPOT_CHECK_PER_CHANNEL = 2
SLUG_RE = re.compile(r"^[A-Za-z0-9_]+$")
METRO_GRID_TOLERANCE = 0.05   # canary: warn when a slot's size-derived length is off by > 5 %
# S37b/S37c logo upgrade. RTHK keeps every programme's artwork in ONE
# directory beside the small logo the programme page shows (measured
# 2026-09-29, 75 polite requests over 9 + 25 programmes on all six channels):
#   {id}_115.jpg           115 x 115   the proLogoImg of 206 of the 247 small logos
#   programme_photo_s.jpg  272 x 272   the proLogoImg of 37
#   {id}_1920_s.jpg        480 x 270   the proLogoImg of 4 (16:9)
#   programme_photo_l.jpg  720 x 720   47-95 KB
#   programme_photo.jpg    the square original: 1134-3001 px, 70 KB-19.4 MB
#                          (31 of 33 sampled "_115" directories hold one)
#   {id}_1920.jpg          1920 x 1080 (16:9), 92-268 KB
# A missing object answers 403 application/xml (the storage's AccessDenied).
# The og:image is a ~1600 x 470 slider and no programme page links a podcast
# feed, so neither is a candidate. The client centre-crops to a square, so a
# square is preferred over a banner; every candidate's square side is >= 2x
# the small logo's (115 -> 720+, 272 -> 720+, 270 -> 720+).
# The 720 px rendition goes FIRST, not the original: 720 px is already >= 2x
# every tile (120 pt @3x = 360 px) and above the 169 pt hero @3x (507 px),
# ~2 MB decoded, where a 3001 px original decodes to ~36 MB per tile on a
# client that draws at full size (the iOS tiles do). The original is asked
# only where no 720 px rendition answers, and only under LOGO_MAX_BYTES.
SMALL_115_RE = re.compile(r"^\d+_115\.jpe?g$", re.IGNORECASE)      # on the file name
SMALL_LOGO_RE = re.compile(r"(?<=[^/])_s(\.jpe?g)$", re.IGNORECASE)  # "_s": the S37b sibling drops it
LOGO_PHOTO = "programme_photo.jpg"       # the square original
LOGO_PHOTO_L = "programme_photo_l.jpg"   # its 720 px square rendition
LOGO_MAX_BYTES = 500_000      # a candidate heavier than this is skipped (the 720 px runs 47-95 KB,
                              # the 1920 banner 92-268 KB, originals 70 KB-19.4 MB)
LOGO_MIN_BYTES = 15_000       # ...and one lighter than this is a stub, not an upgrade (the 272 px
                              # "_s" thumbnail is ~14 KB; the lightest real candidate measured, 47 KB)
LOGO_RECHECK_DAYS = 30        # re-verify a memoised answer at most this often
LOGO_PROGRAMMES_PER_RUN = 60  # programmes checked per run (<= 4 HEADs each, the control included);
                              # the first pass spreads over ~5 runs
LOGO_PROBE_MAX_FAILURES = 3   # the step stops after this many failed HEADs in one run
LOGO_HEAD_ANSWERS = (200, 403, 410)   # returned as answers, never retried or counted by the client
LOGO_MISS_STREAK = 5          # this many programmes in a row with nothing larger stop the step (~6 % miss naturally)


def clean_page_title(t):
    """'香港電台網站 : 第一台|講東講西' / 'rthk.hk : Radio 1|…' / '午間新聞天地 | 所有集數 - RTHK' → title"""
    t = t.strip()
    t = re.sub(r"^(香港電台網站|rthk\.hk)\s*:\s*[^|]*\|\s*", "", t)
    t = re.sub(r"\s*\|\s*所有集數\s*-\s*RTHK$", "", t)
    t = re.sub(r"\s*-\s*RTHK$", "", t)
    if re.match(r"^(香港電台網站|rthk\.hk)\b", t) or t in ("電台", "Radio"):
        return ""                                   # site-only title = no title
    return t.strip()


def ajax_headers(ch, slug):
    return {"X-Requested-With": "XMLHttpRequest",
            "Referer": f"{WWW}/radio/{ch}/programme/{slug}"}


# ---------------------------------------------------------------------------
# 1. Discovery
# ---------------------------------------------------------------------------
def discover(client, programmes, only, run, limit=0):
    today = today_hkt().isoformat()
    st, _, body = client.get(f"{WWW}/sitemap.xml")
    if st != 200:
        raise RuntimeError(f"sitemap.xml {st}")
    sm = body.decode("utf-8", "replace")
    found = {}
    for ch in RTHK_CHANNELS:
        if only and ch not in only:
            continue
        for slug in set(re.findall(rf"/radio/{ch}/programme/([A-Za-z0-9_]+)", sm)):
            found[(ch, slug)] = {"source": "sitemap"}
    run["sitemap_programmes"] = len(found)

    for ch in RTHK_CHANNELS:
        if only and ch not in only:
            continue
        st, _, body = client.get(f"{WWW}/radio/{ch}")
        if st != 200:
            log(f"  channel page {ch}: {st} (active flags for {ch} not refreshed this run)")
            run["warnings"].append(f"channel page {ch} {st}")
            continue
        page = body.decode("utf-8", "replace")
        for m in re.finditer(r"<a data-f='([A-Za-z0-9_]+)'[^>]*title=\"([^\"]*)\"", page):
            slug, title = m.group(1), html.unescape(m.group(2)).strip()
            found.setdefault((ch, slug), {"source": "channel"})
            found[(ch, slug)]["active_today"] = True
            if title:
                found[(ch, slug)]["title_zh"] = title


    # "Now airing" XML — one call covers every channel (it ignores c/p). Each
    # item carries its OWN channel, which matters for simulcasts (a radio4
    # programme airing on radio1 must not be registered under radio1).
    st, _, body = client.get(f"{WWW}/radio/get_radio_programme_info?c=radio1&p=x",
                             ajax_headers("radio1", "x"))
    if st == 200:
        try:
            root = ET.fromstring(body.decode("utf-8", "replace"))
            for item in root.iter("scheduleItem"):
                ich = (item.findtext("channel") or "").strip()
                slug = (item.findtext("programmeFolder") or "").strip()
                if ich not in RTHK_CHANNELS or not SLUG_RE.match(slug) or (only and ich not in only):
                    continue
                zh = (item.findtext("programmeTitleChi") or "").strip()
                en = (item.findtext("programmeTitleEng") or "").strip()
                rec = found.setdefault((ich, slug), {"source": "schedule"})
                rec["active_today"] = True
                if zh and "title_zh" not in rec:
                    rec["title_zh"] = zh
                if en and en != zh and re.search(r"[A-Za-z]", en):
                    rec["title_en"] = en
                thumb = (item.findtext("thumbnail") or "").strip()
                if thumb.startswith("/") and f"/{ich}/" in thumb:
                    rec.setdefault("logo_sched", "https://webstatic.rthk.hk" + thumb)
        except ET.ParseError as e:
            run["warnings"].append(f"schedule xml: {e}")

    # Merge into the persistent programme registry.
    for (ch, slug), rec in found.items():
        key = f"{ch}/{slug}"
        p = programmes.setdefault(key, {"channel": ch, "slug": slug, "discoveredAt": today})
        if rec.get("active_today"):
            p["lastSeenActive"] = today
        for k in ("title_zh", "title_en"):
            if rec.get(k):
                p[k] = rec[k]
        if rec.get("logo_sched") and not p.get("logo"):
            p["logo"] = rec["logo_sched"]
        p["lastSeenAt"] = today
    run["programmes_known"] = len(programmes)

    # Programme pages → canonical title + logo. New slugs first, then rolling.
    def needs_meta(p):
        if only and p["channel"] not in only:
            return False
        last = p.get("lastMetaRefresh")
        if not last:
            return True
        return (today_hkt() - dt.date.fromisoformat(last)).days >= META_REFRESH_DAYS
    queue = [p for p in programmes.values() if needs_meta(p)]
    fresh = [p for p in queue if not p.get("lastMetaRefresh")]
    stale = [p for p in queue if p.get("lastMetaRefresh")]
    random.shuffle(stale)
    todo = fresh + stale[:META_REFRESH_CAP]
    if limit:                      # dev: keep a smoke run small
        todo = todo[:limit]
    for p in todo:
        ch, slug = p["channel"], p["slug"]
        st, _, body = client.get(f"{WWW}/radio/{ch}/programme/{slug}")
        p["lastMetaRefresh"] = today
        if st != 200:
            p["pageStatus"] = st
            continue
        p.pop("pageStatus", None)
        page = body.decode("utf-8", "replace")
        # Title precedence: schedule/channel page (freshest, already set this
        # run) > the logo's title attribute > the cleaned <title>. The page is
        # only a fallback, so it never overwrites a title we already hold.
        page_title = ""
        m = re.search(r'<img class="proLogoImg[^"]*"[^>]*title="([^"]+)"', page)
        if m:
            page_title = html.unescape(m.group(1)).strip()
        if not page_title:
            m = re.search(r"<title>([^<]+)</title>", page)
            if m:
                page_title = clean_page_title(html.unescape(m.group(1)))
        if page_title and not p.get("title_zh"):
            p["title_zh"] = page_title
        m = re.search(r'<img class="proLogoImg[^"]*" src="([^"]+)"', page)
        if m:
            p["logo"] = html.unescape(m.group(1))
        else:
            m = re.search(r'property="og:image" content="([^"]+)"', page)
            if m and "programme" in m.group(1):
                p["logo"] = html.unescape(m.group(1))
    run["meta_refreshed"] = len(todo)


# ---------------------------------------------------------------------------
# 1b. Logo upgrade (S37b/S37c)
# ---------------------------------------------------------------------------
def logo_candidates(url):
    """The larger artworks worth one HEAD each, best first, for an RTHK small
    logo — [] for anything else, so every other logo is never requested.
    Small = an https URL on an rthk.hk host (no query / fragment) whose file
    is "{id}_115.jpg" or ends in "_s.jpg" / "_s.jpeg". Candidates, all in the
    logo's own directory: the 720 px square rendition, the square original,
    then (for "_s" only) the S37b sibling with the "_s" dropped — which for
    '{id}_1920_s.jpg' is the 1920 x 1080 banner, the same shape as today's
    logo; for 'programme_photo_s.jpg' it repeats the original and is dropped."""
    if not isinstance(url, str):
        return []
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "rthk.hk" or host.endswith(".rthk.hk")):
        return []
    if parts.query or parts.fragment:
        return []
    folder, _, name = url.rpartition("/")
    if SMALL_115_RE.match(name):
        own = []
    elif SMALL_LOGO_RE.search(name):
        own = [folder + "/" + SMALL_LOGO_RE.sub(r"\1", name)]
    else:
        return []
    out = []
    for cand in (f"{folder}/{LOGO_PHOTO_L}", f"{folder}/{LOGO_PHOTO}", *own):
        if cand != url and cand not in out:
            out.append(cand)
    return out


def has_cached_episodes(p, window):
    """True when a cached month in the retention window holds an episode —
    i.e. build_catalog would publish the programme (it drops the rest)."""
    return any((read_json(month_path(p["channel"], p["slug"], m)) or {}).get("episodes")
               for m in window)


def logo_memo_answers(memo, logo):
    """The memo answers for this exact logo under TODAY's rule: it was written
    `from` this logo, and its url is None ("nothing larger") or one of
    logo_candidates(logo) with a recorded size in [LOGO_MIN_BYTES, LOGO_MAX_BYTES]. A memo
    an earlier rule wrote (a heavier cap, a URL off the derivation) answers
    nothing: build_catalog does not publish it and the crawl asks again first.
    The one predicate both legs share, so the publish gate re-checks the rule."""
    if not isinstance(memo, dict) or not isinstance(logo, str) or not logo or memo.get("from") != logo:
        return False
    url = memo.get("url")
    if url is None:
        return True
    size = memo.get("bytes")
    return (url in logo_candidates(logo) and isinstance(size, int) and not isinstance(size, bool)
            and LOGO_MIN_BYTES <= size <= LOGO_MAX_BYTES)


def logo_memo_current(memo, logo, today):
    """A memo answers for this exact logo under today's rule and is younger
    than LOGO_RECHECK_DAYS."""
    if not logo_memo_answers(memo, logo):
        return False
    try:
        age = (today - dt.date.fromisoformat(memo.get("checkedAt") or "")).days
    except (TypeError, ValueError):
        return False
    return 0 <= age < LOGO_RECHECK_DAYS


def logo_head_verdict(st, ct, same_url, length):
    """What one HEAD on a candidate says: "hit" (use it), "miss" (a definitive
    no for this candidate — try the next) or "none" (no answer this run).
    A hit is a 200 image served from the candidate URL itself (no redirect)
    with a Content-Length in [LOGO_MIN_BYTES, LOGO_MAX_BYTES] — an unsized
    answer or a stub smaller than the thumbnail it would replace is a miss,
    as is anything heavier than a tile should fetch. A 404 / 410, the storage's
    403 application/xml (how webstatic answers a missing object) and any
    other 4xx but 429 are misses. A transport error, 429, 5xx or a 403 that
    is not the storage's (a block, not an answer) is no answer."""
    ctype = (ct or "").split(";")[0].strip().lower()
    if st == 200:
        usable = ctype.startswith("image/") and same_url and LOGO_MIN_BYTES <= length <= LOGO_MAX_BYTES
        return "hit" if usable else "miss"
    if st == 403:
        return "miss" if "xml" in ctype else "none"
    if 400 <= st < 500 and st != 429:
        return "miss"
    return "none"


def logo_control_ok(st, ct, same_url):
    """The control HEAD on the programme's own small logo (known to exist)
    answered as it should: a 200 image from that URL itself. Anything else
    means the host is not answering us honestly this run."""
    ctype = (ct or "").split(";")[0].strip().lower()
    return st == 200 and ctype.startswith("image/") and same_url


def _iso_date(s):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def upgrade_small_logos(client, programmes, only, run, cap=LOGO_PROGRAMMES_PER_RUN):
    """For each publishing programme whose `logo` is an RTHK small variant
    (logo_candidates), HEAD its candidates in order (one try each, no retry)
    through the shared client — its pacing, UA, budget and breaker — with the
    programme page as Referer, stop at the first hit, and memoise the answer
    as p["logoFull"] = {"from": the small URL, "url": the hit or None when no
    candidate is usable, "status", ["bytes"], "checkedAt"}. A negative memo
    is kept like a positive one, so a programme with nothing larger is asked
    again only after LOGO_RECHECK_DAYS. No answer on any HEAD (see
    logo_head_verdict) leaves the programme's previous memo untouched and the
    next run retries — the step fails soft, today's logo always stands.
    `p["logo"]` is never changed here: build_catalog publishes memo["url"]
    while memo["from"] still equals the logo, so a meta refresh that moves the
    logo simply retires the memo. Never-checked programmes go first, then the
    oldest answers; a programme asked without an answer is stamped
    `logoTriedAt` and rotates behind them, so a few directories that keep
    failing can never starve the queue. At most `cap` programmes a run, and
    the step stops after LOGO_PROBE_MAX_FAILURES failed HEADs so a blocked
    image host costs a handful of requests, never the run. The storage's "missing" 403 is an
    answer (LOGO_HEAD_ANSWERS), so the client neither retries nor counts it;
    a response the step judges a failure although the client took it as an
    answer (a 403 block page) is counted on the client (Client.count_failure),
    so the run ledger and the shared breaker see it.

    "Nothing larger" is believed only with evidence, because the storage
    answers a denied request (IP, Referer, UA) exactly as a missing object
    and a challenge page is a 200: (a) when every candidate misses, ONE
    control HEAD asks for the small logo itself — not a 200 image means no
    answer (a failure; nothing written); (b) a memo that publishes a larger
    artwork is downgraded only by a SECOND all-miss on a later run — the
    first marks it `missAt` and it keeps publishing; (c) LOGO_MISS_STREAK
    programmes in a row with nothing larger stop the step with a warning
    and none of them is memoised. A hit vouches for the host and writes the
    all-miss answers held before it."""
    today = today_hkt()
    iso = today.isoformat()
    window = months_back(RETENTION_MONTHS, today)
    small, due = [], []
    for key, p in sorted(programmes.items()):
        if only and p["channel"] not in only:
            continue
        logo = p.get("logo")
        cands = logo_candidates(logo)
        if not cands:
            p.pop("logoFull", None)                # not (or no longer) a small logo
            p.pop("logoTriedAt", None)
            continue
        if not has_cached_episodes(p, window):
            continue                               # dormant: never published, not worth a request
        small.append(p)
        memo = p.get("logoFull")
        if logo_memo_current(memo, logo, today):
            continue
        last = str(memo.get("checkedAt") or "") if logo_memo_answers(memo, logo) else ""
        last = max(last, str(p.get("logoTriedAt") or ""))   # a programme asked without an answer rotates back
        due.append((last, key, p, cands))
    due.sort(key=lambda t: (t[0], t[1]))

    heads = checked = answered = failed = 0
    held = []                                      # all-miss answers, written once a hit or the end vouches
    tripped = False

    def head(url, referer):
        nonlocal heads
        f0 = client.failures
        st, ct, _ = client.get(url, referer, method="HEAD", tries=1, ok=LOGO_HEAD_ANSWERS)
        heads += 1
        return st, ct, client.failures > f0

    def failure(counted):
        nonlocal failed
        failed += 1
        if not counted:
            client.count_failure()                 # an "answer" the step judged a block

    def write(p, memo):
        nonlocal answered
        p["logoFull"] = memo
        p.pop("logoTriedAt", None)
        answered += 1

    def write_miss(key, p, memo):
        prev = p.get("logoFull")
        if logo_memo_answers(prev, memo["from"]) and prev["url"]:
            first = _iso_date(prev.get("missAt"))
            if first is None or first >= today:    # (b) the first all-miss only marks it
                prev["missAt"] = iso if first is None else prev["missAt"]   # stays due, near the front
                log(f"  logo {key}: nothing larger today — keeps {prev['url'].rsplit('/', 1)[1]} "
                    f"until a later run agrees")
                return
        write(p, memo)
        log(f"  logo {key}: → keeps the small logo")

    for _, key, p, cands in due[:cap]:
        if failed >= LOGO_PROBE_MAX_FAILURES:
            break
        checked += 1
        logo = p["logo"]
        referer = {"Referer": f"{WWW}/radio/{p['channel']}/programme/{p['slug']}"}
        memo = {"from": logo, "url": None, "status": None, "checkedAt": iso}
        for cand in cands:
            if failed >= LOGO_PROBE_MAX_FAILURES:
                memo = None                        # stopped mid-programme: no answer
                break
            st, ct, counted = head(cand, referer)
            verdict = logo_head_verdict(st, ct, client.last_url == cand, client.last_length)
            if verdict == "none":
                failure(counted)
                log(f"  logo {key}: HEAD {st} on {cand.rsplit('/', 1)[1]} — no answer, retry next run")
                memo = None
                break
            if counted:                            # a 4xx miss the client counted (e.g. 400)
                failed += 1
            memo["status"] = st
            if verdict == "hit":
                memo.update(url=cand, bytes=client.last_length)
                break
        if memo is not None and memo["url"] is None:
            # (a) Every candidate missed: is the host answering us at all?
            if failed >= LOGO_PROBE_MAX_FAILURES:
                memo = None
            else:
                st, ct, counted = head(logo, referer)
                if not logo_control_ok(st, ct, client.last_url == logo):
                    failure(counted)
                    log(f"  logo {key}: nothing larger, but HEAD {st} on the logo itself — "
                        f"no answer, retry next run")
                    memo = None
        if memo is None:
            p["logoTriedAt"] = iso                 # the previous memo (if any) stands; rotate to the back
            continue
        if memo["url"] is None:
            held.append((key, p, memo))
            if len(held) >= LOGO_MISS_STREAK:      # (c) a run of misses is a block until proven otherwise
                tripped = True
                break
            continue
        for miss in held:
            write_miss(*miss)
        held = []
        write(p, memo)
        log(f"  logo {key}: → {memo['url'].rsplit('/', 1)[1]}")
    if tripped:
        for _, p, _ in held:
            p["logoTriedAt"] = iso
        run["warnings"].append(f"logo upgrade stopped: {LOGO_MISS_STREAK} programmes in a row had nothing "
                               f"larger although their own logos answered — a soft block or a changed "
                               f"layout? none of them memoised")
    else:
        for miss in held:
            write_miss(*miss)
    if failed >= LOGO_PROBE_MAX_FAILURES:
        run["warnings"].append(f"logo upgrade stopped after {LOGO_PROBE_MAX_FAILURES} failed HEADs "
                               f"({len(due) - answered} due left for the next run)")
    run["logo_small"] = len(small)
    run["logo_checked"] = checked
    run["logo_probes"] = heads
    run["logo_deferred"] = len(due) - answered
    run["logo_upgraded"] = sum(1 for p in small if logo_memo_answers(p.get("logoFull"), p["logo"])
                               and p["logoFull"]["url"])


# ---------------------------------------------------------------------------
# 2. Enumeration
# ---------------------------------------------------------------------------
def month_path(ch, slug, m):
    return RTHK_DATA / ch / slug / f"{m}.json"


def fetch_month(client, ch, slug, m):
    """Walk catchUp?m=&page= to nextPage=-1. Returns (ok, episodes)."""
    episodes, page, seen = [], 1, set()
    while True:
        st, d = client.get_json(f"{WWW}/radio/catchUp?c={ch}&p={slug}&m={m}&page={page}",
                                ajax_headers(ch, slug))
        if st != 200 or d is None:
            return False, episodes
        if d.get("status") != "1":          # {"status":"0"} = dormant, no episodes (valid)
            return True, episodes
        for e in d.get("content", []) or []:
            eid = str(e.get("id", "")).strip()
            try:
                date = iso_from_ddmmyyyy(e.get("date", ""))
            except ValueError:
                continue
            if not eid or eid in seen or not date.startswith(f"{m[:4]}-{m[4:]}"):
                continue
            seen.add(eid)
            ep = {"id": eid, "title": html.unescape((e.get("title") or "").strip()), "date": date}
            parts = [html.unescape(str(x)).strip() for x in (e.get("part") or []) if str(x).strip()]
            if parts:
                ep["parts"] = parts
            episodes.append(ep)
        nxt = d.get("nextPage")
        try:
            nxt = int(nxt)
        except (TypeError, ValueError):
            nxt = -1
        if nxt <= page:
            break
        page = nxt
        if page > 40:           # a month never needs 40 pages; guard a loop
            break
    return True, episodes


def enumerate_programmes(client, programmes, only, limit, backfill, run):
    today = today_hkt()
    window = months_back(RETENTION_MONTHS, today)
    incremental = window[:INCREMENTAL_MONTHS]
    targets = [p for p in programmes.values() if not only or p["channel"] in only]
    targets.sort(key=lambda p: (p["channel"], p["slug"]))
    if limit:
        targets = targets[:limit]
    run["enumerated"] = 0
    run["months_fetched"] = 0
    run["month_failures"] = 0
    backlog = []
    for p in targets:
        ch, slug = p["channel"], p["slug"]
        p_ok = True
        for m in incremental:
            ok, eps = fetch_month(client, ch, slug, m)
            run["months_fetched"] += 1
            if ok:
                write_json(month_path(ch, slug, m), {"fetchedAt": today.isoformat(), "episodes": eps})
            else:
                run["month_failures"] += 1
                p_ok = False
        p["lastEnumerated"] = today.isoformat()
        if not p_ok:
            p["lastEnumerateFailed"] = today.isoformat()
        run["enumerated"] += 1
        for m in window[INCREMENTAL_MONTHS:]:
            if not month_path(ch, slug, m).exists():
                backlog.append((ch, slug, m))
    run["backlog_months"] = len(backlog)
    # Tidy: month files older than the retention window are dead weight.
    keep = set(months_back(RETENTION_MONTHS + 1, today))
    pruned = 0
    for f in RTHK_DATA.glob("*/*/2*.json"):
        if f.stem not in keep:
            f.unlink()
            pruned += 1
    run["months_pruned"] = pruned
    if not backfill:
        return
    # Newest months first so the catalog fills front-to-back.
    backlog.sort(key=lambda t: t[2], reverse=True)
    done = 0
    for ch, slug, m in backlog:
        ok, eps = fetch_month(client, ch, slug, m)
        run["months_fetched"] += 1
        if ok:
            write_json(month_path(ch, slug, m), {"fetchedAt": today.isoformat(), "episodes": eps})
            done += 1
        else:
            run["month_failures"] += 1
    run["backfilled_months"] = done
    run["backlog_months"] = len(backlog) - done


# ---------------------------------------------------------------------------
# 3. Stream spot-check (§2.3)
# ---------------------------------------------------------------------------
def spot_check(client, programmes, overrides, only, run):
    today = today_hkt()
    run["spot_checks"] = 0
    run["spot_misses"] = 0
    for ch in RTHK_CHANNELS:
        if only and ch not in only:
            continue
        cands = []
        for p in programmes.values():
            if p["channel"] != ch or p.get("lastEnumerateFailed") == today.isoformat():
                continue
            for m in months_back(INCREMENTAL_MONTHS, today):
                cache = read_json(month_path(ch, p["slug"], m))
                past = [e for e in (cache or {}).get("episodes", []) if e["date"] < today.isoformat()]
                if past:
                    cands.append((p, past[0]))
                    break
        random.shuffle(cands)
        for p, ep in cands[:SPOT_CHECK_PER_CHANNEL]:
            url = STREAM_TEMPLATE.format(channel=ch, slug=p["slug"], date=ep["date"].replace("-", ""))
            st, _, body = client.get(url, tries=2)
            run["spot_checks"] += 1
            key = f"{ch}/{p['slug']}/{ep['id']}"
            if st == 200 and body.startswith(b"#EXTM3U"):
                p.pop("streamDrift", None)
                overrides.pop(key, None)
                continue
            run["spot_misses"] += 1
            st2, _, frag = client.get(f"{WWW}/radio/getEpisode?c={ch}&p={p['slug']}&e={ep['id']}",
                                      ajax_headers(ch, p["slug"]), tries=2)
            m = re.search(r'file:\s*"([^"?]+)', frag.decode("utf-8", "replace")) if st2 == 200 else None
            if m and m.group(1) != url:
                overrides[key] = m.group(1)
                p["streamDrift"] = today.isoformat()
                log(f"  stream drift {key}: derived {st} → override {m.group(1)}")
            else:
                log(f"  stream check miss {key}: derived {st}, getEpisode {st2} (no override available — audio may be aged out)")


# ---------------------------------------------------------------------------
# 4. Metro retention window (§3.3)
# ---------------------------------------------------------------------------
def metro_url(freq, d, hh="08", mm="00"):
    """The archive file for the slot starting at hh:mm HKT on day d
    ({datetime} = YYYYMMDDHHMM = the slot START; S31)."""
    ymd = d.strftime("%Y%m%d")
    return METRO_TEMPLATE.format(freq=freq, date=ymd, datetime=ymd + hh + mm)


def metro_ok(client, freq, d, hh="08", mm="00"):
    st, _, _ = client.get(metro_url(freq, d, hh, mm), method="HEAD", tries=2)
    return st == 200


def metro_grid_canary(client, freq, day, run):
    """S31 half-hour grid canary — exactly ONE HEAD (plus its one retry on a
    non-404 miss) per frequency per run, on the day's 08:30 slot. The file's
    Content-Length at the 64 kbps CBR rate must come to ~METRO_SEGMENT_MINUTES;
    a missing :30 file or a length off by more than 5 % means Metro changed its
    grid. Warn-only: it never blocks the window or the catalog."""
    run["metro_segment_sec"][freq] = None
    if not metro_ok(client, freq, day, "08", "30"):
        run["warnings"].append(f"metro {freq}: {day} 08:30 slot missing — grid drift?")
        return
    expected = METRO_SEGMENT_MINUTES * 60
    if not client.last_length:
        run["warnings"].append(f"metro {freq}: {day} 08:30 slot has no Content-Length — grid canary blind")
        return
    secs = client.last_length * 8 / METRO_BITRATE_BPS
    run["metro_segment_sec"][freq] = round(secs, 1)
    if abs(secs - expected) > METRO_GRID_TOLERANCE * expected:
        run["warnings"].append(f"metro {freq}: {day} 08:30 slot is ~{secs:.0f}s "
                               f"({client.last_length} B at {METRO_BITRATE_BPS // 1000} kbps), "
                               f"expected {expected}s — grid drift?")


def metro_window(client, window, run):
    today = today_hkt()
    run["metro"] = {}
    run["metro_segment_sec"] = {}
    for freq, meta in METRO_CHANNELS.items():
        prev = window.get(freq, {})
        # latest: yesterday is complete; today counts once its 00:00 slot exists
        latest = None
        for cand in (today, today - dt.timedelta(days=1), today - dt.timedelta(days=2)):
            if metro_ok(client, freq, cand, "00"):
                latest = cand
                break
        if latest is None:
            run["warnings"].append(f"metro {freq}: no recent day answered 200 — window kept from last run")
            run["metro"][freq] = "unreachable"
            continue
        # earliest: binary search over days-back; seed from the previous window
        # so the daily cost is a handful of HEADs, not a full search.
        lo = 0                                  # known good (days back from latest)
        hi = 400                                # known bad
        if prev.get("earliestDate"):
            prev_back = (latest - dt.date.fromisoformat(prev["earliestDate"])).days
            if prev_back > 0:
                if metro_ok(client, freq, latest - dt.timedelta(days=prev_back)):
                    lo = prev_back
                    hi = prev_back + 40 if not metro_ok(client, freq, latest - dt.timedelta(days=prev_back + 40)) else 400
                else:
                    hi = prev_back
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if metro_ok(client, freq, latest - dt.timedelta(days=mid)):
                lo = mid
            else:
                hi = mid
        earliest = latest - dt.timedelta(days=lo)
        window[freq] = {"earliestDate": earliest.isoformat(), "latestDate": latest.isoformat(),
                        "checkedAt": today.isoformat()}
        run["metro"][freq] = f"{earliest}..{latest}"
        # Grid canary on a COMPLETE day (the 03:30 HKT run finds today's 00:00
        # slot, but today's 08:30 has not aired). Runs after the window is
        # recorded so a budget stop here never costs the window update.
        metro_grid_canary(client, freq, min(latest, today - dt.timedelta(days=1)), run)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=2500, help="max HTTP requests this run")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--only", help="comma-separated RTHK channels to touch (dev)")
    ap.add_argument("--limit", type=int, default=0, help="max programmes to enumerate (dev)")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--skip-metro", action="store_true")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None

    client = Client(pace_s=args.pace, budget=args.budget, log=log)
    programmes = read_json(PROGRAMMES_PATH, {})
    overrides = read_json(OVERRIDES_PATH, {})
    window = read_json(METRO_WINDOW_PATH, {})
    run = {"startedAt": dt.datetime.now(HKT).isoformat(timespec="seconds"),
           "budget": args.budget, "warnings": [], "aborted": None}
    t0 = time.monotonic()
    try:
        log("== discovery")
        discover(client, programmes, only, run, args.limit)
        log(f"   {run['programmes_known']} programmes known ({run['sitemap_programmes']} from sitemap), meta refreshed {run.get('meta_refreshed', 0)}")
        log("== logo upgrade")
        upgrade_small_logos(client, programmes, only, run,
                            min(LOGO_PROGRAMMES_PER_RUN, args.limit) if args.limit else LOGO_PROGRAMMES_PER_RUN)
        log(f"   {run['logo_small']} small logos, {run['logo_checked']} checked ({run['logo_probes']} HEADs), "
            f"{run['logo_upgraded']} publish larger, {run['logo_deferred']} deferred")
        log("== enumeration")
        enumerate_programmes(client, programmes, only, args.limit, not args.no_backfill, run)
        log(f"   {run['enumerated']} programmes, {run['months_fetched']} month fetches, {run['month_failures']} failed, backlog {run['backlog_months']} months")
        log("== stream spot-check")
        spot_check(client, programmes, overrides, only, run)
        log(f"   {run['spot_checks']} checks, {run['spot_misses']} misses")
        if not args.skip_metro:
            log("== metro window")
            metro_window(client, window, run)
            log(f"   {run['metro']}; 08:30 slot seconds {run['metro_segment_sec']}")
    except (BudgetExhausted, CircuitOpen) as e:
        run["aborted"] = f"{type(e).__name__}: {e}"
        log(f"!! {run['aborted']}")
    finally:
        run["requests"] = client.requests
        run["failures"] = client.failures
        run["seconds"] = round(time.monotonic() - t0)
        run["finishedAt"] = dt.datetime.now(HKT).isoformat(timespec="seconds")
        write_json(PROGRAMMES_PATH, programmes)
        write_json(OVERRIDES_PATH, overrides)
        write_json(METRO_WINDOW_PATH, window)
        write_json(LAST_RUN_PATH, run)
        log(f"== done: {client.requests} requests, {client.failures} failures, {run['seconds']}s")
    # A budget stop is a normal partial day (the cache keeps what landed);
    # a circuit-open is a real failure the gate must see.
    return 1 if run["aborted"] and run["aborted"].startswith("CircuitOpen") else 0


if __name__ == "__main__":
    sys.exit(main())
