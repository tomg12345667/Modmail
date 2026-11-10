"""
Verification plugin for Modmail.

New members DM the bot a keyword (default "verify"). A Modmail ticket opens,
the bot replies with your verification instructions and shows staff an info
card, and staff run ?verify in the ticket to give the member their role.

Owner setup:
    ?verification setup              interactive setup wizard
    ?verification show               show current settings
    ?verification set <key> <value>  change one setting
    ?verification enable / disable   turn joins handling on or off
    ?verification reset              remove all settings, the alias and autotrigger

Staff:
    ?verify [member]       verify a member (in a ticket, defaults to the ticket's user)
    ?unverify <member>     take verification away
    ?verification pending  list members waiting to verify
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from core import checks
from core.models import PermissionLevel, getLogger

logger = getLogger(__name__)

ALIAS_NAME = "verifyprompt"
PROMPT_COMMAND = "verification prompt"

DEFAULTS = {
    "enabled": True,
    "verified_role": None,  # role ID
    "unverified_role": None,  # role ID or None
    "auto_role": None,  # role ID or None, given to every new joiner
    "join_dm": "Welcome to **{server}**! To get access, reply **{keyword}** to this message and a staff member will verify you.",
    "keyword": "verify",
    "prompt_message": "Thanks {user}! A staff member will verify you shortly. Please tell us how you found the server.",
    "verified_message": "You've been verified in **{server}**. Welcome!",
    "staff_role": None,  # role ID pinged when someone asks to verify
    "log_channel": None,  # channel ID
    "kick_after": 0,  # seconds; 0 = never
    "kick_message": "You were removed from **{server}** because you didn't verify in time. You're welcome to rejoin and try again.",
    "min_account_age": 0,  # days; 0 = off
    "young_account_action": "flag",  # "flag" or "kick"
    "close_after_verify": False,
}

SETTING_HELP = {
    "verified_role": "Role given when someone is verified (required).",
    "unverified_role": "Role new members get until they verify, removed on verify (or `none`).",
    "auto_role": "Extra role every new member gets, kept after verifying (or `none`).",
    "join_dm": "DM sent to new members (or `none` to not DM).",
    "keyword": "Word people DM the bot to start verification.",
    "prompt_message": "Reply sent in the ticket when someone DMs the keyword.",
    "verified_message": "Message sent to the member when verified (or `none`).",
    "staff_role": "Role pinged in the ticket when someone asks to verify (or `none`).",
    "log_channel": "Channel for verification logs (or `none`).",
    "kick_after": "Kick members who haven't verified after this long, e.g. `24h`, `3d` (or `none`).",
    "kick_message": "DM sent to members right before they're kicked for not verifying (or `none`).",
    "min_account_age": "Minimum Discord account age in days, e.g. `7` (or `0` to turn off).",
    "young_account_action": "What to do with accounts under the minimum age: `flag` or `kick`.",
    "close_after_verify": "Close the ticket automatically after `?verify`: `yes` or `no`.",
}

ROLE_KEYS = {"verified_role", "unverified_role", "auto_role", "staff_role"}
TEXT_KEYS = {"join_dm", "prompt_message", "verified_message", "kick_message"}

PLACEHOLDERS = "Placeholders: `{user}` name, `{mention}` mention, `{server}` server name, `{keyword}` keyword."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_duration(text: str) -> int:
    text = (text or "").strip().lower()
    if text in ("", "none", "off", "0", "never", "no"):
        return 0
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*(w|d|h|m|s)", text):
        total += int(amount) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    if total == 0 or re.sub(r"[\d\swdhms]", "", text):
        raise ValueError("Use a time like `30m`, `24h`, `3d` or `1w` (or `none`).")
    if total < 300:
        raise ValueError("The kick timer must be at least 5 minutes.")
    return total


def format_duration(seconds: int) -> str:
    if not seconds:
        return "never"
    parts = []
    for name, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{name}")
            seconds %= size
    return " ".join(parts) or "under a minute"


def parse_bool(text: str) -> bool:
    t = (text or "").strip().lower()
    if t in ("yes", "y", "true", "on", "1", "enable", "enabled"):
        return True
    if t in ("no", "n", "false", "off", "0", "disable", "disabled", "none"):
        return False
    raise ValueError("Answer `yes` or `no`.")


def account_age_days(user: discord.abc.User) -> float:
    return (discord.utils.utcnow() - user.created_at).total_seconds() / 86400


def fill(text: str, member: discord.abc.User, guild: Optional[discord.Guild], keyword: str) -> str:
    return (
        str(text)
        .replace("{user}", getattr(member, "display_name", None) or member.name)
        .replace("{mention}", member.mention)
        .replace("{server}", guild.name if guild else "the server")
        .replace("{keyword}", keyword)
    )


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Verification(commands.Cog):
    """Verify new members through Modmail tickets."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.settings = dict(DEFAULTS)
        self._ready = asyncio.Event()

    async def cog_load(self):
        await self._load()
        self.kick_loop.start()

    async def cog_unload(self):
        self.kick_loop.cancel()

    async def _load(self):
        try:
            doc = await self.db.find_one({"_id": "config"})
            if doc:
                self.settings.update({k: v for k, v in doc.items() if k in DEFAULTS})
        finally:
            self._ready.set()

    async def _save(self):
        await self.db.replace_one({"_id": "config"}, {"_id": "config", **self.settings}, upsert=True)

    # ---- small getters --------------------------------------------------- #
    @property
    def guild(self) -> Optional[discord.Guild]:
        return self.bot.guild

    def role(self, key: str) -> Optional[discord.Role]:
        rid = self.settings.get(key)
        if not rid or self.guild is None:
            return None
        return self.guild.get_role(int(rid))

    def log_channel(self) -> Optional[discord.abc.Messageable]:
        cid = self.settings.get("log_channel")
        if not cid:
            return None
        return self.bot.get_channel(int(cid))

    @property
    def configured(self) -> bool:
        return bool(self.settings.get("verified_role"))

    def _role_problem(self, role: discord.Role) -> Optional[str]:
        me = self.guild.me if self.guild else None
        if me is None:
            return None
        if not me.guild_permissions.manage_roles:
            return "I need the **Manage Roles** permission."
        if role >= me.top_role:
            return f"The {role.mention} role is above my highest role. Move my role above it in Server Settings → Roles."
        if role.managed or role.is_default():
            return f"{role.mention} can't be given out by bots."
        return None

    async def log(self, title: str, member: discord.abc.User, color: int, *, by=None, extra: str = ""):
        channel = self.log_channel()
        if channel is None:
            return
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        desc = f"{member.mention} (`{member.id}`)\nAccount age: **{account_age_days(member):.0f} days**"
        if by is not None:
            desc += f"\nBy: {by.mention}"
        if extra:
            desc += f"\n{extra}"
        embed.description = desc
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning("Verification log failed: %s", e)

    # ---- pending (kick timer) ------------------------------------------- #
    async def add_pending(self, member: discord.Member):
        if not self.settings["kick_after"]:
            return
        kick_at = discord.utils.utcnow() + timedelta(seconds=int(self.settings["kick_after"]))
        await self.db.replace_one(
            {"_id": f"pending-{member.id}"},
            {"_id": f"pending-{member.id}", "type": "pending", "user_id": str(member.id), "kick_at": kick_at.isoformat()},
            upsert=True,
        )

    async def remove_pending(self, user_id: int):
        await self.db.delete_one({"_id": f"pending-{user_id}"})

    @tasks.loop(minutes=5)
    async def kick_loop(self):
        await self._ready.wait()
        if not self.settings["enabled"] or not self.settings["kick_after"] or self.guild is None:
            return
        verified = self.role("verified_role")
        now = discord.utils.utcnow()
        for doc in await self.db.find({"type": "pending"}).to_list(None):
            uid = int(doc["user_id"])
            member = self.guild.get_member(uid)
            if member is None or (verified and verified in member.roles):
                await self.remove_pending(uid)
                continue
            kick_at = datetime.fromisoformat(doc["kick_at"])
            if kick_at.tzinfo is None:
                kick_at = kick_at.replace(tzinfo=timezone.utc)
            if now < kick_at:
                continue
            await self._kick(member, "Didn't verify in time", self.settings["kick_message"])
            await self.remove_pending(uid)

    @kick_loop.before_loop
    async def _before_kick_loop(self):
        await self.bot.wait_until_ready()

    async def _kick(self, member: discord.Member, reason: str, dm_text: Optional[str]):
        if dm_text:
            try:
                await member.send(fill(dm_text, member, self.guild, self.settings["keyword"]))
            except discord.HTTPException:
                pass
        try:
            await member.kick(reason=f"Verification: {reason}")
            await self.log("Kicked", member, 0xED4245, extra=f"Reason: {reason}")
        except discord.HTTPException as e:
            logger.warning("Verification couldn't kick %s: %s", member, e)
            await self.log("Couldn't kick", member, 0xED4245, extra=f"Reason: {reason}\nError: {e}. I need **Kick Members**.")

    # ---- events ---------------------------------------------------------- #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._ready.wait()
        if member.bot or self.guild is None or member.guild.id != self.guild.id:
            return
        if not self.settings["enabled"] or not self.configured:
            return

        age = account_age_days(member)
        min_age = int(self.settings["min_account_age"] or 0)
        young = min_age and age < min_age
        if young and self.settings["young_account_action"] == "kick":
            await self._kick(
                member,
                f"Account younger than {min_age} days",
                f"Your Discord account is too new to join **{{server}}**. Accounts must be at least {min_age} days old.",
            )
            return

        roles = [r for r in (self.role("unverified_role"), self.role("auto_role")) if r is not None]
        if roles:
            try:
                await member.add_roles(*roles, reason="Verification: new member")
            except discord.HTTPException as e:
                logger.warning("Verification couldn't add join roles to %s: %s", member, e)

        if self.settings["join_dm"]:
            try:
                await member.send(fill(self.settings["join_dm"], member, self.guild, self.settings["keyword"]))
            except discord.HTTPException:
                pass  # DMs closed

        await self.add_pending(member)
        extra = f"⚠️ **New account** (under {min_age} days)" if young else ""
        if self.settings["kick_after"]:
            extra += f"\nWill be kicked if not verified within {format_duration(int(self.settings['kick_after']))}."
        await self.log("Member joined", member, 0x3498DB, extra=extra.strip())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if self.guild is not None and member.guild.id == self.guild.id:
            await self.remove_pending(member.id)

    # ---- alias + autotrigger -------------------------------------------- #
    async def install_trigger(self, old_keyword: Optional[str] = None):
        cfg = self.bot.config
        aliases = cfg["aliases"]
        triggers = cfg["auto_triggers"]
        aliases[ALIAS_NAME] = PROMPT_COMMAND
        if old_keyword and old_keyword != self.settings["keyword"] and triggers.get(old_keyword) == ALIAS_NAME:
            triggers.pop(old_keyword, None)
        triggers[self.settings["keyword"]] = ALIAS_NAME
        await cfg.update()

    async def remove_trigger(self):
        cfg = self.bot.config
        cfg["aliases"].pop(ALIAS_NAME, None)
        for k, v in list(cfg["auto_triggers"].items()):
            if v == ALIAS_NAME:
                cfg["auto_triggers"].pop(k, None)
        await cfg.update()

    # ---- setup wizard --------------------------------------------------- #
    async def _ask(self, ctx, question: str, *, current=None, optional=True):
        hint = "Type `skip` to keep the current value" if current is not None else ""
        if optional:
            hint += (", " if hint else "Type ") + "`none` to leave it empty"
        hint += (", " if hint else "Type ") + "or `cancel` to stop."
        embed = discord.Embed(color=self.bot.main_color, description=f"{question}\n\n*{hint}*")
        if current is not None:
            embed.set_footer(text=f"Current: {str(current)[:200]}")
        await ctx.send(embed=embed)
        try:
            msg = await self.bot.wait_for(
                "message",
                check=lambda m: m.author == ctx.author and m.channel == ctx.channel,
                timeout=300,
            )
        except asyncio.TimeoutError:
            raise commands.BadArgument("Setup timed out. Run `?verification setup` again.")
        answer = msg.content.strip()
        if answer.lower() == "cancel":
            raise commands.BadArgument("Setup cancelled. Nothing was changed.")
        if answer.lower() == "skip" and current is not None:
            return "__skip__"
        return answer

    async def _convert(self, ctx, key: str, value: str):
        """Turn user input into a stored setting value. Raises ValueError with a friendly message."""
        low = value.strip().lower()
        if key in ROLE_KEYS:
            if low == "none":
                if key == "verified_role":
                    raise ValueError("A verified role is required.")
                return None
            try:
                role = await commands.RoleConverter().convert(ctx, value.strip())
            except commands.BadArgument:
                raise ValueError(f"I couldn't find a role called `{value}`. Mention it, or paste its ID.")
            if key != "staff_role":
                problem = self._role_problem(role)
                if problem:
                    raise ValueError(problem)
            return role.id
        if key == "log_channel":
            if low == "none":
                return None
            try:
                ch = await commands.TextChannelConverter().convert(ctx, value.strip())
            except commands.BadArgument:
                raise ValueError(f"I couldn't find a channel called `{value}`.")
            return ch.id
        if key in TEXT_KEYS:
            if low == "none":
                if key == "prompt_message":
                    raise ValueError("The verification reply can't be empty.")
                return None
            if len(value) > 1800:
                raise ValueError("Keep messages under 1800 characters.")
            return value
        if key == "keyword":
            if low == "none" or not low or len(low) > 50:
                raise ValueError("Pick a short keyword, like `verify`.")
            return low
        if key == "kick_after":
            return parse_duration(value)
        if key == "min_account_age":
            if low in ("none", "off"):
                return 0
            if not low.isdigit() or int(low) > 365:
                raise ValueError("Enter a number of days between 0 and 365.")
            return int(low)
        if key == "young_account_action":
            if low not in ("flag", "kick"):
                raise ValueError("Choose `flag` or `kick`.")
            return low
        if key == "close_after_verify":
            return parse_bool(value)
        raise ValueError(f"Unknown setting `{key}`.")

    def _display(self, key: str):
        v = self.settings.get(key)
        if key in ROLE_KEYS:
            r = self.role(key)
            return r.mention if r else ("*(deleted role)*" if v else "none")
        if key == "log_channel":
            ch = self.log_channel()
            return ch.mention if ch else ("*(deleted channel)*" if v else "none")
        if key == "kick_after":
            return format_duration(int(v or 0))
        if key == "min_account_age":
            return f"{v} days" if v else "off"
        if key == "close_after_verify":
            return "yes" if v else "no"
        if v is None or v == "":
            return "none"
        return str(v)

    # ---- commands: owner ------------------------------------------------ #
    @commands.group(name="verification", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def verification(self, ctx):
        """Verify new members through Modmail tickets."""
        await ctx.send_help(ctx.command)

    @verification.command(name="setup")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def verification_setup(self, ctx):
        """Interactive setup for verification."""
        await self._ready.wait()
        if self.guild is None:
            return await ctx.send(embed=self._err("I'm not in my main server yet."))
        old_keyword = self.settings["keyword"]
        steps = [
            ("verified_role", "**1/10 · Verified role**\nWhich role should verified members get? Mention it or paste its ID.", False),
            ("unverified_role", "**2/10 · Unverified role**\nWhich role should new members have *until* they verify? It's removed when they're verified.", True),
            ("auto_role", "**3/10 · Auto role**\nAn extra role every new member gets when they join, which they keep after verifying.", True),
            ("join_dm", f"**4/10 · Welcome DM**\nWhat should I DM new members when they join?\n{PLACEHOLDERS}", True),
            ("keyword", "**5/10 · Keyword**\nWhat word should people DM me to start verification? (e.g. `verify`)", False),
            ("prompt_message", f"**6/10 · Verification reply**\nWhat should I reply in the ticket when someone DMs the keyword?\n{PLACEHOLDERS}", False),
            ("staff_role", "**7/10 · Staff ping**\nWhich role should I ping in the ticket when someone asks to verify?", True),
            ("log_channel", "**8/10 · Log channel**\nWhere should I log joins, verifications and kicks? Mention the channel.", True),
            ("kick_after", "**9/10 · Kick timer**\nKick members who haven't verified after how long? e.g. `24h`, `3d`. Only applies to people who join from now on.", True),
            ("min_account_age", "**10/10 · Minimum account age**\nMinimum Discord account age in days, e.g. `7`. Type `0` to turn off.", True),
        ]
        new = dict(self.settings)
        try:
            for key, question, optional in steps:
                current = self._display(key) if (self.configured or key in ("keyword", "join_dm", "prompt_message")) else None
                while True:
                    answer = await self._ask(ctx, question, current=current, optional=optional)
                    if answer == "__skip__":
                        break
                    try:
                        new[key] = await self._convert(ctx, key, answer)
                        break
                    except ValueError as e:
                        await ctx.send(embed=self._err(f"{e} Try again."))
                if key == "min_account_age" and new["min_account_age"]:
                    while True:
                        answer = await self._ask(
                            ctx,
                            "**Young accounts**\nWhat should I do with accounts under that age? `flag` (warn staff in the log) or `kick`.",
                            current=new["young_account_action"],
                            optional=False,
                        )
                        if answer == "__skip__":
                            break
                        try:
                            new["young_account_action"] = await self._convert(ctx, "young_account_action", answer)
                            break
                        except ValueError as e:
                            await ctx.send(embed=self._err(f"{e} Try again."))
        except commands.BadArgument as e:
            return await ctx.send(embed=self._err(str(e)))

        new["enabled"] = True
        self.settings = new
        await self._save()
        await self.install_trigger(old_keyword)
        embed = self._settings_embed("Verification is set up ✅")
        embed.add_field(
            name="How it works now",
            value=(
                f"1. New members get the join roles and the welcome DM.\n"
                f"2. They DM me **{self.settings['keyword']}**, a ticket opens and I reply with your message.\n"
                f"3. Staff check them and run `{ctx.clean_prefix}verify` in the ticket."
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @verification.command(name="show", aliases=["settings", "config"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def verification_show(self, ctx):
        """Show the verification settings."""
        await self._ready.wait()
        await ctx.send(embed=self._settings_embed("Verification settings"))

    @verification.command(name="set")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def verification_set(self, ctx, key: str.lower = None, *, value: str = None):
        """
        Change one verification setting.

        Run without anything to see every setting you can change.
        """
        await self._ready.wait()
        if key not in SETTING_HELP or value is None:
            lines = "\n".join(f"`{k}`: {v}" for k, v in SETTING_HELP.items())
            embed = discord.Embed(
                title="Verification settings you can change",
                description=f"{lines}\n\nExample: `{ctx.clean_prefix}verification set kick_after 24h`\n{PLACEHOLDERS}",
                color=self.bot.main_color,
            )
            return await ctx.send(embed=embed)
        try:
            converted = await self._convert(ctx, key, value)
        except ValueError as e:
            return await ctx.send(embed=self._err(str(e)))
        old_keyword = self.settings["keyword"]
        self.settings[key] = converted
        await self._save()
        if key == "keyword" and self.configured:
            await self.install_trigger(old_keyword)
        await ctx.send(
            embed=discord.Embed(color=self.bot.main_color, description=f"`{key}` is now: {self._display(key)}")
        )

    @verification.command(name="enable")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def verification_enable(self, ctx):
        """Turn verification back on."""
        await self._ready.wait()
        if not self.configured:
            return await ctx.send(embed=self._err(f"Run `{ctx.clean_prefix}verification setup` first."))
        self.settings["enabled"] = True
        await self._save()
        await self.install_trigger()
        await ctx.send(embed=discord.Embed(color=self.bot.main_color, description="Verification is **on**."))

    @verification.command(name="disable")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def verification_disable(self, ctx):
        """Pause verification (no join roles, DMs, prompts or kicks). Settings are kept."""
        await self._ready.wait()
        self.settings["enabled"] = False
        await self._save()
        await self.remove_trigger()
        await ctx.send(
            embed=discord.Embed(
                color=self.bot.main_color,
                description=f"Verification is **off**. Your settings are kept; `{ctx.clean_prefix}verification enable` turns it back on.",
            )
        )

    @verification.command(name="reset")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def verification_reset(self, ctx):
        """Delete all verification settings, the alias, the autotrigger and the kick list."""
        await self._ready.wait()
        await self.remove_trigger()
        await self.db.delete_many({})
        self.settings = dict(DEFAULTS)
        await ctx.send(embed=discord.Embed(color=self.bot.main_color, description="Verification has been reset."))

    # ---- command run by the autotrigger --------------------------------- #
    @verification.command(name="prompt", hidden=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def verification_prompt(self, ctx):
        """Send the verification reply in this ticket (used by the autotrigger)."""
        await self._ready.wait()
        if not self.settings["enabled"] or not self.configured:
            return
        thread = ctx.thread
        user = thread.recipient
        member = self.guild.get_member(user.id) if self.guild else None

        reply = self.bot.get_command("reply")
        await ctx.invoke(reply, msg=fill(self.settings["prompt_message"], user, self.guild, self.settings["keyword"]))

        verified = self.role("verified_role")
        embed = discord.Embed(title="Verification request", color=0xFEE75C)
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
        age = account_age_days(user)
        min_age = int(self.settings["min_account_age"] or 0)
        age_text = f"{age:.0f} days (created {discord.utils.format_dt(user.created_at, 'D')})"
        if min_age and age < min_age:
            age_text = f"⚠️ {age_text}, under the {min_age}-day minimum"
        embed.add_field(name="Account age", value=age_text, inline=False)
        if member is None:
            status = "❌ Not in the server"
        elif verified and verified in member.roles:
            status = "✅ Already verified"
        else:
            status = f"⏳ Not verified · joined {discord.utils.format_dt(member.joined_at, 'R')}" if member.joined_at else "⏳ Not verified"
        embed.add_field(name="Status", value=status, inline=False)
        embed.set_footer(text=f"Approve with {self.bot.prefix}verify · Deny with {self.bot.prefix}close")
        staff = self.role("staff_role")
        await ctx.channel.send(
            content=staff.mention if staff else None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        if member is not None:
            await self.log("Verification requested", member, 0xFEE75C, extra=f"Ticket: {ctx.channel.mention}")

    # ---- commands: staff ------------------------------------------------ #
    async def _resolve_member(self, ctx, member: Optional[discord.Member]) -> Optional[discord.Member]:
        if member is not None:
            return member
        thread = getattr(ctx, "thread", None)
        if thread is not None and self.guild is not None:
            return self.guild.get_member(thread.recipient.id)
        return None

    @commands.command(name="verify")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def verify(self, ctx, member: discord.Member = None):
        """
        Verify a member.

        In a Modmail ticket you can leave out the member to verify the ticket's user.
        """
        await self._ready.wait()
        if not self.configured:
            return await ctx.send(embed=self._err(f"Verification isn't set up. An owner can run `{ctx.clean_prefix}verification setup`."))
        target = await self._resolve_member(ctx, member)
        if target is None:
            return await ctx.send(embed=self._err("Tell me who to verify, or run this inside their ticket. They must be in the server."))
        verified = self.role("verified_role")
        if verified is None:
            return await ctx.send(embed=self._err("The verified role no longer exists. Set a new one with `verification set verified_role`."))
        if verified in target.roles:
            return await ctx.send(embed=self._err(f"{target.mention} is already verified."))
        try:
            await target.add_roles(verified, reason=f"Verified by {ctx.author}")
            unverified = self.role("unverified_role")
            if unverified and unverified in target.roles:
                await target.remove_roles(unverified, reason=f"Verified by {ctx.author}")
        except discord.HTTPException as e:
            problem = self._role_problem(verified) or str(e)
            return await ctx.send(embed=self._err(f"I couldn't change their roles. {problem}"))

        await self.remove_pending(target.id)
        await self.log("Verified", target, 0x57F287, by=ctx.author)

        in_their_ticket = getattr(ctx, "thread", None) is not None and ctx.thread.recipient.id == target.id
        text = self.settings["verified_message"]
        if text:
            text = fill(text, target, self.guild, self.settings["keyword"])
            if in_their_ticket:
                await ctx.invoke(self.bot.get_command("reply"), msg=text)
            else:
                try:
                    await target.send(text)
                except discord.HTTPException:
                    pass
        await ctx.send(embed=discord.Embed(color=0x57F287, description=f"✅ {target.mention} is verified."))
        if in_their_ticket and self.settings["close_after_verify"]:
            await ctx.invoke(self.bot.get_command("close"), option="silently")

    @commands.command(name="unverify")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def unverify(self, ctx, member: discord.Member = None):
        """Remove a member's verification (gives the unverified role back)."""
        await self._ready.wait()
        target = await self._resolve_member(ctx, member)
        if target is None:
            return await ctx.send(embed=self._err("Tell me who to unverify."))
        verified, unverified = self.role("verified_role"), self.role("unverified_role")
        try:
            if verified and verified in target.roles:
                await target.remove_roles(verified, reason=f"Unverified by {ctx.author}")
            if unverified and unverified not in target.roles:
                await target.add_roles(unverified, reason=f"Unverified by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.send(embed=self._err(f"I couldn't change their roles: {e}"))
        await self.log("Unverified", target, 0xE67E22, by=ctx.author)
        await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=f"{target.mention} is no longer verified."))

    @verification.command(name="pending")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def verification_pending(self, ctx):
        """List members waiting to verify (who'll be kicked if they don't)."""
        await self._ready.wait()
        docs = await self.db.find({"type": "pending"}).to_list(None)
        lines = []
        for d in sorted(docs, key=lambda d: d["kick_at"]):
            member = self.guild.get_member(int(d["user_id"])) if self.guild else None
            if member is None:
                continue
            kick_at = datetime.fromisoformat(d["kick_at"])
            if kick_at.tzinfo is None:
                kick_at = kick_at.replace(tzinfo=timezone.utc)
            lines.append(f"{member.mention}: kicked {discord.utils.format_dt(kick_at, 'R')}")
        if not lines:
            text = "Nobody is waiting to verify." if self.settings["kick_after"] else "The kick timer is off, so nobody is tracked."
            return await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=text))
        embed = discord.Embed(title=f"Waiting to verify ({len(lines)})", description="\n".join(lines)[:4000], color=self.bot.main_color)
        await ctx.send(embed=embed)

    # ---- embeds ---------------------------------------------------------- #
    def _settings_embed(self, title: str) -> discord.Embed:
        status = "on" if self.settings["enabled"] else "off"
        embed = discord.Embed(title=title, color=self.bot.main_color, description=f"Status: **{status}**")
        rows = [
            ("Verified role", "verified_role"),
            ("Unverified role", "unverified_role"),
            ("Auto role", "auto_role"),
            ("Keyword", "keyword"),
            ("Staff ping", "staff_role"),
            ("Log channel", "log_channel"),
            ("Kick timer", "kick_after"),
            ("Min account age", "min_account_age"),
        ]
        for name, key in rows:
            embed.add_field(name=name, value=self._display(key))
        if self.settings["min_account_age"]:
            embed.add_field(name="Young accounts", value=self.settings["young_account_action"])
        embed.add_field(name="Close after verify", value=self._display("close_after_verify"))
        for name, key in (("Welcome DM", "join_dm"), ("Verification reply", "prompt_message"), ("Verified message", "verified_message")):
            embed.add_field(name=name, value=(self._display(key))[:1024], inline=False)
        return embed

    def _err(self, text: str) -> discord.Embed:
        return discord.Embed(title="Error", description=text, color=self.bot.error_color)


async def setup(bot):
    await bot.add_cog(Verification(bot))
