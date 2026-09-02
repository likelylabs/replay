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
  2. RTHK enumeration — for every programme: current + previous month always
     (catchUp?m=&page= walked to nextPage=-1); older months inside the
     12-month window only when missing from the cache (backfill drains over
     runs until the budget is spent). Months are cached immutably at
     data/rthk/{ch}/{slug}/{YYYYMM}.json.
  3. Stream spot-check — a sample of recent episodes per channel against the
     derived Akamai template; on a miss, getEpisode's authoritative `file:`
     is stored as a per-episode override (data/rthk/overrides.json).
  4. Metro window — binary-search the earliest hourly MP3 still served per
     frequency; confirm the latest day. (data/metro/window.json)
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
import xml.etree.ElementTree as ET

from common import (BudgetExhausted, CircuitOpen, Client, INCREMENTAL_MONTHS,
                    METRO_CHANNELS, METRO_DATA, METRO_TEMPLATE, METRO_WINDOW_PATH,
                    OVERRIDES_PATH, PROGRAMMES_PATH, RETENTION_MONTHS, RTHK_CHANNELS,
                    RTHK_DATA, STREAM_TEMPLATE, WWW, HKT, LAST_RUN_PATH, iso_from_ddmmyyyy,
                    log, month_key, months_back, read_json, today_hkt, write_json)

META_REFRESH_DAYS = 30      # re-read a programme page this often
META_REFRESH_CAP = 25       # ...but at most this many refreshes per run (new slugs exempt)
SPOT_CHECK_PER_CHANNEL = 2
SLUG_RE = re.compile(r"^[A-Za-z0-9_]+$")


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
def metro_ok(client, freq, d, hh="08"):
    url = METRO_TEMPLATE.format(freq=freq, date=d.strftime("%Y%m%d"),
                                datetime=d.strftime("%Y%m%d") + hh + "00")
    st, _, _ = client.get(url, method="HEAD", tries=2)
    return st == 200


def metro_window(client, window, run):
    today = today_hkt()
    run["metro"] = {}
    for freq, meta in METRO_CHANNELS.items():
        prev = window.get(freq, {})
        # latest: yesterday is complete; today counts once its first hour exists
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
        log("== enumeration")
        enumerate_programmes(client, programmes, only, args.limit, not args.no_backfill, run)
        log(f"   {run['enumerated']} programmes, {run['months_fetched']} month fetches, {run['month_failures']} failed, backlog {run['backlog_months']} months")
        log("== stream spot-check")
        spot_check(client, programmes, overrides, only, run)
        log(f"   {run['spot_checks']} checks, {run['spot_misses']} misses")
        if not args.skip_metro:
            log("== metro window")
            metro_window(client, window, run)
            log(f"   {run['metro']}")
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
