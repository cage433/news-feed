#!/usr/bin/env python3
"""Build a static news page from the RSS/Atom feeds listed in feeds.txt.

Fetches every feed, merges the entries into one reverse-chronological list,
and writes out/index.html. No database, no server: the output is a single
self-contained HTML file.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
OUT_FILE = ROOT / "out" / "index.html"
STATE_FILE = ROOT / "state.json"

MAX_AGE_DAYS = 21       # drop anything older than this
MAX_PER_FEED = 25       # keep a prolific source from swamping the page
MAX_TOTAL = 400         # overall cap on the page
SUMMARY_CHARS = 220
WORKERS = 8
RETRIES = 3             # transient 5xx / network blips
RETRY_BACKOFF = 2.0     # seconds, multiplied by attempt number

# Identify honestly. Several of these sites sit behind bot filters that
# allow-list real feed readers but challenge generic scraper user agents.
USER_AGENT = "news-feed-reader/1.0 (personal RSS aggregator; +https://github.com/cage433/news-feed)"


@dataclass
class Item:
    title: str
    link: str
    published: datetime
    summary: str
    source: str
    section: str
    dated: bool = True  # False when the feed gave no date and we stamped first-seen


@dataclass
class Feed:
    section: str
    name: str
    url: str


def read_feeds() -> list[Feed]:
    feeds = []
    for lineno, raw in enumerate(FEEDS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            print(f"feeds.txt:{lineno}: expected 'Section | Name | URL', skipping", file=sys.stderr)
            continue
        feeds.append(Feed(*parts))
    return feeds


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(raw: str, limit: int | None = None) -> str:
    """Strip HTML tags and entities down to a plain one-line string."""
    text = WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", raw or ""))).strip()
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut.rstrip(".,;:—-") + "…"
    return text


def canonical(url: str) -> str:
    """Normalise a link for dedupe: drop tracking params and the fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = "&".join(
        p for p in parts.query.split("&")
        if p and not p.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid"))
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), query, ""))


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
    return None


def fetch(feed: Feed) -> tuple[Feed, list[Item], str | None]:
    """Return (feed, items, error). A broken feed must not fail the build."""
    parsed = None
    problem = "unknown error"

    # Cloudflare-fronted sites throw occasional 5xx blips. Without a retry a
    # single bad moment silently drops a whole source off the page.
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(RETRY_BACKOFF * attempt)
        try:
            parsed = feedparser.parse(feed.url, agent=USER_AGENT, request_headers={
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            })
        except Exception as exc:  # network, DNS, malformed XML that raises
            problem = f"{type(exc).__name__}: {exc}"
            continue

        status = getattr(parsed, "status", None)
        if status and status >= 500:
            problem = f"HTTP {status}"      # transient — worth another go
            continue
        if status and status >= 400:
            return feed, [], f"HTTP {status}"   # 404/403 won't fix itself
        if not parsed.entries:
            reason = getattr(parsed, "bozo_exception", None)
            return feed, [], f"no entries ({reason})" if reason else "no entries"
        break
    else:
        return feed, [], problem

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    items: list[Item] = []
    newest: datetime | None = None

    for entry in parsed.entries[:MAX_PER_FEED]:
        link = (getattr(entry, "link", "") or "").strip()
        title = clean_text(getattr(entry, "title", ""))
        if not link or not title:
            continue

        when = entry_time(entry)
        if when is not None:
            newest = when if newest is None else max(newest, when)
            if when < cutoff:
                continue
            when = min(when, now)  # guard against feeds whose clocks run ahead

        summary = clean_text(
            getattr(entry, "summary", "") or getattr(entry, "description", ""),
            SUMMARY_CHARS,
        )
        # `published` stays None for undated entries; collect() stamps them
        # with the date we first saw them.
        items.append(Item(title, link, when, summary, feed.name, feed.section,
                          dated=when is not None))

    if not items and newest is not None:
        # The feed parsed and has entries, they're just all stale. Worth
        # surfacing — it usually means the publisher abandoned the feed.
        return feed, [], f"nothing newer than {newest:%d %b %Y}"
    return feed, items, None


def load_state() -> dict[str, str]:
    """link -> ISO timestamp we first saw it. Used only for undated feeds."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, str]) -> None:
    horizon = datetime.now(timezone.utc) - timedelta(days=60)
    pruned = {
        link: seen for link, seen in state.items()
        if datetime.fromisoformat(seen) >= horizon
    }
    try:
        STATE_FILE.write_text(json.dumps(pruned, indent=0, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(f"  ! could not write {STATE_FILE.name}: {exc}", file=sys.stderr)


def collect(feeds: list[Feed]) -> tuple[list[Item], list[tuple[str, str]]]:
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch, feeds))

    state = load_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    items: list[Item] = []
    errors: list[tuple[str, str]] = []
    seen: set[str] = set()

    for feed, feed_items, error in results:
        if error:
            errors.append((feed.name, error))
            print(f"  ! {feed.name}: {error}", file=sys.stderr)
            continue

        kept = 0
        for item in feed_items:
            key = canonical(item.link)
            if key in seen:
                continue

            if item.published is None:
                # Undated feed (Weekly Worker publishes no per-entry dates).
                # Anchor it to when we first saw the link, so it ages off the
                # top of the page instead of resurfacing on every build.
                first_seen = state.setdefault(key, now.isoformat())
                item.published = datetime.fromisoformat(first_seen)
                if item.published < cutoff:
                    continue

            seen.add(key)
            items.append(item)
            kept += 1

        print(f"  · {feed.name}: {kept} items", file=sys.stderr)

    save_state(state)
    items.sort(key=lambda i: i.published, reverse=True)
    return items[:MAX_TOTAL], errors


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(items: list[Item], errors: list[tuple[str, str]], sections: list[str]) -> str:
    e = html.escape
    built = datetime.now(timezone.utc)

    chips = ['<button class="chip is-active" data-filter="all">All</button>']
    chips += [
        f'<button class="chip" data-filter="{e(slug(s))}">{e(s)}</button>'
        for s in sections
    ]

    rows = []
    for item in items:
        iso = item.published.isoformat()
        summary = (
            f'<p class="summary">{e(item.summary)}</p>' if item.summary else ""
        )
        rows.append(
            f'<li class="item" data-section="{e(slug(item.section))}">'
            f'<a class="headline" href="{e(item.link)}" target="_blank" rel="noopener noreferrer">{e(item.title)}</a>'
            f'{summary}'
            f'<p class="meta"><span class="source">{e(item.source)}</span>'
            f'<time datetime="{e(iso)}"{"" if item.dated else " data-undated=\"1\""}>'
            f'{e(item.published.strftime("%d %b %H:%M"))}</time></p>'
            f"</li>"
        )

    problems = ""
    if errors:
        listed = ", ".join(f"{e(name)} ({e(msg)})" for name, msg in errors)
        problems = f'<p class="problems">Unavailable this run: {listed}</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>News Feed</title>
<style>
  :root {{
    --bg: #fbfaf8; --panel: #ffffff; --ink: #1b1a18; --muted: #6b6862;
    --line: #e4e0d9; --accent: #8c2f24; --chip: #f1eee8;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #16151a; --panel: #1e1d23; --ink: #ece9e4; --muted: #9a958d;
      --line: #302e37; --accent: #e0776a; --chip: #272630;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 46rem; margin: 0 auto; padding: 2rem 1.1rem 4rem; }}
  header {{ border-bottom: 1px solid var(--line); padding-bottom: 1rem; margin-bottom: 1.25rem; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .35rem; letter-spacing: -.01em; }}
  .built {{ color: var(--muted); font-size: .82rem; margin: 0; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: .4rem; margin: 1rem 0 1.5rem; }}
  .chip {{
    font: inherit; font-size: .85rem; cursor: pointer; color: var(--ink);
    background: var(--chip); border: 1px solid transparent; border-radius: 999px;
    padding: .3rem .8rem;
  }}
  .chip:hover {{ border-color: var(--line); }}
  .chip.is-active {{ background: var(--accent); color: #fff; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  .item {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: .85rem 1rem; margin-bottom: .6rem;
  }}
  .item[hidden] {{ display: none; }}
  .headline {{
    color: var(--ink); text-decoration: none; font-weight: 600;
    font-size: 1.03rem; line-height: 1.35; display: block;
  }}
  .headline:hover {{ color: var(--accent); text-decoration: underline; }}
  .summary {{ color: var(--muted); font-size: .9rem; margin: .35rem 0 0; }}
  .meta {{
    display: flex; gap: .5rem; align-items: baseline;
    margin: .5rem 0 0; font-size: .78rem; color: var(--muted);
  }}
  .source {{ font-weight: 600; color: var(--accent); }}
  .meta time::before {{ content: "·"; margin-right: .5rem; }}
  footer {{ margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--line);
            color: var(--muted); font-size: .8rem; }}
  .problems {{ color: var(--muted); font-size: .8rem; }}
  .empty {{ color: var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>News Feed</h1>
    <p class="built">{len(items)} stories · updated <time id="built" datetime="{e(built.isoformat())}">{e(built.strftime("%d %b %Y %H:%M UTC"))}</time></p>
  </header>

  <nav class="filters">{"".join(chips)}</nav>

  <ul id="list">
    {"".join(rows) or '<li class="empty">No stories found. Check the build log.</li>'}
  </ul>

  <footer>
    <p>Built from {len(sections)} sections of RSS. Edit <code>feeds.txt</code> to change sources.</p>
    {problems}
  </footer>
</div>

<script>
  // Relative times are computed in the browser so a cached page never
  // shows a stale "2 hours ago".
  const rtf = new Intl.RelativeTimeFormat(undefined, {{ numeric: "auto" }});
  const UNITS = [["year",31536000],["month",2592000],["week",604800],
                 ["day",86400],["hour",3600],["minute",60]];
  for (const el of document.querySelectorAll("time[datetime]")) {{
    const secs = (Date.now() - new Date(el.dateTime)) / 1000;
    const unit = UNITS.find(([, s]) => secs >= s);
    const rel = unit ? rtf.format(-Math.floor(secs / unit[1]), unit[0]) : "just now";
    el.title = el.textContent;
    // Sources that publish no date get "first seen", not a claimed pub date.
    if (el.dataset.undated) {{
      el.textContent = "first seen " + rel;
      el.title = "This source publishes no date; " + el.title + " is when this link first appeared.";
    }} else {{
      el.textContent = rel;
    }}
  }}

  const list = document.getElementById("list");
  document.querySelector(".filters").addEventListener("click", (ev) => {{
    const chip = ev.target.closest(".chip");
    if (!chip) return;
    for (const c of document.querySelectorAll(".chip")) c.classList.toggle("is-active", c === chip);
    const want = chip.dataset.filter;
    for (const item of list.querySelectorAll(".item")) {{
      item.hidden = want !== "all" && item.dataset.section !== want;
    }}
  }});
</script>
</body>
</html>
"""


def main() -> int:
    feeds = read_feeds()
    if not feeds:
        print("No feeds configured in feeds.txt", file=sys.stderr)
        return 1

    print(f"Fetching {len(feeds)} feeds…", file=sys.stderr)
    items, errors = collect(feeds)

    if not items:
        print("Every feed failed — not overwriting the page.", file=sys.stderr)
        return 1

    sections = list(dict.fromkeys(f.section for f in feeds))
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(render(items, errors, sections), encoding="utf-8")

    print(f"Wrote {OUT_FILE} — {len(items)} items, {len(errors)} feeds unavailable", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
