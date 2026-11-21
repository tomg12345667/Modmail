#!/usr/bin/env bash
# Auto-deploy Modmail from GitHub to every bot on this server.
#
# Checks the GitHub repo for a new commit. When there is one, it builds an
# update from it and runs update-bots.sh, which backs up each bot, updates
# it, checks it starts, and rolls it back if anything breaks.
#
#   bash ~/auto-deploy.sh          check once (cron runs this every few minutes)
#   bash ~/auto-deploy.sh --init   mark the current commit as already deployed
#   bash ~/auto-deploy.sh --force  deploy the latest commit even if it was tried before
#
# History is written to ~/modmail-deploy.log

set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

REPO_URL="${REPO_URL:-https://github.com/tomg12345667/Modmail.git}"
BRANCH="${BRANCH:-master}"
SRC="${SRC:-$HOME/modmail-src}"            # a copy of the repo, used only for building updates
UPDATER="${UPDATER:-$HOME/update-bots.sh}"
STATE="${STATE:-$HOME/.modmail-deploy}"
LOG="${LOG:-$HOME/modmail-deploy.log}"

# The parts of the repo that make up the bot. .env, data/ and installed
# plugins are never touched (update-bots.sh protects them too).
DEPLOY_PATHS=(bot.py core cogs requirements.txt plugins/registry.json tools)

mkdir -p "$STATE"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

# Never run two deploys at once (e.g. a slow update overlapping the next cron run).
exec 9>"$STATE/lock"
if ! flock -n 9; then
  exit 0
fi

command -v git >/dev/null || { log "ERROR: git is not installed (sudo apt install -y git)"; exit 1; }
[ -f "$UPDATER" ] || { log "ERROR: $UPDATER not found"; exit 1; }

if [ ! -d "$SRC/.git" ]; then
  log "Cloning $REPO_URL into $SRC"
  git clone -q --branch "$BRANCH" "$REPO_URL" "$SRC" || { log "ERROR: clone failed"; exit 1; }
fi

if ! git -C "$SRC" fetch -q origin "$BRANCH" 2>>"$LOG"; then
  log "ERROR: couldn't reach GitHub; will try again next time"
  exit 1
fi
NEW=$(git -C "$SRC" rev-parse "origin/$BRANCH")
SHORT=${NEW:0:7}
LAST=$(cat "$STATE/deployed" 2>/dev/null || true)
FAILED=$(cat "$STATE/failed" 2>/dev/null || true)

case "${1:-}" in
  --init)
    echo "$NEW" > "$STATE/deployed"; rm -f "$STATE/failed"
    log "Marked $SHORT as deployed (nothing changed on the bots)"
    exit 0;;
  --force)
    rm -f "$STATE/failed"; FAILED=""; LAST="";;
esac

[ "$NEW" = "$LAST" ] && exit 0
if [ "$NEW" = "$FAILED" ]; then
  exit 0   # this commit already failed; wait for a newer one (or use --force)
fi

git -C "$SRC" reset -q --hard "origin/$BRANCH"
MSG=$(git -C "$SRC" log -1 --format='%s' "$NEW")
log "New commit $SHORT: $MSG"

ZIP="$STATE/modmail-update-$SHORT.zip"
rm -f "$STATE"/modmail-update-*.zip
existing=()
for p in "${DEPLOY_PATHS[@]}"; do [ -e "$SRC/$p" ] && existing+=("$p"); done
if ! (cd "$SRC" && git archive --format=zip -o "$ZIP" HEAD -- "${existing[@]}"); then
  log "ERROR: couldn't build the update"; echo "$NEW" > "$STATE/failed"; exit 1
fi

if bash "$UPDATER" "$ZIP" >>"$LOG" 2>&1; then
  echo "$NEW" > "$STATE/deployed"; rm -f "$STATE/failed"
  log "Deployed $SHORT to all bots"
else
  echo "$NEW" > "$STATE/failed"
  log "Deploy of $SHORT had problems; see the summary above. Bots that failed were rolled back."
  exit 1
fi
