# news-feed

A single static page listing recent stories from a set of news sites, built
from their RSS feeds. Links and short summaries only — no article text is
copied. Rebuilt automatically every 30 minutes by GitHub Actions and published
to GitHub Pages.

## Changing the sources

Edit `feeds.txt`. One line per feed:

```
Section | Display name | Feed URL
```

Sections become the filter buttons at the top of the page. Lines starting with
`#` are ignored, so you can park a source without deleting it. Commit and push;
the page rebuilds on its own.

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
reverse-chronological list, drops anything older than three weeks, de-duplicates
by link, and writes `out/index.html`. That file is self-contained — no external
CSS, fonts, or scripts — so it works offline and loads instantly.

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
