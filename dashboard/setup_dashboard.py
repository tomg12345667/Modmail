"""
Connect the dashboard to every Modmail bot on this server.

    python3 setup_dashboard.py

What it does:
  1. Finds every bot folder in your home folder (a folder with bot.py and .env).
  2. Gives each bot its own private port and a random secret, and adds them to
     that bot's .env (DASH_API_PORT / DASH_API_SECRET). Existing values are kept.
  3. Writes bots.json (the dashboard's list of bots).
  4. Creates the dashboard's .env, asking for the Discord app details once.

Run it again any time you add a bot. Then restart the bots (pm2 restart all).
"""

import json
import os
import re
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
FIRST_PORT = 8301


def read_env(path):
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def append_env(path, values):
    with open(path, "a", encoding="utf-8") as fp:
        fp.write("\n# Web dashboard (private, localhost only)\n")
        for k, v in values.items():
            fp.write(f"{k}={v}\n")


def pm2_name(folder):
    try:
        with open(os.path.join(folder, "ecosystem.config.js"), encoding="utf-8") as fp:
            m = re.search(r'name:\s*"([^"]+)"', fp.read())
            return m.group(1) if m else None
    except OSError:
        return None


def find_bots(base):
    found = []
    for name in sorted(os.listdir(base)):
        folder = os.path.join(base, name)
        if os.path.isfile(os.path.join(folder, "bot.py")) and os.path.isfile(os.path.join(folder, ".env")):
            if os.path.isfile(os.path.join(folder, "core", "dashapi.py")):
                found.append(folder)
            else:
                print(f"  ! {name}: skipped - this bot doesn't have the dashboard code yet. Update it first (auto-deploy or update-bots.sh).")
    return found


def ask(question, default=""):
    shown = f" [{default}]" if default else ""
    answer = input(f"{question}{shown}: ").strip()
    return answer or default


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else HOME
    print(f"Looking for Modmail bots in {base} ...")
    bots = find_bots(base)
    if not bots:
        sys.exit("No bots with the dashboard code found. Update your bots first, then run this again.")

    bots_file = os.path.join(HERE, "bots.json")
    existing = {b["id"]: b for b in json.load(open(bots_file))} if os.path.exists(bots_file) else {}
    used_ports = {int(read_env(os.path.join(f, ".env")).get("DASH_API_PORT", 0) or 0) for f in bots}
    next_port = FIRST_PORT
    entries = []
    for folder in bots:
        env_path = os.path.join(folder, ".env")
        env = read_env(env_path)
        bot_id = pm2_name(folder) or os.path.basename(folder)
        add = {}
        port = env.get("DASH_API_PORT")
        if not port:
            while next_port in used_ports:
                next_port += 1
            port = str(next_port)
            used_ports.add(next_port)
            add["DASH_API_PORT"] = port
        secret = env.get("DASH_API_SECRET")
        if not secret or len(secret) < 24:
            secret = secrets.token_urlsafe(32)
            add["DASH_API_SECRET"] = secret
        if add:
            append_env(env_path, add)
        # Leave the name empty to show the bot's real server name (e.g. "RingNet"); set one in bots.json to override it.
        name = existing.get(bot_id, {}).get("name", "")
        entries.append({"id": bot_id, "name": name, "url": f"http://127.0.0.1:{port}", "secret": secret})
        print(f"  + {bot_id:<26} port {port}  ({'new settings added' if add else 'already set up'})")

    with open(bots_file, "w", encoding="utf-8") as fp:
        json.dump(entries, fp, indent=2)
    os.chmod(bots_file, 0o600)
    print(f"\nSaved {len(entries)} bots to {bots_file}")

    dash_env = os.path.join(HERE, ".env")
    if not os.path.exists(dash_env):
        print("\nDashboard settings (from the Discord Developer Portal, see the setup steps):")
        client_id = ask("  Discord application ID")
        client_secret = ask("  Discord client secret")
        public_url = ask("  Dashboard address", "https://dash.ringnet.org").rstrip("/")
        owners = ask("  Your Discord user ID (sees offline bots too)")
        with open(dash_env, "w", encoding="utf-8") as fp:
            fp.write(
                f"DISCORD_CLIENT_ID={client_id}\nDISCORD_CLIENT_SECRET={client_secret}\nPUBLIC_URL={public_url}\n"
                f"SESSION_SECRET={secrets.token_urlsafe(48)}\nDASH_OWNERS={owners}\nHOST=127.0.0.1\nPORT=8200\n"
            )
        os.chmod(dash_env, 0o600)
        print(f"Saved {dash_env}")
    else:
        print(f"Kept your existing {dash_env}")

    names = " ".join(pm2_name(f) or os.path.basename(f) for f in bots)
    print("\nNext:")
    print(f"  pm2 restart {names}")
    print("  pm2 start ecosystem.config.js   (first time)  or  pm2 restart modmail-dashboard")


if __name__ == "__main__":
    main()
