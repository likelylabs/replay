"""
Shared plumbing for the replay crawler: paths, source constants, and one
polite HTTP client (serial, paced, backoff, request budget, circuit breaker).

Stdlib only. Politeness rules per REPLAY.md §2.4: ~1 req/s, realistic UA +
Referer, exponential backoff on non-200, one machine, never the app.
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
RTHK_DATA = DATA / "rthk"
METRO_DATA = DATA / "metro"
PROGRAMMES_PATH = RTHK_DATA / "programmes.json"
OVERRIDES_PATH = RTHK_DATA / "overrides.json"
METRO_WINDOW_PATH = METRO_DATA / "window.json"
LAST_RUN_PATH = DATA / "last-run.json"
INDEX_PATH = REPO / "index.json"
PROG_DIR = REPO / "prog"

CRAWLER_VERSION = 1
STREAM_VERSION = 1

HKT = dt.timezone(dt.timedelta(hours=8))

# --- RTHK -------------------------------------------------------------------
WWW = "https://www.rthk.hk"
RTHK_CHANNELS = {
    "radio1": {"name_zh": "第一台", "name_en": "Radio 1"},
    "radio2": {"name_zh": "第二台", "name_en": "Radio 2"},
    "radio3": {"name_zh": "第三台", "name_en": "Radio 3"},
    "radio4": {"name_zh": "第四台", "name_en": "Radio 4"},
    "radio5": {"name_zh": "第五台", "name_en": "Radio 5"},
    "pth":    {"name_zh": "普通話台", "name_en": "Putonghua Channel"},
}
# Derived stream template (REPLAY §2.3). The catalog stores the template, not
# per-episode URLs; the app fills {channel},{slug},{date}.
STREAM_TEMPLATE = ("https://rthkaod2022.akamaized.net/m4a/radio/archive/"
                   "{channel}/{slug}/m4a/{date}.m4a/master.m3u8")
RETENTION_MONTHS = 12      # RTHK keeps ~12 months of catch-up
INCREMENTAL_MONTHS = 2     # daily: re-crawl current + previous month only

# --- Metro ------------------------------------------------------------------
METRO_CHANNELS = {
    "104":  {"id": "metro_mf",   "name_zh": "新城財經台", "name_en": "Metro Finance"},
    "997":  {"id": "metro_info", "name_zh": "新城知訊台", "name_en": "Metro Info"},
    "1044": {"id": "metro_plus", "name_zh": "新城采訊台", "name_en": "Metro Plus"},
}
METRO_TEMPLATE = "https://arch.metroradio.hk/{freq}/{date}/{freq}_{datetime}.mp3"
# Metro's archive is a HALF-HOUR grid (DECISIONS S31, probed 2026-09-24): one
# file per 30-minute slot starting at HH:00 and HH:30 — 48 a day — so
# {datetime} = YYYYMMDDHHMM with MM ∈ {00, 30} (the slot START). Each file is
# one ~30-min MPEG-2 Layer III, 64 kbps CBR, 22.05 kHz MP3 ≈ 14.33 MB
# (~1,792 s). The old "hourly, ~14 MB/hour" reading was wrong: 14 MB at
# 64 kbps IS 30 minutes. Published as index.json `segmentMinutes`.
METRO_SEGMENT_MINUTES = 30
# Canary only (crawl.py grid check: Content-Length × 8 ÷ this ≈ the slot's
# seconds). Never published — the app never derives a duration from it.
METRO_BITRATE_BPS = 64_000

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")


class BudgetExhausted(Exception):
    pass


class CircuitOpen(Exception):
    pass


class Client:
    """Serial, paced HTTP client with a request budget and a failure breaker."""

    def __init__(self, pace_s=1.0, budget=2500, breaker_ratio=0.25, breaker_min=40, log=print):
        self.pace_s = pace_s
        self.budget = budget
        self.requests = 0
        self.failures = 0
        self.breaker_ratio = breaker_ratio
        self.breaker_min = breaker_min
        self.log = log
        self.last_length = 0      # Content-Length of the last response (0 = absent)
        self.last_url = ""        # final URL of the last 2xx response, after redirects ("" = none)
        self._last = 0.0

    def _pace(self):
        wait = self.pace_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url, headers=None, method="GET", timeout=40, tries=3, ok=(200,)):
        """Return (status, content_type, body). Retries with backoff on any
        non-ok status or transport error; raises BudgetExhausted / CircuitOpen.
        A 404 is returned immediately (it is an answer, not a failure).
        Sets self.last_length to the final response's Content-Length (0 when
        absent or unparseable) — a HEAD's size without downloading the body —
        and self.last_url to the URL that answered (differs from `url` only
        when urllib followed a redirect; "" when no 2xx arrived)."""
        self.last_length = 0
        self.last_url = ""
        if self.requests >= self.budget:
            raise BudgetExhausted(f"request budget {self.budget} exhausted")
        req_headers = {"User-Agent": UA, "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"}
        req_headers.update(headers or {})
        last = (-1, "", b"")
        for attempt in range(tries):
            self._pace()
            self.requests += 1
            req = urllib.request.Request(url, method=method, headers=req_headers)
            self.last_length = 0
            self.last_url = ""
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = b"" if method == "HEAD" else r.read()
                    last = (r.status, r.headers.get("Content-Type", ""), body)
                    self.last_length = _content_length(r.headers)
                    self.last_url = getattr(r, "url", None) or url
            except urllib.error.HTTPError as e:
                last = (e.code, e.headers.get("Content-Type", "") if e.headers else "", b"")
                if e.code == 404:
                    return last
            except Exception as e:  # DNS, TLS, timeout, reset
                last = (-1, repr(e), b"")
            if last[0] in ok:
                return last
            self.failures += 1
            self._check_breaker()
            if attempt < tries - 1:
                back = 2 ** (attempt + 1)
                self.log(f"  retry {attempt + 1}/{tries - 1} in {back}s: {last[0]} {url}")
                time.sleep(back)
        return last

    def _check_breaker(self):
        if self.requests >= self.breaker_min and self.failures / self.requests > self.breaker_ratio:
            raise CircuitOpen(f"{self.failures}/{self.requests} requests failed — stopping so a block is not hammered")

    def get_json(self, url, headers=None):
        st, ct, body = self.get(url, headers)
        if st != 200:
            return st, None
        try:
            return st, json.loads(body.decode("utf-8"))   # JSON despite text/html
        except (UnicodeDecodeError, json.JSONDecodeError):
            return st, None


def _content_length(headers):
    try:
        return max(0, int(str(headers.get("Content-Length") or 0).strip()))
    except (AttributeError, TypeError, ValueError):
        return 0


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def today_hkt():
    return dt.datetime.now(HKT).date()


def month_key(d):
    return f"{d.year}{d.month:02d}"


def months_back(n, start=None):
    """[YYYYMM for this month, last month, ... n months]"""
    start = start or today_hkt()
    out = []
    for k in range(n):
        y, m = divmod((start.year * 12 + start.month - 1) - k, 12)
        out.append(f"{y}{m + 1:02d}")
    return out


def iso_from_ddmmyyyy(s):
    return dt.datetime.strptime(s.strip(), "%d/%m/%Y").date().isoformat()


def log(msg):
    print(msg, flush=True)
