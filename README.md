# news-feed

A single static page listing recent stories from a set of news sites, built
from their RSS feeds. Links and short summaries only — no article text is
copied. Rebuilt automatically every 30 minutes by GitHub Actions and published
to GitHub Pages.

## Changing the sources

Edit `feeds.txt`. One line per feed:

```
Display name | Feed URL | days to keep
```

The third column is optional and defaults to 14 days. Lines starting with `#`
are ignored, so you can park a source without deleting it. Commit and push; the
page rebuilds on its own.

A malformed line is skipped rather than failing the build, but it's reported in
the page footer as well as the log, so a typo can't make a source quietly
disappear.

To find a site's feed URL, try `https://thesite.com/feed/` first — that covers
most WordPress sites. Otherwise view the homepage source and look for
`<link rel="alternate" type="application/rss+xml">`.

## Running it locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python build.py
open out/index.html
```

## How it works

`build.py` fetches every feed in parallel, merges the entries into one
reverse-chronological list, drops anything older than `MAX_AGE_DAYS`,
de-duplicates by link, and writes `out/index.html`. That file is
self-contained — no external CSS, fonts, or scripts — so it works offline and
loads instantly.

## Read state

Opening a story marks it read; the ✓ button on each row toggles it without
opening. Read stories fade but stay in place — the "unread only" button hides
them, and "mark all read" clears whatever is currently visible, so it respects
the source filter.

This lives in the browser's `localStorage`, keyed by canonical link. There is no
account and no server, so **read state does not follow you between devices or
browsers** — your laptop and phone keep separate tallies. Making it shared would
mean a backend, which this deliberately doesn't have.

Entries expire after 180 days rather than being pruned to whatever is on the
page. That distinction matters: feeds fail transiently (Venezuelanalysis
returned an HTTP 520 during development), and a source's stories vanish from the
build when they do. Pruning by page membership would discard their read state
during the outage and resurrect every story as unread once the feed recovered.

## How long stories stay on the page

Each build starts from scratch: there is no archive. A story is on the page only
while it is *both* still in the publisher's own feed and newer than that feed's
third column in `feeds.txt`. Whichever runs out first wins, and which one that
is varies enormously by publisher — measured across these ten feeds:

| Source | Items in feed | Feed reaches back |
|---|---|---|
| CounterPunch | 15 | 1.6 days |
| Breakthrough News | 8 | 3 days |
| Venezuelanalysis | 16 | 18 days |
| Electronic Intifada | 20 | 24 days |
| Cosmonaut | 20 | 30 days |
| Monthly Review | 10 | 38 days |
| MintPress News | 10 | 58 days |
| Media Lens | 10 | 164 days |

So the per-feed number can only ever *shorten* a source's window. Raising
CounterPunch's to 60 changes nothing, because their feed rolls over in under two
days. Raising Media Lens's genuinely shows more, because their feed holds five
months of posts. Extending a fast source's window would mean storing entries
between builds, which this deliberately doesn't do.

A feed that fails does not fail the build; it's listed in the page footer
instead. The build only exits non-zero if *every* feed fails, which prevents a
network blip from replacing a good page with an empty one.

## Notes on specific sources

- **CEPR** — their `/feed/` is abandoned: the newest entry is the WordPress
  default "Hello world!" post from July 2024, and it's the only feed they
  advertise. It stays in `feeds.txt` so the footer keeps reporting it; if they
  ever fix it, it starts working with no changes here.
- **Weekly Worker** — its feed is at the non-standard `/worker/rss`, and it
  publishes no per-entry dates, only a channel timestamp regenerated on each
  request. Those entries are timestamped with the date this build first saw the
  link (shown as "first seen …") and `state.json` remembers that between runs,
  cached by the workflow. Without it, Weekly Worker would pin itself to the top
  of the page on every build.
- **NACLA** — publishes no feed. Every feed path (`/feed`, `/feed/`,
  `/?feed=rss2`, `/category/news-analysis/feed`) redirects to the homepage. It's
  commented out in `feeds.txt` in case they restore it.
- Several of these sites sit behind bot filters that challenge generic scraper
  user agents but allow real feed readers through, which is why `build.py`
  identifies itself honestly as an RSS aggregator.
