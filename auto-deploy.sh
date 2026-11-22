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
# It also keeps the web dashboard (~/modmail-dashboard) up to date: when the
# dashboard/ folder changes on GitHub, the new files are copied in (your .env,
# bots.json and .venv are never touched) and the dashboard is restarted.
#
# History is written to ~/modmail-deploy.log

set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

REPO_URL="${REPO_URL:-https://github.com/tomg12345667/Modmail.git}"
BRANCH="${BRANCH:-master}"
SRC="${SRC:-$HOME/modmail-src}"            # a copy of the repo, used only for building updates
UPDATER="${UPDATER:-$HOME/update-bots.sh}"
STATE="${STATE:-$HOME/.modmail-deploy}"
DASH_DIR="${DASH_DIR:-$HOME/modmail-dashboard}"
DASH_PM2="${DASH_PM2:-modmail-dashboard}"
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

deploy_dashboard() {
  [ -d "$DASH_DIR" ] || return 0                       # dashboard not installed on this server
  local tree; tree=$(git -C "$SRC" rev-parse "$NEW:dashboard" 2>/dev/null) || return 0
  [ "$tree" = "$(cat "$STATE/dashboard" 2>/dev/null)" ] && return 0
  local old_req; old_req=$(sha256sum "$DASH_DIR/requirements.txt" 2>/dev/null | cut -d' ' -f1)
  # Only files kept in git are copied, so .env, bots.json and .venv stay as they are.
  if ! git -C "$SRC" archive "$NEW" dashboard | tar -x --strip-components=1 -C "$DASH_DIR"; then
    log "ERROR: couldn't copy the dashboard update"; return 1
  fi
  if [ -x "$DASH_DIR/.venv/bin/pip" ] && [ "$old_req" != "$(sha256sum "$DASH_DIR/requirements.txt" | cut -d' ' -f1)" ]; then
    "$DASH_DIR/.venv/bin/pip" install -q -r "$DASH_DIR/requirements.txt" >>"$LOG" 2>&1 || log "WARNING: dashboard packages didn't install"
  fi
  if command -v pm2 >/dev/null && pm2 describe "$DASH_PM2" >/dev/null 2>&1; then
    pm2 restart "$DASH_PM2" --update-env >/dev/null 2>&1 && log "Dashboard updated to $SHORT and restarted" || log "WARNING: dashboard updated but didn't restart"
  else
    log "Dashboard files updated to $SHORT (start it with: cd $DASH_DIR && pm2 start ecosystem.config.js)"
  fi
  echo "$tree" > "$STATE/dashboard"
}

[ "$NEW" = "$LAST" ] && { deploy_dashboard; exit 0; }
if [ "$NEW" = "$FAILED" ]; then
  deploy_dashboard
  exit 0   # this commit already failed; wait for a newer one (or use --force)
fi

git -C "$SRC" reset -q --hard "origin/$BRANCH"
MSG=$(git -C "$SRC" log -1 --format='%s' "$NEW")
log "New commit $SHORT: $MSG"
deploy_dashboard

existing_paths() { for p in "${DEPLOY_PATHS[@]}"; do [ -e "$SRC/$p" ] && echo "$p"; done; }
# Commits that only change the dashboard (or docs) don't need the bots restarted.
if [ -n "$LAST" ] && git -C "$SRC" cat-file -e "$LAST" 2>/dev/null \
   && git -C "$SRC" diff --quiet "$LAST" "$NEW" -- $(existing_paths); then
  echo "$NEW" > "$STATE/deployed"; rm -f "$STATE/failed"
  log "No bot changes in $SHORT; bots left running"
  exit 0
fi

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
