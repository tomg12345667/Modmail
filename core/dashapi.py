"""
Private API that lets the central web dashboard manage this bot.

It only listens on 127.0.0.1 and every request must carry the shared secret,
so nothing outside the server can reach it. Each request also names the
Discord user who is signed in to the dashboard, and every action is checked
against that user's permissions using the bot's own rules (the same check
commands use, including `?perms override`).

Enable it in .env:
    DASH_API_PORT=8301
    DASH_API_SECRET=<long random string, the same one the dashboard uses>
"""

import asyncio
import hmac
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

import discord
from aiohttp import web

from core.checks import check_permissions
from core.models import DMDisabled, InvalidConfigError, PermissionLevel, getLogger

logger = getLogger(__name__)
_START = time.time()

# Dashboard area -> the command whose permission level guards it.
CAPABILITIES = {
    "settings": "config",
    "permissions": "permissions",
    "snippets": "snippet add",
    "aliases": "alias add",
    "autotriggers": "autotrigger add",
    "blocks": "block",
    "logs": "logs",
    "threadmenu": "threadmenu",
    "plugins": "plugins add",
    "dm": "enable",
    "verification": "verification setup",
    "msgcmd": "msgcmd create",
}

MENU_KEYS = ("thread_creation_menu_options", "thread_creation_menu_submenus")
MENU_PRIVATE = ("thread_creation_menu_enabled",)  # set via ?threadmenu, not ?config
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# A stand-in for a command context, so we can reuse the bot's own permission
# checks and run a few existing commands (plugins, enable/disable) as-is.
# --------------------------------------------------------------------------- #
class _CaptureChannel:
    def __init__(self, guild):
        self.guild = guild
        self.id = 0
        self.name = "dashboard"
        self.sent = []
        self.mention = "#dashboard"

    def permissions_for(self, member):
        perms = getattr(member, "guild_permissions", None)
        return perms if perms is not None else discord.Permissions.none()

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, "embed": kwargs.get("embed")})
        return _FakeMessage(self)

    @asynccontextmanager
    async def typing(self):
        yield


class _FakeMessage:
    def __init__(self, channel, author=None, content=""):
        self.channel = channel
        self.author = author
        self.content = content
        self.attachments = []
        self.id = 0
        self.guild = channel.guild

    async def delete(self, *a, **k):
        pass

    async def add_reaction(self, *a, **k):
        pass

    async def edit(self, *a, **k):
        pass


class DashContext:
    def __init__(self, bot, author, command=None):
        self.bot = bot
        self.author = author
        self.guild = getattr(author, "guild", None) or bot.modmail_guild
        self.channel = _CaptureChannel(self.guild)
        self.message = _FakeMessage(self.channel, author)
        self.command = command
        self.prefix = self.clean_prefix = bot.prefix
        self.thread = None

    async def send(self, content=None, **kwargs):
        return await self.channel.send(content, **kwargs)

    def typing(self):
        return self.channel.typing()

    async def trigger_typing(self):
        pass

    async def invoke(self, cmd, *args, **kwargs):
        return await cmd.callback(cmd.cog, self, *args, **kwargs)

    def replies(self):
        out = []
        for m in self.channel.sent:
            e = m["embed"]
            if e is not None:
                is_error = (e.title or "").lower() == "error" or (
                    e.color is not None and e.color.value == int(self.bot.error_color)
                )
                out.append({"title": e.title or "", "text": e.description or "", "error": is_error})
            elif m["content"]:
                out.append({"title": "", "text": str(m["content"]), "error": False})
        return out


class _UserStub:
    """A user who isn't in either server (only bot owners get anywhere with this)."""

    def __init__(self, uid):
        self.id = uid
        self.roles = []
        self.guild = None
        self.guild_permissions = discord.Permissions.none()


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #
class DashboardAPI:
    def __init__(self, bot):
        self.bot = bot
        self.runner = None
        self.secret = (bot.config["dash_api_secret"] or "").strip()

    # ---- server ----------------------------------------------------------- #
    async def start(self, port: int, host: str = "127.0.0.1"):
        if self.runner is not None:
            return
        if len(self.secret) < 24:
            logger.error("Dashboard API not started: DASH_API_SECRET must be at least 24 characters.")
            return
        app = web.Application(middlewares=[self._middleware], client_max_size=2 * 1024 * 1024)
        r = app.router
        r.add_get("/api/me", self.me)
        r.add_get("/api/overview", self.overview)
        r.add_get("/api/guild", self.guild_info)
        r.add_get("/api/members", self.members)
        r.add_get("/api/settings", self.settings_get)
        r.add_put("/api/settings/{key}", self.settings_put)
        r.add_delete("/api/settings/{key}", self.settings_delete)
        r.add_get("/api/permissions", self.perms_get)
        r.add_post("/api/permissions", self.perms_post)
        r.add_post("/api/overrides", self.overrides_post)
        for kind in ("snippets", "aliases", "autotriggers"):
            r.add_get(f"/api/{kind}", self.kv_get)
            r.add_put(f"/api/{kind}/{{name}}", self.kv_put)
            r.add_delete(f"/api/{kind}/{{name}}", self.kv_delete)
        r.add_get("/api/blocks", self.blocks_get)
        r.add_post("/api/blocks", self.blocks_post)
        r.add_delete("/api/blocks/{kind}/{id}", self.blocks_delete)
        r.add_get("/api/logs", self.logs_list)
        r.add_get("/api/logs/{key}", self.logs_get)
        r.add_get("/api/logs/{key}/grants", self.grants_get)
        r.add_post("/api/logs/{key}/grants", self.grants_post)
        r.add_delete("/api/logs/{key}/grants/{user}", self.grants_delete)
        r.add_get("/api/shared", self.shared_get)
        r.add_get("/api/threads", self.threads_get)
        r.add_get("/api/threadmenu", self.menu_get)
        r.add_put("/api/threadmenu", self.menu_put)
        r.add_get("/api/plugins", self.plugins_get)
        r.add_post("/api/plugins", self.plugins_post)
        r.add_post("/api/dm", self.dm_post)
        r.add_get("/api/verification", self.verif_get)
        r.add_put("/api/verification/{key}", self.verif_put)
        r.add_post("/api/verification/{action}", self.verif_action)
        r.add_get("/api/msgcmd", self.msgcmd_get)
        r.add_put("/api/msgcmd/{name}", self.msgcmd_put)
        r.add_delete("/api/msgcmd/{name}", self.msgcmd_delete)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()
        logger.info("Dashboard API listening on %s:%s", host, port)

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    @web.middleware
    async def _middleware(self, request, handler):
        given = request.headers.get("X-Dash-Secret", "")
        if not given or not hmac.compare_digest(given, self.secret):
            return web.json_response({"error": "Not allowed."}, status=401)
        uid = request.headers.get("X-Dash-User", "")
        if not uid.isdigit():
            return web.json_response({"error": "No user."}, status=401)
        request["uid"] = int(uid)
        try:
            return await handler(request)
        except ApiError as e:
            return web.json_response({"error": str(e)}, status=e.status)
        except InvalidConfigError as e:
            return web.json_response({"error": str(e)}, status=400)
        except web.HTTPException:
            raise
        except Exception as e:  # keep the bot alive whatever happens
            logger.error("Dashboard API error on %s %s", request.method, request.path, exc_info=True)
            return web.json_response({"error": f"Something went wrong: {type(e).__name__}"}, status=500)

    # ---- identity + permissions ------------------------------------------ #
    def _member(self, uid: int):
        for guild in (self.bot.modmail_guild, self.bot.guild):
            if guild is not None:
                m = guild.get_member(uid)
                if m is not None:
                    return m
        return _UserStub(uid)

    async def allowed(self, uid: int, cap: str) -> bool:
        command_name = CAPABILITIES[cap]
        if self.bot.get_command(command_name) is None:
            return False  # plugin not installed (or command missing): never fall through to "allowed"
        member = self._member(uid)
        if await self.bot.is_owner(member):
            return True
        if isinstance(member, _UserStub):
            return False
        ctx = DashContext(self.bot, member)
        return await check_permissions(ctx, command_name)

    async def need(self, request, cap: str):
        if not await self.allowed(request["uid"], cap):
            raise ApiError("You don't have permission for this.", 403)

    async def level_name(self, uid: int) -> str:
        member = self._member(uid)
        if await self.bot.is_owner(member):
            return "OWNER"
        if isinstance(member, _UserStub):
            return "NONE"
        if member.guild == self.bot.modmail_guild and member.guild_permissions.administrator:
            best = PermissionLevel.ADMINISTRATOR
        else:
            best = None
        checks = {str(member.id), *(str(r.id) for r in member.roles)}
        levels = self.bot.config["level_permissions"]
        for level in sorted(PermissionLevel, reverse=True):
            if level in (PermissionLevel.INVALID,):
                continue
            ids = levels.get(level.name, [])
            if -1 in ids or checks & {str(i) for i in ids}:
                if best is None or level > best:
                    best = level
                break
        return best.name if best is not None else "NONE"

    async def me(self, request):
        uid = request["uid"]
        caps = {cap: await self.allowed(uid, cap) for cap in CAPABILITIES}
        shared = len(await self._active_grants(user_id=uid))
        return web.json_response(
            {"level": await self.level_name(uid), "can": caps, "shared": shared, "bot": self._bot_card()}
        )

    # ---- temporary log access ("Share this log") ------------------------- #
    @property
    def _grants(self):
        return self.bot.api.db["dashboard_grants"]

    async def _active_grants(self, *, user_id=None, key=None):
        query = {}
        if user_id is not None:
            query["user_id"] = str(user_id)
        if key is not None:
            query["key"] = key
        now = discord.utils.utcnow().isoformat()
        out = []
        for g in await self._grants.find(query).to_list(None):
            if str(g.get("expires", "")) > now:
                out.append(g)
            else:
                await self._grants.delete_one({"_id": g["_id"]})  # tidy up expired access
        return out

    async def _can_read_log(self, uid, key):
        if await self.allowed(uid, "logs"):
            return True
        return bool(await self._active_grants(user_id=uid, key=key))

    async def grants_get(self, request):
        await self.need(request, "logs")
        key = request.match_info["key"]
        rows = []
        for g in await self._active_grants(key=key):
            rows.append({**self._name_for(g["user_id"]), "expires": g["expires"], "granted_by": g.get("granted_by")})
        return web.json_response(rows)

    async def grants_post(self, request):
        await self.need(request, "logs")
        key = request.match_info["key"]
        if not await self.bot.api.logs.find_one({"key": key, "guild_id": str(self.bot.guild_id)}):
            raise ApiError("Log not found.", 404)
        body = await request.json()
        target = str(body.get("user_id", "")).strip()
        if not target.isdigit() or not 15 <= len(target) <= 20:
            raise ApiError("Enter a Discord user ID (15-20 digits).")
        try:
            hours = int(body.get("hours", 24))
        except (TypeError, ValueError):
            raise ApiError("Pick how long they can see it.")
        if not 1 <= hours <= 720:
            raise ApiError("Access can last from 1 hour to 30 days.")
        from datetime import timedelta

        who = self._member(request["uid"])
        expires = (discord.utils.utcnow() + timedelta(hours=hours)).isoformat()
        await self._grants.replace_one(
            {"_id": f"{key}:{target}"},
            {"_id": f"{key}:{target}", "key": key, "user_id": target, "expires": expires,
             "granted_by": getattr(who, "name", str(request["uid"]))},
            upsert=True,
        )
        return web.json_response({"ok": True, "expires": expires, **self._name_for(target)})

    async def grants_delete(self, request):
        await self.need(request, "logs")
        res = await self._grants.delete_one({"_id": f"{request.match_info['key']}:{request.match_info['user']}"})
        if not res.deleted_count:
            raise ApiError("They didn't have access.", 404)
        return web.json_response({"ok": True})

    async def shared_get(self, request):
        out = []
        for g in await self._active_grants(user_id=request["uid"]):
            doc = await self.bot.api.logs.find_one({"key": g["key"], "guild_id": str(self.bot.guild_id)})
            if doc:
                out.append({**self._log_card(doc), "expires": g["expires"], "granted_by": g.get("granted_by")})
        return web.json_response(out)

    def _bot_card(self):
        user = self.bot.user
        g = self.bot.guild
        return {
            "name": g.name if g else (user.name if user else "Modmail"),
            "bot_name": user.name if user else "",
            "avatar": str(user.display_avatar.url) if user else None,
            "icon": str(g.icon.url) if g and g.icon else None,
            "id": str(user.id) if user else "",
        }

    # ---- overview + guild data ------------------------------------------- #
    async def overview(self, request):
        if not any([await self.allowed(request["uid"], c) for c in CAPABILITIES]):
            raise ApiError("You don't have access to this bot.", 403)
        bot = self.bot
        mg = bot.modmail_guild
        return web.json_response(
            {
                **self._bot_card(),
                "version": str(bot.version),
                "latency_ms": round(bot.latency * 1000) if bot.latency == bot.latency else None,
                "uptime_s": int(time.time() - _START),
                "open_threads": len([t for t in bot.threads.cache.values() if getattr(t, "channel", None)]),
                "plugins": len(bot.config["plugins"]),
                "dm_disabled": DMDisabled(bot.config["dm_disabled"]).name,
                "main_guild": bot.guild.name if bot.guild else None,
                "staff_guild": mg.name if mg and mg != bot.guild else None,
                "level": await self.level_name(request["uid"]),
            }
        )

    async def guild_info(self, request):
        if not any([await self.allowed(request["uid"], c) for c in ("permissions", "settings", "verification", "msgcmd", "threadmenu", "blocks")]):
            raise ApiError("You don't have permission for this.", 403)
        out = {}
        for label, g in (("staff", self.bot.modmail_guild), ("main", self.bot.guild)):
            if g is None or (label == "main" and g == self.bot.modmail_guild):
                continue
            out[label] = {
                "id": str(g.id),
                "name": g.name,
                "roles": [
                    {"id": str(r.id), "name": r.name, "color": f"#{r.color.value:06x}" if r.color.value else None}
                    for r in sorted(g.roles, key=lambda r: -r.position)
                    if not r.is_default()
                ],
                "channels": [
                    {"id": str(c.id), "name": c.name, "category": c.category.name if c.category else None}
                    for c in g.text_channels
                ],
                "categories": [{"id": str(c.id), "name": c.name} for c in g.categories],
            }
        return web.json_response(out)

    async def members(self, request):
        if not any([await self.allowed(request["uid"], c) for c in ("permissions", "blocks")]):
            raise ApiError("You don't have permission for this.", 403)
        q = request.query.get("q", "").strip().lower()
        seen, out = set(), []
        for g in (self.bot.modmail_guild, self.bot.guild):
            if g is None:
                continue
            for m in g.members:
                if m.id in seen or m.bot:
                    continue
                if not q or q in m.name.lower() or q in (m.display_name or "").lower() or q == str(m.id):
                    seen.add(m.id)
                    out.append({"id": str(m.id), "name": m.name, "display": m.display_name, "avatar": str(m.display_avatar.url)})
                    if len(out) >= 25:
                        return web.json_response(out)
        return web.json_response(out)

    def _name_for(self, sid: str) -> dict:
        if sid in ("-1", -1):
            return {"id": "-1", "name": "@everyone", "type": "everyone"}
        try:
            i = int(sid)
        except (TypeError, ValueError):
            return {"id": str(sid), "name": str(sid), "type": "unknown"}
        for g in (self.bot.modmail_guild, self.bot.guild):
            if g is None:
                continue
            r = g.get_role(i)
            if r is not None:
                return {"id": str(i), "name": f"@{r.name}", "type": "role"}
            m = g.get_member(i)
            if m is not None:
                return {"id": str(i), "name": m.name, "type": "user"}
        u = self.bot.get_user(i)
        return {"id": str(i), "name": u.name if u else f"Unknown ({i})", "type": "user" if u else "unknown"}

    # ---- settings -------------------------------------------------------- #
    def _help(self):
        if not hasattr(self, "_help_cache"):
            path = os.path.join(os.path.dirname(__file__), "config_help.json")
            try:
                with open(path, encoding="utf-8") as fp:
                    self._help_cache = json.load(fp)
            except OSError:
                self._help_cache = {}
        return self._help_cache

    def _kind(self, key):
        c = self.bot.config
        if key in c.booleans:
            return "boolean"
        if key in c.colors:
            return "color"
        if key in c.time_deltas or key in getattr(c, "duration_seconds", ()):
            return "duration"
        if key in getattr(c, "integers", ()):
            return "integer"
        if key in c.enums:
            return "choice"
        default = c.public_keys.get(key)
        if isinstance(default, (dict, list)):
            return "json"
        return "text"

    def _value(self, key):
        c = self.bot.config
        if key in c.booleans:
            return bool(c.get(key))
        if key in getattr(c, "integers", ()) or key in getattr(c, "duration_seconds", ()):
            return self._serial(c.get(key))
        return self._serial(c.get(key, convert=False))

    def _serial(self, v):
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, (dict, list)):
            return json.loads(json.dumps(v, default=str))
        return str(v)

    async def settings_get(self, request):
        await self.need(request, "settings")
        c = self.bot.config
        help_ = self._help()
        items = []
        for key in sorted(c.public_keys):
            if key in ("snippets", "aliases", "auto_triggers", "plugins") or key.startswith("thread_creation_menu_options") or key == "thread_creation_menu_submenus":
                continue  # managed on their own pages
            h = help_.get(key, {})
            kind = self._kind(key)
            item = {
                "key": key,
                "value": self._value(key),
                "default": self._serial(c.public_keys.get(key)),
                "kind": kind,
                "description": h.get("description", ""),
                "notes": h.get("notes", []),
            }
            if kind == "choice":
                item["choices"] = [e.name.lower() for e in c.enums[key]]
            items.append(item)
        return web.json_response(items)

    async def settings_put(self, request):
        await self.need(request, "settings")
        key = request.match_info["key"].lower()
        c = self.bot.config
        if key not in c.public_keys:
            raise ApiError(f"`{key}` isn't a setting you can change.")
        body = await request.json()
        value = body.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ApiError("Enter a value, or use Reset to go back to the default.")
        if isinstance(value, (dict, list)):
            c[key] = value
            await c.update()
        else:
            from core.guildprofile import KEYS as PROFILE_KEYS, apply_guild_profile

            if key in PROFILE_KEYS:
                previous = c.get(key, convert=False)
                await c.set(key, str(value))
                try:
                    status = await apply_guild_profile(self.bot, key)
                except Exception as e:
                    if previous is None:
                        c.remove(key)
                    else:
                        await c.set(key, previous)
                    raise ApiError(str(e))
                await c.update()
                return web.json_response({"ok": True, "message": status, "value": self._value(key)})
            await c.set(key, str(value) if not isinstance(value, bool) else ("yes" if value else "no"))
            await c.update()
        return web.json_response({"ok": True, "value": self._value(key)})

    async def settings_delete(self, request):
        await self.need(request, "settings")
        key = request.match_info["key"].lower()
        c = self.bot.config
        if key not in c.public_keys:
            raise ApiError(f"`{key}` isn't a setting you can change.")
        c.remove(key)
        await c.update()
        message = None
        from core.guildprofile import KEYS as PROFILE_KEYS, apply_guild_profile

        if key in PROFILE_KEYS:
            try:
                message = await apply_guild_profile(self.bot, key)
            except Exception as e:
                message = f"Reset, but Discord didn't accept the change: {e}"
        return web.json_response({"ok": True, "message": message, "value": self._value(key)})

    # ---- permissions ----------------------------------------------------- #
    async def perms_get(self, request):
        await self.need(request, "permissions")
        c = self.bot.config
        levels = {
            lvl.name: [self._name_for(x) for x in c["level_permissions"].get(lvl.name, [])]
            for lvl in sorted(PermissionLevel, reverse=True)
            if lvl not in (PermissionLevel.INVALID, PermissionLevel.REGULAR)
        }
        commands = {name: [self._name_for(x) for x in ids] for name, ids in c["command_permissions"].items() if ids}
        overrides = dict(c["override_command_level"])
        all_cmds = sorted({cmd.qualified_name for cmd in self.bot.walk_commands()})
        return web.json_response({"levels": levels, "commands": commands, "overrides": overrides, "all_commands": all_cmds})

    async def perms_post(self, request):
        await self.need(request, "permissions")
        body = await request.json()
        kind, name, target, add = body.get("kind"), str(body.get("name", "")), str(body.get("target", "")), bool(body.get("add", True))
        target_val = -1 if target in ("-1", "everyone", "@everyone") else target
        if target_val != -1 and not target.isdigit():
            raise ApiError("Pick a role or a member.")
        if kind == "level":
            try:
                level = PermissionLevel[name.upper()]
            except KeyError:
                raise ApiError("Unknown level.")
            await self.bot.update_perms(level, target_val, add=add)
        elif kind == "command":
            cmd = self.bot.get_command(name)
            if cmd is None:
                raise ApiError(f"There's no command called `{name}`.")
            await self.bot.update_perms(cmd.qualified_name, target_val, add=add)
        else:
            raise ApiError("Unknown permission type.")
        return web.json_response({"ok": True})

    async def overrides_post(self, request):
        await self.need(request, "permissions")
        body = await request.json()
        name, level = str(body.get("command", "")).strip(), body.get("level")
        cmd = self.bot.get_command(name)
        if cmd is None:
            raise ApiError(f"There's no command called `{name}`.")
        c = self.bot.config
        if level in (None, "", "default"):
            c["override_command_level"].pop(cmd.qualified_name, None)
        else:
            try:
                lvl = PermissionLevel[str(level).upper()]
            except KeyError:
                raise ApiError("Unknown level.")
            c["override_command_level"][cmd.qualified_name] = lvl.name
        await c.update()
        return web.json_response({"ok": True})

    # ---- snippets / aliases / autotriggers ------------------------------ #
    _KV = {"snippets": ("snippets", "snippets"), "aliases": ("aliases", "aliases"), "autotriggers": ("auto_triggers", "autotriggers")}

    def _kv_kind(self, request):
        return request.path.split("/")[2]

    async def kv_get(self, request):
        kind = self._kv_kind(request)
        await self.need(request, kind)
        key, _ = self._KV[kind]
        return web.json_response(dict(sorted(self.bot.config[key].items())))

    async def kv_put(self, request):
        kind = self._kv_kind(request)
        await self.need(request, kind)
        key, _ = self._KV[kind]
        name = request.match_info["name"].strip()
        body = await request.json()
        value = str(body.get("value", "")).strip()
        if not value:
            raise ApiError("The text can't be empty.")
        if kind in ("snippets", "aliases"):
            name = name.lower()
            if not re.fullmatch(r"[a-z0-9_\-]{1,100}", name):
                raise ApiError("Names can only use letters, numbers, - and _ (no spaces).")
            if self.bot.get_command(name) is not None:
                raise ApiError(f"`{name}` is already a command.")
            other = "aliases" if kind == "snippets" else "snippets"
            if name in self.bot.config[other]:
                raise ApiError(f"`{name}` is already one of your {other}.")
            if len(value) > 4000:
                raise ApiError("Keep it under 4000 characters.")
        if kind in ("aliases", "autotriggers"):
            first = value.split()[0].lower()
            if self.bot.get_command(first) is None and first not in self.bot.config["aliases"] and first not in self.bot.config["snippets"]:
                raise ApiError(f"`{first}` isn't a command, alias or snippet.")
        if kind == "autotriggers" and len(name) > 100:
            raise ApiError("Keep the keyword under 100 characters.")
        self.bot.config[key][name] = value
        await self.bot.config.update()
        return web.json_response({"ok": True})

    async def kv_delete(self, request):
        kind = self._kv_kind(request)
        await self.need(request, kind)
        key, _ = self._KV[kind]
        name = request.match_info["name"]
        if self.bot.config[key].pop(name, None) is None and self.bot.config[key].pop(name.lower(), None) is None:
            raise ApiError("That doesn't exist.", 404)
        await self.bot.config.update()
        return web.json_response({"ok": True})

    # ---- blocks ---------------------------------------------------------- #
    async def blocks_get(self, request):
        await self.need(request, "blocks")
        c = self.bot.config
        users = [{**self._name_for(k), "reason": v or ""} for k, v in c["blocked"].items()]
        roles = [{**self._name_for(k), "reason": v or ""} for k, v in c["blocked_roles"].items()]
        return web.json_response({"users": users, "roles": roles})

    async def blocks_post(self, request):
        await self.need(request, "blocks")
        body = await request.json()
        kind, target = body.get("kind", "user"), str(body.get("target", "")).strip()
        reason = str(body.get("reason", "")).strip()
        if not target.isdigit():
            raise ApiError("Pick a member or role, or enter an ID.")
        if await self.bot.is_owner(_UserStub(int(target))):
            raise ApiError("Bot owners can't be blocked.")
        who = self._member(request["uid"])
        reason = f"by {getattr(who, 'name', 'dashboard')}: {reason}" if reason else f"by {getattr(who, 'name', 'dashboard')}"
        key = "blocked_roles" if kind == "role" else "blocked"
        self.bot.config[key][target] = reason
        await self.bot.config.update()
        return web.json_response({"ok": True})

    async def blocks_delete(self, request):
        await self.need(request, "blocks")
        key = "blocked_roles" if request.match_info["kind"] == "role" else "blocked"
        if self.bot.config[key].pop(request.match_info["id"], None) is None:
            raise ApiError("Not blocked.", 404)
        await self.bot.config.update()
        return web.json_response({"ok": True})

    # ---- logs + open tickets -------------------------------------------- #
    @staticmethod
    def _log_card(doc):
        r, cl = doc.get("recipient") or {}, doc.get("closer") or {}
        return {
            "key": doc.get("key"),
            "open": doc.get("open", False),
            "title": doc.get("title"),
            "created_at": str(doc.get("created_at") or ""),
            "closed_at": str(doc.get("closed_at") or ""),
            "recipient": {"id": str(r.get("id", "")), "name": r.get("name", "Unknown"), "avatar": r.get("avatar_url")},
            "closer": {"id": str(cl.get("id", "")), "name": cl.get("name")} if cl else None,
            "messages": doc.get("message_count", len(doc.get("messages") or [])),
        }

    async def logs_list(self, request):
        await self.need(request, "logs")
        api = self.bot.api
        q = request.query.get("q", "").strip()
        page = max(0, int(request.query.get("page", "0") or 0))
        per = 25
        guild = str(self.bot.guild_id)
        if q:
            found = {}
            ors = [{"key": q}, {"recipient.id": q}, {"recipient.name": {"$regex": re.escape(q), "$options": "i"}}]
            for d in await api.logs.find({"guild_id": guild, "$or": ors}).sort("created_at", -1).to_list(200):
                found[d["key"]] = d
            try:
                for d in await api.search_by_text(q, 200):
                    found.setdefault(d["key"], d)
            except Exception:
                pass
            docs = sorted(found.values(), key=lambda d: str(d.get("created_at")), reverse=True)
            total = len(docs)
            docs = docs[page * per : (page + 1) * per]
        else:
            total = await api.logs.count_documents({"guild_id": guild})
            docs = await api.logs.find({"guild_id": guild}).sort("created_at", -1).skip(page * per).limit(per).to_list(per)
        return web.json_response({"total": total, "page": page, "per_page": per, "logs": [self._log_card(d) for d in docs]})

    async def logs_get(self, request):
        if not await self._can_read_log(request["uid"], request.match_info["key"]):
            raise ApiError("You don't have permission for this.", 403)
        doc = await self.bot.api.logs.find_one({"key": request.match_info["key"], "guild_id": str(self.bot.guild_id)})
        if not doc:
            raise ApiError("Log not found.", 404)
        card = self._log_card(doc)
        card["close_message"] = doc.get("close_message")
        staff = await self.allowed(request["uid"], "logs")
        card["shared_view"] = not staff
        messages = doc.get("messages") or []
        if not staff:
            # People a log is shared with see what the member saw: no staff notes.
            messages = [m for m in messages if m.get("type") not in ("note", "internal", "persistent_note")]
        card["messages"] = [
            {
                "author": {
                    "name": (m.get("author") or {}).get("name", "Unknown"),
                    "avatar": (m.get("author") or {}).get("avatar_url"),
                    "mod": bool((m.get("author") or {}).get("mod")),
                },
                "content": m.get("content") or "",
                "type": m.get("type", "thread_message"),
                "timestamp": str(m.get("timestamp") or ""),
                "edited": bool(m.get("edited")),
                "attachments": [
                    {"url": a.get("url"), "filename": a.get("filename"), "is_image": bool(a.get("is_image"))}
                    for a in (m.get("attachments") or [])
                ],
            }
            for m in messages
        ]
        return web.json_response(card)

    async def threads_get(self, request):
        await self.need(request, "logs")
        out = []
        for t in list(self.bot.threads.cache.values()):
            ch = getattr(t, "channel", None)
            r = getattr(t, "recipient", None)
            if ch is None or r is None:
                continue
            out.append(
                {
                    "user": {"id": str(r.id), "name": getattr(r, "name", str(r.id))},
                    "channel": {"id": str(ch.id), "name": getattr(ch, "name", ""), "url": getattr(ch, "jump_url", None)},
                    "created_at": str(getattr(ch, "created_at", "") or ""),
                    "snoozed": bool(getattr(t, "snoozed", False)),
                }
            )
        out.sort(key=lambda x: x["created_at"], reverse=True)
        return web.json_response(out)

    # ---- thread menu ----------------------------------------------------- #
    async def menu_get(self, request):
        await self.need(request, "threadmenu")
        c = self.bot.config
        keys = sorted({*(k for k in c.public_keys if k.startswith("thread_creation_menu_")), *MENU_KEYS, *MENU_PRIVATE})
        return web.json_response({k: self._value(k) for k in keys})

    @staticmethod
    def _check_options(opts, where, submenus, allow_submenu=True):
        if not isinstance(opts, dict):
            raise ApiError(f"{where} is not a list of options.")
        if len(opts) > (25 if allow_submenu else 24):
            raise ApiError(f"{where} has too many options (max {25 if allow_submenu else 24}).")
        for key, o in opts.items():
            if not isinstance(o, dict):
                raise ApiError(f"{where}: option `{key}` is broken.")
            label = str(o.get("label") or "").strip()
            if not label or len(label) > 100:
                raise ApiError(f"{where}: every option needs a label (max 100 characters).")
            if o.get("description") and len(str(o["description"])) > 100:
                raise ApiError(f"{where}: the description for '{label}' is over 100 characters.")
            t = o.get("type")
            if t not in ("command", "submenu"):
                raise ApiError(f"{where}: '{label}' must be a command or a submenu.")
            if t == "submenu" and str(o.get("callback")) not in submenus:
                raise ApiError(f"{where}: '{label}' opens submenu `{o.get('callback')}`, which doesn't exist.")
            if t == "command" and not str(o.get("callback") or "").strip():
                raise ApiError(f"{where}: '{label}' needs a command to run.")

    async def menu_put(self, request):
        await self.need(request, "threadmenu")
        body = await request.json()
        c = self.bot.config
        options = body.get("thread_creation_menu_options", c["thread_creation_menu_options"])
        submenus = body.get("thread_creation_menu_submenus", c["thread_creation_menu_submenus"])
        if not isinstance(submenus, dict):
            raise ApiError("Submenus are broken.")
        self._check_options(options, "Main menu", submenus)
        for name, sub in submenus.items():
            self._check_options(sub, f"Submenu `{name}`", submenus)
        c["thread_creation_menu_options"] = options
        c["thread_creation_menu_submenus"] = submenus
        for k, v in body.items():
            if k in MENU_PRIVATE:
                c[k] = bool(v)
                continue
            if k in MENU_KEYS or not k.startswith("thread_creation_menu_") or k not in c.public_keys:
                continue
            if v is None:
                c.remove(k)
            else:
                await c.set(k, ("yes" if v else "no") if isinstance(v, bool) else str(v))
        await c.update()
        return web.json_response({"ok": True})

    # ---- plugins ---------------------------------------------------------- #
    async def plugins_get(self, request):
        await self.need(request, "plugins")
        cog = self.bot.get_cog("Plugins")
        registry = getattr(cog, "registry", {}) or {}
        installed = list(self.bot.config["plugins"])
        loaded = {str(p) for p in getattr(cog, "loaded_plugins", set())}
        return web.json_response(
            {
                "installed": [{"name": p, "loaded": p in loaded} for p in installed],
                "registry": [
                    {"name": k, "title": v.get("title", k), "description": v.get("description", ""), "repository": v.get("repository")}
                    for k, v in sorted(registry.items())
                ],
            }
        )

    async def plugins_post(self, request):
        await self.need(request, "plugins")
        body = await request.json()
        action, name = body.get("action"), str(body.get("name", "")).strip()
        cmd = {"add": "plugins add", "remove": "plugins remove", "update": "plugins update"}.get(action)
        if cmd is None or not name:
            raise ApiError("Choose add, remove or update and a plugin.")
        replies = await self._run(request["uid"], cmd, plugin_name=name)
        return web.json_response({"ok": not any(r["error"] for r in replies), "replies": replies})

    async def dm_post(self, request):
        await self.need(request, "dm")
        mode = (await request.json()).get("mode")
        cmd = {"enable": "enable", "disable_new": "disable new", "disable_all": "disable all"}.get(mode)
        if cmd is None:
            raise ApiError("Unknown mode.")
        replies = await self._run(request["uid"], cmd)
        return web.json_response({"ok": True, "replies": replies, "dm_disabled": DMDisabled(self.bot.config["dm_disabled"]).name})

    async def _run(self, uid, command_name, *args, guild=None, **kwargs):
        cmd = self.bot.get_command(command_name)
        if cmd is None:
            raise ApiError(f"`{command_name}` isn't available on this bot.")
        ctx = DashContext(self.bot, self._member(uid), command=cmd)
        if guild is not None:
            ctx.guild = ctx.channel.guild = guild
        await cmd.callback(cmd.cog, ctx, *args, **kwargs)
        return ctx.replies()

    # ---- verification plugin --------------------------------------------- #
    def _verif(self):
        cog = self.bot.get_cog("Verification")
        if cog is None:
            raise ApiError("The verification plugin isn't installed on this bot.", 404)
        return cog

    async def verif_get(self, request):
        await self.need(request, "verification")
        cog = self._verif()
        await cog._ready.wait()
        mod = sys.modules[type(cog).__module__]
        return web.json_response(
            {
                "settings": self._serial(cog.settings),
                "help": getattr(mod, "SETTING_HELP", {}),
                "pending": await cog.db.count_documents({"type": "pending"}),
                "alias_installed": self.bot.config["aliases"].get("verifyprompt") is not None,
            }
        )

    async def verif_put(self, request):
        await self.need(request, "verification")
        cog = self._verif()
        await cog._ready.wait()
        key = request.match_info["key"]
        value = str((await request.json()).get("value", ""))
        replies = await self._run(request["uid"], "verification set", key, value=value or "none", guild=self.bot.guild)
        err = next((r["text"] for r in replies if r["error"]), None)
        if err:
            raise ApiError(err)
        return web.json_response({"ok": True, "settings": self._serial(cog.settings)})

    async def verif_action(self, request):
        await self.need(request, "verification")
        action = request.match_info["action"]
        if action not in ("enable", "disable"):
            raise ApiError("Unknown action.")
        replies = await self._run(request["uid"], f"verification {action}")
        err = next((r["text"] for r in replies if r["error"]), None)
        if err:
            raise ApiError(err)
        return web.json_response({"ok": True, "settings": self._serial(self._verif().settings)})

    # ---- message commands plugin ----------------------------------------- #
    def _msgcmd(self):
        cog = self.bot.get_cog("Message Commands")
        if cog is None:
            raise ApiError("The message commands plugin isn't installed on this bot.", 404)
        return cog

    async def msgcmd_get(self, request):
        await self.need(request, "msgcmd")
        cog = self._msgcmd()
        return web.json_response([self._serial(d) for _, d in sorted(cog.data.items())])

    async def msgcmd_put(self, request):
        await self.need(request, "msgcmd")
        cog = self._msgcmd()
        mod = sys.modules[type(cog).__module__]
        name = request.match_info["name"].lower().lstrip("?")
        body = await request.json()
        old = cog.data.get(name)
        if old is None:
            problem = cog._name_problem(name)
            if problem:
                raise ApiError(problem)
        text = str(body.get("text", "")).strip()
        if not text or len(text) > 1900:
            raise ApiError("The message must be 1-1900 characters.")
        found = mod.find_variables(text)
        questions = {v["name"]: str(v.get("question") or "").strip() for v in body.get("variables") or [] if isinstance(v, dict)}
        variables = [{"name": n, "question": questions.get(n) or f"What should `{{{n}}}` be?"} for n in found]
        dest = str(body.get("destination", "here"))
        if dest not in ("here", "reply", "anonreply"):
            if not dest.isdigit() or self.bot.get_channel(int(dest)) is None:
                raise ApiError("Pick where the message is sent.")
        level = str(body.get("level", "supporter")).lower()
        if level not in ("supporter", "moderator", "administrator", "owner"):
            raise ApiError("Pick who can use it.")
        doc = {"_id": f"msgcmd-{name}", "type": "msgcmd", "name": name, "text": text, "variables": variables, "destination": dest, "level": level}
        await cog.db.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        cog.data[name] = doc
        cog._register(doc)
        return web.json_response({"ok": True, "command": doc})

    async def msgcmd_delete(self, request):
        await self.need(request, "msgcmd")
        cog = self._msgcmd()
        name = request.match_info["name"].lower()
        if name not in cog.data:
            raise ApiError("No message command with that name.", 404)
        await cog.db.delete_one({"_id": f"msgcmd-{name}"})
        cog.data.pop(name, None)
        cog._unregister(name)
        return web.json_response({"ok": True})
