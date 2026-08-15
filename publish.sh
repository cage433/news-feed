#!/bin/zsh
# Build the page and publish it to the `published` branch, which GitHub Pages
# serves directly.
#
# The build runs here rather than in GitHub Actions because six sources
# (Electronic Intifada and five *.substack.com feeds) return HTTP 403 to
# GitHub's datacenter IP ranges. They answer a home connection fine.
#
# Run by launchd every 30 minutes; safe to run by hand at any time.

set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO="/Users/alex/repos/news-feed"
LOG="$HOME/Library/Logs/news-feed.log"
LOCK="$REPO/.publish.lock"
BRANCH="published"

cd "$REPO" || exit 1

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S')  $*" >>"$LOG"; }

# Keep the log from growing forever.
if [[ -f "$LOG" && $(wc -c <"$LOG") -gt 1000000 ]]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# mkdir is atomic, so this is a reliable lock. A stale lock older than an
# hour is assumed to be a crashed run rather than a live one.
if ! mkdir "$LOCK" 2>/dev/null; then
  if [[ -n $(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null) ]]; then
    log "clearing stale lock"
    rmdir "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { log "could not take lock, giving up"; exit 1; }
  else
    log "another run is in progress, skipping"
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# --- build -----------------------------------------------------------------
if ! .venv/bin/python build.py >>"$LOG" 2>&1; then
  log "BUILD FAILED — keeping the previously published page"
  exit 1
fi

# --- test ------------------------------------------------------------------
# The page's JavaScript lives inside a Python f-string. Test before publishing
# so a broken filter never reaches the live site.
for t in test_filters.js test_read_expiry.js; do
  if ! deno run --allow-read=. "$t" >/dev/null 2>>"$LOG"; then
    log "TEST FAILED ($t) — not publishing"
    exit 1
  fi
done

# --- publish ---------------------------------------------------------------
# Build a single orphan commit containing just the page and force-push it, so
# the branch always holds exactly one commit instead of accumulating 48 a day.
blob=$(git hash-object -w out/index.html) || { log "hash-object failed"; exit 1; }
nojekyll=$(printf '' | git hash-object -w --stdin)

tree=$(
  {
    printf '100644 blob %s\t.nojekyll\n' "$nojekyll"
    printf '100644 blob %s\tindex.html\n' "$blob"
  } | git mktree
) || { log "mktree failed"; exit 1; }

commit=$(git commit-tree "$tree" -m "Published $(date -u '+%Y-%m-%dT%H:%M:%SZ')") \
  || { log "commit-tree failed"; exit 1; }

# Braces are load-bearing: in zsh, "$commit:refs/..." applies the `:r`
# history modifier to $commit and silently mangles the refspec.
if git push -q -f origin "${commit}:refs/heads/${BRANCH}" 2>>"$LOG"; then
  # -o, not -c: the rows are emitted on a single line, so counting lines
  # would always report 1.
  items=$(grep -o 'class="item"' out/index.html | wc -l | tr -d ' ')
  log "published $items stories"
else
  log "PUSH FAILED"
  exit 1
fi
