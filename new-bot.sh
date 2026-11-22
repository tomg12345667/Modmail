#!/usr/bin/env bash
# Create a new Modmail bot on this server from the latest code on GitHub.
#
#   bash ~/modmail-src/new-bot.sh "TMTG Utilities"
#
# It makes ~/<name>-modmail (e.g. ~/tmtg-utilities-modmail), installs it, asks
# for the bot token / server ID / owner once, starts it in PM2, adds it to
# update-bots.sh so auto-deploy keeps it updated, and connects it to the web
# dashboard. Safe to run again for the same name: settings and data are kept.

set -euo pipefail

DISPLAY_NAME="${1:-}"
[ -n "$DISPLAY_NAME" ] || { echo 'Usage: bash new-bot.sh "Community Name"'; exit 1; }

SLUG=$(printf '%s' "$DISPLAY_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\+/-/g; s/^-//; s/-$//')
case "$SLUG" in *-modmail) ;; *) SLUG="$SLUG-modmail";; esac
SRC="${SRC:-$HOME/modmail-src}"
DIR="$HOME/$SLUG"
UPDATER="${UPDATER:-$HOME/update-bots.sh}"
DASH_DIR="${DASH_DIR:-$HOME/modmail-dashboard}"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$SRC/.git" ] || fail "$SRC not found. Run bash ~/auto-deploy.sh once first."
command -v pm2 >/dev/null || fail "PM2 is not installed."
python3 -c "import venv, ensurepip" 2>/dev/null || fail "python3-venv is missing. Run: sudo apt install -y python3-venv python3-pip"

say "Getting the latest code"
git -C "$SRC" fetch -q origin
REF=$(git -C "$SRC" rev-parse --abbrev-ref origin/HEAD 2>/dev/null || echo origin/master)

say "Setting up $DISPLAY_NAME in $DIR"
mkdir -p "$DIR"
# Code only: the dashboard and GitHub files aren't part of a bot. .env and data/ are never in git.
git -C "$SRC" archive "$REF" | tar -x -C "$DIR" --exclude='dashboard' --exclude='.github'
mkdir -p "$DIR/data"
sed -i "s/name: \"[^\"]*\"/name: \"$SLUG\"/" "$DIR/ecosystem.config.js"

cd "$DIR"
[ -x .venv/bin/python ] || python3 -m venv .venv
say "Installing packages (this can take a minute)"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ -f .env ]; then
  say "Keeping existing settings (.env)"
else
  say "Bot settings for $DISPLAY_NAME"
  read -rp "  Bot token: " TOKEN;                        [ -n "$TOKEN" ] || fail "A bot token is required."
  read -rp "  Server (guild) ID: " GUILD_ID;             [ -n "$GUILD_ID" ] || fail "A server ID is required."
  read -rp "  Owner user ID(s), comma-separated: " OWNERS; [ -n "$OWNERS" ] || fail "An owner ID is required."
  umask 077
  printf 'TOKEN=%s\nGUILD_ID=%s\nOWNERS=%s\n\nDATABASE_TYPE=json\nDISABLE_AUTOUPDATES=true\n' "$TOKEN" "$GUILD_ID" "$OWNERS" > .env
  umask 022
fi

# Keep it updated with the other bots.
if [ -f "$UPDATER" ] && ! grep -q "/$SLUG\"" "$UPDATER"; then
  sed -i "/^BOTS=(/a\\  \"\$HOME/$SLUG\"" "$UPDATER"
  say "Added to $UPDATER, so auto-deploy updates it too"
fi

# Connect to the web dashboard (adds its private port + secret to .env).
if [ -f "$DASH_DIR/setup_dashboard.py" ]; then
  say "Connecting to the web dashboard"
  (cd "$DASH_DIR" && python3 setup_dashboard.py)
  pm2 restart modmail-dashboard >/dev/null 2>&1 || true
fi

if pm2 describe "$SLUG" >/dev/null 2>&1; then
  pm2 restart "$SLUG" --update-env >/dev/null
else
  pm2 start ecosystem.config.js >/dev/null
fi
pm2 save >/dev/null

say "Done. $DISPLAY_NAME is running as PM2 process \"$SLUG\"."
echo "  Logs:      pm2 logs $SLUG"
echo "  Settings:  $DIR/.env"
