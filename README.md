# likelylabs/replay

Server-side catalog for the radio app's Replay (重溫) section — a daily crawl of
public broadcaster catch-up listings, published as static JSON. The app never
talks to a broadcaster for metadata; it reads this catalog and streams audio
from the broadcaster's own public CDN.

Spec and operating tenets live in the private coordination repo
(`~/localdev/radioapp-hq`: `REPLAY.md`, `CLAUDE.md`). No secrets here, ever.
