# Modmail — JSON database + PM2 edition

This is the official Modmail bot with one change: **everything the bot stores
(logs, config, notes, snippets, plugin data) is saved as JSON files** instead of
MongoDB. No database server is needed. It runs under **PM2**.

Logs are delivered as **.txt transcripts** directly in Discord, so there is no
logviewer website to host.

## What's different from upstream

| | Upstream | This edition |
|---|---|---|
| Storage | MongoDB | JSON files in `./data` (one folder per collection, one file per document) |
| `?logs @user` | Embeds with web links | Same embeds + a `.txt` with the user's full log history |
| `?logs key <key>` | Embed with link | Same + that log as `.txt` |
| `?loglink` | Link | The current thread's transcript as `.txt` |
| Thread closed (log channel) | Link | Same message with the transcript attached |
| Hosting | Bot + MongoDB + logviewer | Bot only |

All other commands, settings and plugins work exactly as before. Plugins that
use the database get the same API (`find_one`, `update_one`, `upsert`, etc.).

## Requirements

- Python 3.10 – 3.13
- Node.js + PM2 (`npm install -g pm2`)
- git

## Install

```bash
cd modmail
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
cp .env.example .env
nano .env                                     # fill in TOKEN, GUILD_ID, OWNERS
```

In the Discord Developer Portal, turn on the **Server Members** and
**Message Content** intents for the bot (same as normal Modmail).

## Run with PM2

```bash
pm2 start ecosystem.config.js
pm2 save               # remember it
pm2 startup            # run the printed command so it starts on reboot
```

Everyday commands:

```bash
pm2 logs modmail       # live console output
pm2 restart modmail
pm2 stop modmail
```

`?restart` inside Discord also works; PM2 restarts the bot automatically.

## Backups

All data is in the `data/` folder (or `DATA_DIR` if you set one). Back it up by
copying that folder, e.g.:

```bash
tar czf modmail-backup-$(date +%F).tar.gz data/
```

## Important

- Keep `DISABLE_AUTOUPDATES=true` in `.env`. Auto-update pulls the official
  code from GitHub and would remove the JSON support. Don't run `?update`.
- Only run **one** copy of the bot against the same `data/` folder.

## Optional

- **Web log pages:** set `LOG_VIEWER_PORT=8000` and `LOG_URL=http://your-ip:8000`
  to also serve logs as web pages; links then appear alongside the `.txt` files.
- **Moving from an existing MongoDB:** with the bot stopped, run
  `.venv/bin/python tools/mongo_to_json.py "<your mongodb uri>"`.
