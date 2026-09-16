#!/usr/bin/env python3
"""
Runner probe / daily canary for the RTHK + Metro catch-up sources.

REPLAY.md §2.4 validation step 1: prove, from an actual GitHub Actions runner,
that every endpoint the crawler depends on answers a clean 200 with the
expected body shape. Kept as a scheduled canary so a block on runner IPs is
noticed the day it happens, not when the catalog silently stops moving.

Stdlib only. Serial, ~1 req/s, realistic UA + Referer (politeness, §2.4).
Exit non-zero on any failed check so the workflow goes red.
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request

WWW = "https://www.rthk.hk"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
CDN = "https://rthkaod2022.akamaized.net/m4a/radio/archive/{c}/{p}/m4a/{d}.m4a/master.m3u8"
METRO = "https://arch.metroradio.hk/{f}/{d}/{f}_{d}{hh}00.mp3"
PACE_S = 1.0

CH, SLUG = "radio1", "Free_as_the_wind"
CHANNELS = ["radio1", "radio2", "radio3", "radio4", "radio5", "pth"]

results = []


def fetch(url, headers=None, method="GET", timeout=40):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = b"" if method == "HEAD" else r.read()
            return r.status, r.headers.get("Content-Type", ""), body, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), b"", time.monotonic() - t0
    except Exception as e:  # DNS, TLS, timeout
        return -1, repr(e), b"", time.monotonic() - t0
    finally:
        time.sleep(PACE_S)


def check(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def month_str(offset):
    today = dt.date.today()
    y, m = divmod((today.year * 12 + today.month - 1) - offset, 12)
    return f"{y}{m + 1:02d}"


def main():
    # 0. egress IP — tells us which ranges RTHK would see (Azure for hosted runners)
    st, _, body, _ = fetch("https://api.ipify.org")
    print(f"runner egress ip: {body.decode(errors='replace') if st == 200 else '?'}")

    # 1. sitemap discovery
    st, ct, body, dtime = fetch(f"{WWW}/sitemap.xml")
    slugs = {c: set(re.findall(rf"/radio/{c}/programme/([A-Za-z0-9_]+)", body.decode(errors="replace"))) for c in CHANNELS}
    total = len(set().union(*slugs.values()))
    check("sitemap.xml", st == 200 and total > 200,
          f"{st} {ct} {len(body)}B {dtime:.1f}s programmes={total} " + " ".join(f"{c}={len(s)}" for c, s in slugs.items()))

    # 2. channel page (active programmes)
    st, ct, body, dtime = fetch(f"{WWW}/radio/{CH}")
    active = set(re.findall(r"data-f='([A-Za-z0-9_]+)'", body.decode(errors="replace")))
    check(f"channel page /radio/{CH}", st == 200 and len(active) > 5,
          f"{st} {len(body)}B {dtime:.1f}s active={len(active)}")

    # 3. catchUpByMonth — current + previous month (the daily incremental window)
    dates = []  # newest first: the listing is newest first, current month before previous
    hdr = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{WWW}/radio/{CH}/programme/{SLUG}"}
    for off in (0, 1):
        m = month_str(off)
        st, ct, body, dtime = fetch(f"{WWW}/radio/catchUpByMonth?c={CH}&p={SLUG}&m={m}", hdr)
        try:
            d = json.loads(body.decode("utf-8"))
            n = len(d.get("content", []))
            ok = st == 200 and d.get("status") == "1" and (n > 0 or off == 0)
            dates += [e["date"] for e in d.get("content", [])]
            check(f"catchUpByMonth {CH}/{SLUG} m={m}", ok,
                  f"{st} {ct} {dtime:.1f}s status={d.get('status')} episodes={n} nextPage={d.get('nextPage')}")
        except Exception as e:
            check(f"catchUpByMonth {CH}/{SLUG} m={m}", False, f"{st} {ct} unparseable: {e!r} head={body[:80]!r}")

    # 4. Akamai HLS master — newest episode, else the next-newest. RTHK lists an
    # episode before its archive is uploaded, so one late upload must not go red;
    # a runner block or path change still fails both.
    ok, notes = False, []
    for date in dates[:2]:
        d8 = dt.datetime.strptime(date, "%d/%m/%Y").strftime("%Y%m%d")
        st, ct, body, dtime = fetch(CDN.format(c=CH, p=SLUG, d=d8))
        ok = st == 200 and body.startswith(b"#EXTM3U")
        notes.append(f"{st} {ct} {dtime:.1f}s date={d8} head={body[:40]!r}")
        if ok:
            break
    check("akamai master.m3u8", ok, " → ".join(notes) or "no episode date to derive from")

    # 5. Metro hourly MP3 — yesterday 08:00 HKT, all three frequencies
    yday = (dt.datetime.utcnow() + dt.timedelta(hours=8) - dt.timedelta(days=1)).strftime("%Y%m%d")
    for f in ("104", "997", "1044"):
        st, ct, _, dtime = fetch(METRO.format(f=f, d=yday, hh="08"), method="HEAD")
        check(f"metro {f} {yday} 08:00", st == 200, f"{st} {ct} {dtime:.1f}s")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
