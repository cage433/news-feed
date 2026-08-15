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
from urllib.parse import quote, urlsplit, urlunsplit

import feedparser

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
OUT_FILE = ROOT / "out" / "index.html"
STATE_FILE = ROOT / "state.json"

DEFAULT_MAX_AGE_DAYS = 14   # used when feeds.txt gives no per-feed value
MAX_PER_FEED = 25       # keep a prolific source from swamping the page
MAX_TOTAL = 400         # overall cap on the page
SUMMARY_CHARS = 220
WORKERS = 8
RETRIES = 3             # transient 5xx / network blips
RETRY_BACKOFF = 2.0     # seconds, multiplied by attempt number

# Tab icon. An emoji drawn into an inline SVG and embedded as a data URI, so
# the page stays a single self-contained file — no favicon.ico to publish
# alongside it, and no request for the browser to 404 on. Change the emoji
# here to change the icon.
FAVICON_EMOJI = "📰"
FAVICON = "data:image/svg+xml," + quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    f"<text y='.95em' font-size='92'>{FAVICON_EMOJI}</text></svg>"
)

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
    dated: bool = True  # False when the feed gave no date and we stamped first-seen


@dataclass
class Feed:
    name: str
    url: str
    max_age_days: int = DEFAULT_MAX_AGE_DAYS


def read_feeds() -> tuple[list[Feed], list[tuple[str, str]]]:
    """Return (feeds, problems). Problems are reported on the page as well as
    the log — a skipped line means a source silently vanishes otherwise."""
    feeds: list[Feed] = []
    problems: list[tuple[str, str]] = []

    for lineno, raw in enumerate(FEEDS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) not in (2, 3):
            problems.append((f"feeds.txt:{lineno}",
                             "expected 'Name | URL' or 'Name | URL | days', skipped"))
            continue

        name, url = parts[0], parts[1]
        age = DEFAULT_MAX_AGE_DAYS
        if len(parts) == 3:
            try:
                age = int(parts[2])
                if age < 1:
                    raise ValueError
            except ValueError:
                problems.append((f"feeds.txt:{lineno}",
                                 f"'{parts[2]}' is not a positive number of days, "
                                 f"using {DEFAULT_MAX_AGE_DAYS}"))
                age = DEFAULT_MAX_AGE_DAYS

        feeds.append(Feed(name, url, age))

    for where, msg in problems:
        print(f"  ! {where}: {msg}", file=sys.stderr)
    return feeds, problems


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


def fetch(feed: Feed) -> tuple[Feed, list[Item], str | None, str | None]:
    """Return (feed, items, error, quiet).

    `error` is a real failure and is reported on the page. `quiet` means the
    feed is healthy but has published nothing inside its window — normal for
    an occasional essayist, so it goes to the log only. A broken feed must
    not fail the build either way.
    """
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
            return feed, [], f"HTTP {status}", None   # 404/403 won't fix itself
        if not parsed.entries:
            # feedparser doesn't raise on network failures — it returns an
            # empty feed with bozo_exception set. Those are worth retrying;
            # a genuinely empty but well-formed feed is not.
            reason = getattr(parsed, "bozo_exception", None)
            if reason is not None:
                problem = f"{type(reason).__name__}: {reason}"
                continue
            return feed, [], "no entries", None
        break
    else:
        return feed, [], problem, None

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=feed.max_age_days)
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
        items.append(Item(title, link, when, summary, feed.name,
                          dated=when is not None))

    if not items and newest is not None:
        # Parsed fine, entries present, just none inside the window. Common
        # for an essayist who posts every few months — log it, don't put it
        # on the page as though something were broken.
        return feed, [], None, f"nothing newer than {newest:%d %b %Y}"
    return feed, items, None, None


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

    items: list[Item] = []
    errors: list[tuple[str, str]] = []
    seen: set[str] = set()

    for feed, feed_items, error, quiet in results:
        if error:
            errors.append((feed.name, error))
            print(f"  ! {feed.name}: {error}", file=sys.stderr)
            continue
        if quiet:
            print(f"  · {feed.name}: {quiet} (last {feed.max_age_days}d)", file=sys.stderr)
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
                if item.published < now - timedelta(days=feed.max_age_days):
                    continue

            seen.add(key)
            items.append(item)
            kept += 1

        print(f"  · {feed.name}: {kept} items (last {feed.max_age_days}d)", file=sys.stderr)

    save_state(state)
    items.sort(key=lambda i: i.published, reverse=True)
    return items[:MAX_TOTAL], errors


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(items: list[Item], errors: list[tuple[str, str]]) -> str:
    e = html.escape
    built = datetime.now(timezone.utc)

    # One chip per source that actually produced stories, in alphabetical
    # order — with this many sources, being able to find one by name beats
    # having the noisiest first.
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source] = counts.get(item.source, 0) + 1
    ranked = sorted(counts, key=lambda s: s.lower())

    source_chips = [
        f'<button class="chip src" data-source="{e(slug(name))}">'
        f'{e(name)}<span class="count">{counts[name]}</span></button>'
        for name in ranked
    ]

    rows = []
    for item in items:
        iso = item.published.isoformat()
        summary = (
            f'<p class="summary">{e(item.summary)}</p>' if item.summary else ""
        )
        rows.append(
            # data-id is the canonical link: stable across rebuilds, which is
            # what read state has to hang off.
            f'<li class="item" data-source="{e(slug(item.source))}"'
            f' data-id="{e(canonical(item.link))}">'
            f'<a class="headline" href="{e(item.link)}" target="_blank" rel="noopener noreferrer">{e(item.title)}</a>'
            f'{summary}'
            f'<p class="meta"><span class="source">{e(item.source)}</span>'
            f'<time datetime="{e(iso)}"{"" if item.dated else " data-undated=\"1\""}>'
            f'{e(item.published.strftime("%d %b %H:%M"))}</time>'
            # Empty box; the tick is filled in by JS for read stories only.
            f'<button class="mark" type="button" title="Mark as read"></button></p>'
            f"</li>"
        )

    # Only rendered when something actually went wrong, so the page ends
    # cleanly at the last story on a normal run.
    problems = ""
    if errors:
        listed = ", ".join(f"{e(name)} ({e(msg)})" for name, msg in errors)
        problems = (
            '<footer><p class="problems">Unavailable this run: '
            f'{listed}</p></footer>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>News Feed</title>
<link rel="icon" href="{FAVICON}">
<link rel="apple-touch-icon" href="{FAVICON}">
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
    /* A long press excludes a source, so suppress the OS text-selection
       callout that would otherwise fire at the same moment.
       touch-action also drops the 300ms double-tap delay. */
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
    touch-action: manipulation;
  }}
  .chip:hover {{ border-color: var(--line); }}
  /* Three states: focused (solid), excluded (struck through), and dimmed —
     the last for sources merely not focused, so a focus reads as one bright
     chip rather than eight rejected ones. */
  .sources {{ gap: .35rem; }}
  .chip.src {{ font-size: .78rem; padding: .25rem .6rem; }}
  .chip.src.is-focus {{ background: var(--accent); color: #fff; }}
  .chip.src.is-focus .count {{ opacity: .75; }}
  .chip.src.is-excluded {{ opacity: .38; text-decoration: line-through; }}
  .chip.src.is-dimmed {{ opacity: .45; }}
  /* Visible feedback while a long press is arming, so a hold that hasn't
     registered yet is distinguishable from one that has. */
  .chip.src.is-pressing {{ transform: scale(.92); opacity: .7; }}
  .chip.src {{
    transition: transform .12s ease, opacity .12s ease;
    /* Not `manipulation`: that leaves the browser free to reinterpret the
       touch as a page scroll, and Android fires pointercancel the moment it
       starts considering that — killing the long press every time. `none`
       claims touches on a chip outright. The row is a thin strip, so
       scrolling elsewhere is unaffected. */
    touch-action: none;
  }}
  .chip.src[hidden], .chip.link[hidden] {{ display: none; }}
  .view {{ margin-top: -.9rem; }}
  #unread-only.is-active {{ background: var(--accent); color: #fff; }}
  /* Read stories stay on the page but recede, so the eye lands on what's
     new without anything disappearing unless you ask for it. */
  .item.is-read {{ opacity: .5; }}
  .item.is-read .headline {{ font-weight: 500; color: var(--muted); }}
  /* Fixed square so the box doesn't resize when the tick appears. */
  .mark {{
    font: inherit; font-size: .78rem; line-height: 1; cursor: pointer;
    margin-left: auto; flex: none; width: 1.15rem; height: 1.15rem;
    display: inline-flex; align-items: center; justify-content: center;
    padding: 0; border-radius: 4px;
    /* Same colour whether ticked or not: the tick alone carries the state.
       Using --line for the empty box made it invisible in dark mode, where
       the border and the panel behind it are nearly the same value. */
    background: none; border: 1px solid var(--accent); color: var(--accent);
  }}
  .mark:hover {{ background: var(--chip); }}
  .count {{ opacity: .55; margin-left: .35rem; font-variant-numeric: tabular-nums; }}
  .chip.link {{
    background: none; color: var(--muted); text-decoration: underline;
    padding: .25rem .4rem; font-size: .78rem;
  }}
  footer {{ margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--line); }}
  .problems {{ color: var(--muted); font-size: .8rem; margin: 0; }}
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
  .empty {{ color: var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>News Feed</h1>
    <p class="built"><span id="shown">{len(items)}</span> stories ·
       <span id="unread">0</span> unread · updated
       <time id="built" datetime="{e(built.isoformat())}">{e(built.strftime("%d %b %Y %H:%M UTC"))}</time></p>
  </header>

  <nav class="filters sources" id="sources">
    {"".join(source_chips)}
    <button class="chip link" id="clear" hidden>all</button>
  </nav>

  <nav class="filters view">
    <button class="chip" id="unread-only" type="button">unread only</button>
    <button class="chip link" id="mark-all" type="button">mark all read</button>
  </nav>

  <ul id="list">
    {"".join(rows) or '<li class="empty">No stories found. Check the build log.</li>'}
  </ul>

  {problems}
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
  const items = [...list.querySelectorAll(".item")];
  const sourceChips = [...document.querySelectorAll(".chip.src")];
  const countEl = document.getElementById("shown");
  const clearBtn = document.getElementById("clear");

  // Two independent source filters: `focus` narrows to a single source,
  // `excluded` hides several. Focus wins while it's set — combining them
  // would let you focus a source and exclude it at the same time.
  const unreadEl = document.getElementById("unread");
  const unreadBtn = document.getElementById("unread-only");
  const markAllBtn = document.getElementById("mark-all");

  const saved = JSON.parse(localStorage.getItem("newsfeed-filters") || "{{}}");
  let focus = saved.focus || null;
  let unreadOnly = Boolean(saved.unreadOnly);
  const excluded = new Set(saved.excluded || []);

  // Read state is keyed by canonical link and kept separately, so clearing
  // filters never clears what you've read. Each entry stores when it was
  // marked, and old ones expire — pruning to "stories on the page right now"
  // would wipe a source's history during a transient feed outage, and every
  // one of its stories would come back unread.
  const READ_TTL_DAYS = 180;
  const raw = JSON.parse(localStorage.getItem("newsfeed-read") || "{{}}");
  const horizon = Date.now() - READ_TTL_DAYS * 86400000;
  const read = new Map(
    Object.entries(Array.isArray(raw) ? {{}} : raw).filter(([, at]) => at > horizon));

  function saveRead() {{
    localStorage.setItem("newsfeed-read", JSON.stringify(Object.fromEntries(read)));
  }}

  // Stories marked read in this session stay on screen even under "unread
  // only", so the list doesn't reshuffle under the cursor as you work down
  // it. Not persisted: they're gone on the next load, which is the point.
  const justRead = new Set();

  function setRead(item, value) {{
    const id = item.dataset.id;
    if (value) {{
      read.set(id, Date.now());
      justRead.add(id);
    }} else {{
      read.delete(id);
      justRead.delete(id);
    }}
    saveRead();
  }}

  saveRead();  // persist the expiry sweep even if nothing else happens

  function apply() {{
    let shown = 0, unread = 0;
    for (const item of items) {{
      const src = item.dataset.source;
      const isRead = read.has(item.dataset.id);
      const passesSource = focus ? src === focus : !excluded.has(src);
      const ok = passesSource
                 && (!unreadOnly || !isRead || justRead.has(item.dataset.id));

      item.hidden = !ok;
      item.classList.toggle("is-read", isRead);
      const mark = item.querySelector(".mark");
      mark.textContent = isRead ? "✓" : "";
      mark.title = isRead ? "Mark as unread" : "Mark as read";
      if (ok) shown++;
      // Unread count tracks the source filter but ignores the unread toggle —
      // otherwise turning "unread only" on would always report zero left.
      if (passesSource && !isRead) unread++;
    }}

    for (const c of sourceChips) {{
      const src = c.dataset.source;
      c.classList.toggle("is-focus", focus === src);
      c.classList.toggle("is-excluded", !focus && excluded.has(src));
      c.classList.toggle("is-dimmed", Boolean(focus) && focus !== src);
    }}

    unreadBtn.classList.toggle("is-active", unreadOnly);
    markAllBtn.hidden = unread === 0;
    // Only offer "all" when there's actually something to clear.
    clearBtn.hidden = !focus && excluded.size === 0;
    countEl.textContent = shown;
    unreadEl.textContent = unread;
    localStorage.setItem("newsfeed-filters",
      JSON.stringify({{ focus, excluded: [...excluded], unreadOnly }}));
  }}

  function toggleExclude(src) {{
    // Drop any focus — otherwise excluding the focused source would leave an
    // empty page with no obvious way back.
    focus = null;
    if (excluded.has(src)) excluded.delete(src);
    else excluded.add(src);
  }}

  function toggleFocus(src) {{
    // Focusing is a fresh start: drop exclusions too, so no invisible state
    // survives behind the focused view and surprises you later.
    focus = focus === src ? null : src;
    excluded.clear();
  }}

  const sourcesNav = document.getElementById("sources");

  sourcesNav.addEventListener("click", (ev) => {{
    const chip = ev.target.closest(".chip");
    if (!chip) return;

    // Swallow the click the browser fires after a long press.
    if (longPressed) {{ longPressed = false; return; }}

    if (chip === clearBtn) {{
      focus = null;
      excluded.clear();
    }} else if (chip.dataset.source) {{
      if (ev.shiftKey) toggleExclude(chip.dataset.source);
      else toggleFocus(chip.dataset.source);
    }}
    apply();
  }});

  // Touch has no shift key, so a long press is the mobile equivalent of
  // shift-click. Restricted to touch and pen: on a mouse it would mean a
  // slow click silently excluded a source instead of focusing it.
  const LONG_PRESS_MS = 450;
  const MOVE_TOLERANCE = 12;   // px; a resting finger is never perfectly still
  let pressTimer = null;
  let longPressed = false;
  let pressChip = null;
  let pressX = 0, pressY = 0;

  function cancelPress() {{
    if (pressTimer !== null) {{ clearTimeout(pressTimer); pressTimer = null; }}
    if (pressChip) {{ pressChip.classList.toggle("is-pressing", false); pressChip = null; }}
  }}

  sourcesNav.addEventListener("pointerdown", (ev) => {{
    // Anything that isn't a mouse. Some browsers report an empty pointerType,
    // and treating those as touch is the safer failure.
    if (ev.pointerType === "mouse") return;
    const chip = ev.target.closest(".chip.src");
    if (!chip) return;

    longPressed = false;
    cancelPress();
    pressChip = chip;
    pressX = ev.clientX || 0;
    pressY = ev.clientY || 0;
    chip.classList.toggle("is-pressing", true);

    // Keep receiving events for this pointer even if the finger drifts off
    // the chip, and stop the browser retargeting them elsewhere.
    if (chip.setPointerCapture && ev.pointerId !== undefined) {{
      try {{ chip.setPointerCapture(ev.pointerId); }} catch (e) {{ /* not fatal */ }}
    }}

    pressTimer = setTimeout(() => {{
      pressTimer = null;
      longPressed = true;          // tells the click handler to stand down
      if (pressChip) pressChip.classList.toggle("is-pressing", false);
      pressChip = null;
      toggleExclude(chip.dataset.source);
      apply();
    }}, LONG_PRESS_MS);
  }});

  // Only a real drag cancels. Cancelling on any pointermove at all meant the
  // press never survived the tiny jitter of a finger resting on the glass.
  sourcesNav.addEventListener("pointermove", (ev) => {{
    if (pressTimer === null) return;
    const dx = (ev.clientX || 0) - pressX;
    const dy = (ev.clientY || 0) - pressY;
    if (Math.hypot(dx, dy) > MOVE_TOLERANCE) cancelPress();
  }});

  // Deliberately not pointerleave: setting pointer capture can itself fire
  // leave events on the old target chain, which would cancel instantly.
  for (const evt of ["pointerup", "pointercancel"]) {{
    sourcesNav.addEventListener(evt, cancelPress);
  }}

  // Stop iOS/Android offering their own copy-and-select menu on a long press.
  sourcesNav.addEventListener("contextmenu", (ev) => {{
    if (ev.target.closest(".chip.src")) ev.preventDefault();
  }});

  // Load the page with #debug to see which pointer events actually fire on a
  // phone. Touch behaviour can't be reproduced in the test harness, so this
  // is the only way to diagnose it without guessing.
  if (typeof location !== "undefined" && location.hash.indexOf("debug") !== -1) {{
    const panel = document.createElement("div");
    panel.style.cssText = "position:fixed;bottom:0;left:0;right:0;max-height:45vh;"
      + "overflow:auto;background:#000;color:#4f4;font:11px/1.4 monospace;"
      + "padding:.5rem;z-index:9999;white-space:pre-wrap";
    document.body.appendChild(panel);

    let n = 0;
    const t0 = Date.now();
    const say = (m) => {{
      panel.textContent = (++n) + " +" + (Date.now() - t0) + "ms  " + m
        + "\\n" + panel.textContent.slice(0, 3000);
    }};
    say("debug on — press and hold a source chip");

    for (const t of ["pointerdown", "pointerup", "pointercancel", "pointerleave",
                     "contextmenu", "click", "touchstart", "touchend", "touchcancel"]) {{
      sourcesNav.addEventListener(t, (ev) => {{
        const c = ev.target && ev.target.className ? String(ev.target.className) : "-";
        say(t + "  pointerType=" + (ev.pointerType || "-") + "  target=" + c);
      }}, true);
    }}
  }}

  // Opening a story marks it read; the ✓ button toggles without opening.
  list.addEventListener("click", (ev) => {{
    const item = ev.target.closest(".item");
    if (!item) return;

    if (ev.target.closest(".mark")) {{
      setRead(item, !read.has(item.dataset.id));
      apply();
    }} else if (ev.target.closest(".headline")) {{
      setRead(item, true);
      apply();
    }}
  }});

  unreadBtn.addEventListener("click", () => {{
    unreadOnly = !unreadOnly;
    apply();
  }});

  markAllBtn.addEventListener("click", () => {{
    // Only what's currently visible, so it respects the source filter and
    // can't silently bury stories you've filtered out of sight. Unlike a
    // single mark, this clears the view — emptying the queue is the point.
    for (const item of items) if (!item.hidden) read.set(item.dataset.id, Date.now());
    justRead.clear();
    saveRead();
    apply();
  }});

  apply();
</script>
</body>
</html>
"""


def main() -> int:
    feeds, problems = read_feeds()
    if not feeds:
        print("No feeds configured in feeds.txt", file=sys.stderr)
        return 1

    print(f"Fetching {len(feeds)} feeds…", file=sys.stderr)
    items, errors = collect(feeds)
    errors = problems + errors

    if not items:
        print("Every feed failed — not overwriting the page.", file=sys.stderr)
        return 1

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(render(items, errors), encoding="utf-8")

    print(f"Wrote {OUT_FILE} — {len(items)} items, {len(errors)} problems", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
