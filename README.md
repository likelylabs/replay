# likelylabs/replay

Server-side catalog for the radio app's **Replay (重溫)** section: a daily,
polite crawl of public broadcaster catch-up listings, published as static JSON
on GitHub Pages. The app never contacts a broadcaster for metadata — it reads
this catalog and streams audio straight from the broadcaster's own public CDN.

Spec, tenets and decisions live in the private coordination repo
(`~/localdev/radioapp-hq`: `REPLAY.md`, `CLAUDE.md`). **No secrets here, ever.**

## Pipeline

```
 broadcaster sites                this repo (GitHub Actions)              the app
 ─────────────────                ─────────────────────────────           ───────
 RTHK sitemap + channel pages ─┐  crawl.yml (daily 03:30 HKT)             GET index.json
 RTHK catchUp JSON (per month) ─┼─► tools/crawl.py   → data/  (cache)      → channel / programme browse
 RTHK Akamai HLS (spot-check)  ─┤  tools/build_catalog.py → index.json     GET prog/{ch}/{slug}.json
 Metro half-hour MP3 (window)  ─┘     (publish gate)      + prog/           → episode list → derived stream URL
                                   pages-deploy.yml → Pages (last-good)
```

- `tools/probe.py` / `probe.yml` — daily canary that every source still
  answers 200 from a hosted runner (red = runner-IP block or path change).
- `tools/crawl.py` — the only code that talks to broadcasters. Serial,
  ~1 req/s, realistic UA + Referer, backoff, a per-run request budget and a
  circuit breaker. Writes only `data/`. Metro costs a handful of HEADs per
  frequency per day: the window search plus ONE `:30`-slot grid canary
  (Content-Length ÷ 64 kbps ≈ 30 min, else a `grid drift?` warning in
  `data/last-run.json`; warn-only, `metro_segment_sec` records the reading).
  Logo upgrade: a publishing programme whose logo is one of RTHK's small
  variants (`{id}_115.jpg` 115 px, `…_s.jpg` 272 px) gets a HEAD on each larger
  artwork in the same directory, in order, until one is a usable image: the
  720 px square `programme_photo_l.jpg`, the square original
  `programme_photo.jpg` only where no 720 px answers (≤ 500 KB — originals run
  to 19 MB and 3001 px), and for `_s` the sibling with `_s` dropped.
  The answer (or "none larger") is memoised in `data/rthk/programmes.json`
  `logoFull` and re-checked at most every 30 days — a few HEADs on most days,
  ≤ 60 programmes (≤ 4 HEADs each) in any run, stops after 3 failed HEADs.
  RTHK's storage answers a denied request exactly like a missing object, so
  "none larger" needs evidence: one control HEAD on the small logo itself
  (not a 200 image = no answer, counted as a failure), a published upgrade is
  withdrawn only when a later run agrees, and 5 programmes in a row with
  nothing larger stop the step without memoising any of them.
- `tools/build_catalog.py` — the only writer of `index.json` + `prog/`.
  Validates, drops dormant programmes, blanks uninformative episode titles
  (below), and refuses to publish a catalog that shrank past the safety floor
  (≥90% programmes / ≥80% episodes of last-good).
- `pages-deploy.yml` — publishes the public surface (`index.json`, `prog/`,
  `robots.txt`) with 3 fresh deploy attempts + a 30-min drift reconciler.
  Gated on the `PAGES_ENABLED` repository variable; `PUBLIC_BASE_URL` tells
  the reconciler where the live catalog is.

## Catalog contract (app-facing)

- `index.json` — every channel with its programme list (RTHK, `browse:
  "programme"`) or its by-time window (Metro, `browse: "bytime"`). Schema:
  `schema/index.schema.json`. A programme's optional `logo` is an image URL
  on RTHK's own host — a larger square programme artwork ≤ 500 KB wherever the
  crawl has verified one (the 720 × 720 rendition; where RTHK has none, the
  original photo, which can be 1134–3001 px; a 16:9 `_1920_s` thumbnail may
  fall back to its 1920 px banner — decode it to the tile's size, never at
  full size), else what the programme page or schedule offered.
- `prog/{channel}/{slug}.json` — one programme's episodes, newest first.
  Schema: `schema/programme.schema.json`. An episode `title` of `""` means RTHK
  gave no episode title — only the programme name (after an NFKC / case /
  punctuation fold, or contained in it) or a fixed label carried by ≥80% of a
  programme's ≥4 episodes (節目內容, 歌曲選播, a host's show name). Show the
  programme title instead. Real titles are published verbatim.
- Stream URLs are **derived client-side**: `index.streamTemplate` filled with
  `channel`, `slug`, `date` (YYYYMMDD) — unless an episode carries `streamUrl`.
  Metro: the archive is a **half-hour grid** — one ~30-min MP3 per slot, slots
  every `segmentMinutes` (30) from 00:00 HKT, 48 a day. Fill the channel's
  `streamTemplate` with `freq`, `date`, `datetime` = the slot START
  (YYYYMMDDHHMM, MM ∈ {00, 30}); a 404 is a gap. `segmentMinutes` drives the
  slot grid, URL and label only — it is not a playback duration.
- `streamVersion` is a forward-compat guard: the app ignores catalogs whose
  version exceeds what it understands.

## Local dev (stdlib only)

```bash
python3 tools/probe.py                                    # sources alive?
python3 tools/crawl.py --only radio1 --limit 5 --budget 100 --no-backfill --skip-metro
python3 tools/build_catalog.py                            # gate + write index.json / prog/
python3 -m unittest discover -s tests                     # offline tests (no network)
```

`index.json` + `prog/` are committed by the daily crawl Action — don't commit a
local rebuild. The tests build the committed `data/` into a temp dir instead;
the JSON-schema checks run when `jsonschema` is installed and skip otherwise.

## Go-live checklist (owner)

1. Flip the repo **public** (Pages is public-repo-only on this org's plan).
2. Settings → Pages → Source = **GitHub Actions**.
3. Repo variables: `PAGES_ENABLED=true`; `PUBLIC_BASE_URL=https://likelylabs.github.io/replay`
   (switch to `https://replay.likelylabs.com` once the CNAME is live).
4. Run `pages-deploy` → verify `index.json` on the github.io URL.
5. Cloudflare DNS: `replay CNAME likelylabs.github.io` (DNS-only / grey cloud).
6. Commit `CNAME` (`replay.likelylabs.com`), enforce HTTPS in Pages settings,
   update `PUBLIC_BASE_URL`.
