#!/bin/zsh
# Install (or remove) the launchd job that rebuilds and publishes the page
# every 30 minutes.
#
#   ./install-agent.sh              install and start
#   ./install-agent.sh --uninstall  stop and remove
#   ./install-agent.sh --status     is it running, and when did it last run?
#
# The plist is generated here rather than committed, so it always points at
# wherever this checkout actually lives. That makes moving to another Mac a
# clone plus one command.

set -u

REPO="${0:A:h}"
LABEL="com.cage433.newsfeed"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TARGET="gui/$(id -u)/$LABEL"

case "${1:-}" in
  --uninstall)
    launchctl bootout "$TARGET" 2>/dev/null && print "stopped $LABEL" || print "was not running"
    rm -f "$PLIST" && print "removed $PLIST"
    print "\nThe repo and the published page are untouched. To publish by hand:"
    print "  $REPO/publish.sh"
    exit 0
    ;;
  --status)
    if launchctl print "$TARGET" >/dev/null 2>&1; then
      launchctl print "$TARGET" | grep -E "^\s+(state|runs|last exit code) " | sed 's/^[[:space:]]*/  /'
    else
      print "  not loaded"
    fi
    print "\nrecent activity:"
    tail -n 3 "$HOME/Library/Logs/news-feed.log" 2>/dev/null | sed 's/^/  /' || print "  no log yet"
    exit 0
    ;;
esac

# --- preflight -------------------------------------------------------------
fail=0
[[ -x "$REPO/publish.sh" ]]     || { print "missing: publish.sh (or not executable)"; fail=1 }
[[ -x "$REPO/.venv/bin/python" ]] || { print "missing: .venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; fail=1 }
command -v deno >/dev/null      || { print "missing: deno — run: brew install deno"; fail=1 }
git -C "$REPO" remote get-url origin >/dev/null 2>&1 || { print "missing: git remote 'origin'"; fail=1 }
(( fail )) && exit 1

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat >"$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$REPO/publish.sh</string>
  </array>

  <!-- Every 30 minutes. Unlike cron, launchd runs a job it missed while the
       machine was asleep rather than silently skipping it. -->
  <key>StartInterval</key>
  <integer>1800</integer>

  <key>RunAtLoad</key>
  <true/>

  <!-- publish.sh keeps its own log; these catch anything that escapes it. -->
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/news-feed.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/news-feed.launchd.log</string>

  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLISTEOF

plutil -lint "$PLIST" >/dev/null || { print "generated plist is malformed"; exit 1 }

launchctl bootout "$TARGET" 2>/dev/null          # replace any existing job
launchctl bootstrap "gui/$(id -u)" "$PLIST" || { print "bootstrap failed"; exit 1 }

print "installed $LABEL"
print "  repo:  $REPO"
print "  runs:  every 30 minutes, and once now"
print "  log:   ~/Library/Logs/news-feed.log"
print "\nCheck on it with: ./install-agent.sh --status"
