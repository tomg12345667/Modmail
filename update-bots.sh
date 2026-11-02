#!/usr/bin/env bash
# Update and health-check every Modmail bot on this server.
#
#   bash update-bots.sh                     update all bots with the newest ~/modmail-update-*.zip
#   bash update-bots.sh path/to/update.zip  update all bots with that zip
#   bash update-bots.sh check               only run health checks, change nothing
#
# For each bot it: backs up its files, applies the update, checks the code
# compiles, restarts it in PM2 and confirms it stays online. If anything
# fails, that bot is rolled back to its backup automatically.
# .env and data/ are never overwritten.

set -uo pipefail

# --- Bot folders (add more here) ------------------------------------------
BOTS=(
  "$HOME/modmail"
  "$HOME/nwipbx-modmail"
)
STABLE_SECONDS=${STABLE_SECONDS:-20}   # how long a bot must stay up after restart to count as healthy

# ---------------------------------------------------------------------------
G=$'\033[1;32m'; R=$'\033[1;31m'; Y=$'\033[1;33m'; B=$'\033[1;34m'; N=$'\033[0m'
say()  { printf '\n%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '   %s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '   %s!%s %s\n' "$Y" "$N" "$*"; }
bad()  { printf '   %s✘%s %s\n' "$R" "$N" "$*"; }

pm2_name() {  # PM2 process name from the bot's ecosystem file
  sed -n 's/.*name: *"\([^"]*\)".*/\1/p' "$1/ecosystem.config.js" 2>/dev/null | head -1
}

pm2_info() {  # prints "<status> <restarts>" for a PM2 process, or "missing 0"
  pm2 jlist 2>/dev/null | python3 -c '
import json, sys
name = sys.argv[1]
try:
    procs = json.load(sys.stdin)
except Exception:
    procs = []
for p in procs:
    if p.get("name") == name:
        env = p.get("pm2_env", {})
        print(env.get("status", "unknown"), env.get("restart_time", 0))
        break
else:
    print("missing 0")
' "$1"
}

# Static checks for one bot folder. Prints results; returns non-zero on a hard failure.
check_files() {
  local dir="$1" fail=0
  [ -f "$dir/bot.py" ]              && ok "code present"            || { bad "bot.py missing"; fail=1; }
  [ -x "$dir/.venv/bin/python" ]    && ok "Python environment"      || { bad "no .venv (run install steps)"; fail=1; }
  if [ -f "$dir/.env" ]; then
    ok ".env present"
    grep -q '^TOKEN=..*' "$dir/.env"    || { bad ".env has no TOKEN"; fail=1; }
    grep -q '^GUILD_ID=..*' "$dir/.env" || { bad ".env has no GUILD_ID"; fail=1; }
    grep -qi '^DISABLE_AUTOUPDATES=true' "$dir/.env" \
      || warn "DISABLE_AUTOUPDATES=true is not set; ?update could undo the JSON changes"
  else
    bad ".env missing"; fail=1
  fi
  if [ -d "$dir/data" ]; then
    local n; n=$(find "$dir/data" -name '*.json' | wc -l)
    ok "data folder ($n records, $(du -sh "$dir/data" 2>/dev/null | cut -f1))"
    local broken
    broken=$("$dir/.venv/bin/python" - "$dir/data" <<'PY' 2>/dev/null
import json, os, sys
bad = []
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        if f.endswith(".json"):
            p = os.path.join(root, f)
            try:
                json.load(open(p, encoding="utf-8"))
            except Exception:
                bad.append(os.path.relpath(p, sys.argv[1]))
print("\n".join(bad))
PY
)
    if [ -n "$broken" ]; then
      warn "unreadable data files (the bot will skip them):"; printf '      %s\n' $broken
    fi
  else
    warn "no data folder yet (created on first start)"
  fi
  if [ -x "$dir/.venv/bin/python" ] && [ -f "$dir/bot.py" ]; then
    if (cd "$dir" && .venv/bin/python -m compileall -q bot.py core cogs >/dev/null 2>&1); then
      ok "code compiles"
    else
      bad "code has syntax errors"; fail=1
    fi
  fi
  return $fail
}

check_running() {  # $1=pm2 name
  local info status restarts
  info=$(pm2_info "$1"); status=${info% *}; restarts=${info#* }
  case "$status" in
    online)  ok "PM2: online (restarted $restarts times in total)";;
    missing) bad "PM2: no process called \"$1\""; return 1;;
    *)       bad "PM2: $status"; return 1;;
  esac
}

wait_stable() {  # $1=pm2 name: is the process still online after STABLE_SECONDS without crash-restarting?
  local name="$1" before after
  sleep 3
  before=$(pm2_info "$name")
  [ "${before% *}" = online ] || return 1
  sleep "$STABLE_SECONDS"
  after=$(pm2_info "$name")
  [ "$after" = "$before" ]
}

# ---------------------------------------------------------------------------
command -v pm2 >/dev/null     || { bad "PM2 is not installed"; exit 1; }
command -v python3 >/dev/null || { bad "python3 is not installed"; exit 1; }
command -v unzip >/dev/null   || { bad "unzip is not installed (sudo apt install -y unzip)"; exit 1; }

MODE="update"; ZIP=""
if [ "${1:-}" = "check" ]; then
  MODE="check"
elif [ -n "${1:-}" ]; then
  ZIP="$1"
else
  ZIP=$(ls -t "$HOME"/modmail-update-*.zip 2>/dev/null | head -1 || true)
fi

declare -A RESULT

# --- Check-only mode --------------------------------------------------------
if [ "$MODE" = "check" ]; then
  for dir in "${BOTS[@]}"; do
    name=$(pm2_name "$dir")
    say "${name:-$dir}  ($dir)"
    if [ ! -d "$dir" ]; then bad "folder not found"; RESULT[$dir]="MISSING"; continue; fi
    r=OK
    check_files "$dir" || r=PROBLEM
    if [ -n "$name" ]; then check_running "$name" || r=PROBLEM; else bad "no PM2 name in ecosystem.config.js"; r=PROBLEM; fi
    RESULT[$dir]=$r
  done
  say "Summary"
  for dir in "${BOTS[@]}"; do
    [ "${RESULT[$dir]}" = OK ] && ok "$dir: healthy" || bad "$dir: ${RESULT[$dir]}"
  done
  exit 0
fi

# --- Update mode ------------------------------------------------------------
[ -n "$ZIP" ] || { bad "No update zip given and none found matching ~/modmail-update-*.zip"; exit 1; }
[ -f "$ZIP" ] || { bad "File not found: $ZIP"; exit 1; }
ZIP=$(cd "$(dirname "$ZIP")" && pwd)/$(basename "$ZIP")
unzip -tq "$ZIP" >/dev/null 2>&1 || { bad "$ZIP is damaged or not a zip file"; exit 1; }

say "Update file: $ZIP"
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
unzip -q "$ZIP" -d "$STAGE"
# Older update zips put everything inside a "modmail/" folder; handle both layouts.
SRC="$STAGE"
if [ ! -d "$STAGE/core" ] && [ ! -d "$STAGE/cogs" ] && [ ! -f "$STAGE/bot.py" ]; then
  sub=$(find "$STAGE" -mindepth 1 -maxdepth 1 -type d | head -1)
  [ -n "$sub" ] && SRC="$sub"
fi
# Never let an update overwrite settings, data or the Python environment.
rm -rf "$SRC/.env" "$SRC/data" "$SRC/.venv" "$SRC/backups" "$SRC/plugins/@local"
FILES=$(cd "$SRC" && find . -type f ! -name ecosystem.config.js | sed 's#^\./##' | sort)
[ -n "$FILES" ] || { bad "The zip has no files to install"; exit 1; }
echo "   Files in update:"; printf '     %s\n' $FILES

for dir in "${BOTS[@]}"; do
  name=$(pm2_name "$dir")
  say "Updating ${name:-$dir}  ($dir)"
  if [ ! -d "$dir" ]; then bad "folder not found, skipping"; RESULT[$dir]="SKIPPED (folder missing)"; continue; fi
  if [ -z "$name" ]; then bad "no PM2 name in ecosystem.config.js, skipping"; RESULT[$dir]="SKIPPED (no PM2 name)"; continue; fi

  # 1. Back up everything this update touches, plus .env and data/.
  stamp=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$dir/backups"
  backup="$dir/backups/before-update-$stamp.tar.gz"
  existing=()
  for f in $FILES; do [ -e "$dir/$f" ] && existing+=("$f"); done
  [ -f "$dir/.env" ] && existing+=(".env")
  [ -d "$dir/data" ] && existing+=("data")
  if ! tar -czf "$backup" -C "$dir" "${existing[@]}" requirements.txt 2>/dev/null; then
    bad "backup failed, not touching this bot"; RESULT[$dir]="SKIPPED (backup failed)"; continue
  fi
  new_files=()
  for f in $FILES; do [ -e "$dir/$f" ] || new_files+=("$f"); done
  ok "backup saved: backups/$(basename "$backup")"

  rollback() {
    warn "rolling back $name"
    for f in "${new_files[@]}"; do rm -f "$dir/$f"; done
    tar -xzf "$backup" -C "$dir" --exclude=data --exclude=.env
    pm2 restart "$name" --update-env >/dev/null 2>&1 || pm2 start "$dir/ecosystem.config.js" >/dev/null 2>&1
  }

  # 2. Apply.
  old_req=$(md5sum "$dir/requirements.txt" 2>/dev/null | cut -d' ' -f1)
  (cd "$SRC" && tar -cf - $FILES) | tar -xf - -C "$dir"
  ok "files copied"
  new_req=$(md5sum "$dir/requirements.txt" 2>/dev/null | cut -d' ' -f1)
  if [ "$old_req" != "$new_req" ]; then
    echo "   installing changed dependencies..."
    if ! "$dir/.venv/bin/pip" install -q -r "$dir/requirements.txt"; then
      bad "dependency install failed"; rollback; RESULT[$dir]="ROLLED BACK (dependencies)"; continue
    fi
    ok "dependencies updated"
  fi

  # 3. Check before restarting.
  if ! check_files "$dir"; then
    bad "checks failed"; rollback; RESULT[$dir]="ROLLED BACK (checks failed)"; continue
  fi
  if ! (cd "$dir" && .venv/bin/python -c "import bot" >/dev/null 2>"$STAGE/import.err"); then
    bad "bot fails to load:"; tail -5 "$STAGE/import.err" | sed 's/^/      /'
    rollback; RESULT[$dir]="ROLLED BACK (load error)"; continue
  fi
  ok "bot loads"

  # 4. Restart and confirm it stays up.
  if pm2 describe "$name" >/dev/null 2>&1; then
    pm2 restart "$name" --update-env >/dev/null
  else
    pm2 start "$dir/ecosystem.config.js" >/dev/null
  fi
  echo "   restarted; watching for ${STABLE_SECONDS}s..."
  if wait_stable "$name"; then
    ok "$name is online and stable"
    RESULT[$dir]="UPDATED"
  else
    bad "$name crashed after the update. Last log lines:"
    pm2 logs "$name" --nostream --lines 15 2>/dev/null | tail -15 | sed 's/^/      /'
    rollback; RESULT[$dir]="ROLLED BACK (crashed after restart)"
  fi
  # Keep the 10 newest backups.
  ls -1t "$dir"/backups/before-update-*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f
done

pm2 save >/dev/null 2>&1

say "Summary"
status=0
for dir in "${BOTS[@]}"; do
  case "${RESULT[$dir]}" in
    UPDATED) ok "$dir: updated";;
    *)       bad "$dir: ${RESULT[$dir]}"; status=1;;
  esac
done
echo
echo "   Check any time with:  bash $(basename "$0") check"
exit $status
