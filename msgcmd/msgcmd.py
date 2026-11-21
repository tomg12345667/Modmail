"""
Message commands with variables for Modmail.

Create your own commands (like ?outage) that send a message with blanks to
fill in. When someone runs the command, the bot asks for each variable,
shows a preview, and sends it.

    ?msgcmd create          step-by-step: name, message, variables, where to send, who can use it
    ?msgcmd edit <name>     change a command
    ?msgcmd delete <name>   delete a command
    ?msgcmd list            list your commands
    ?msgcmd show <name>     show a command's message and settings

Running a command:
    ?outage                 the bot asks for each variable, then confirms
    ?outage 2pm | 4pm       fill the variables in order, separated by |, and send straight away
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from core.checks import has_permissions_predicate
from core.models import PermissionLevel, getLogger
from core import checks

logger = getLogger(__name__)

VAR_RE = re.compile(r"\{([A-Za-z0-9_]{1,32})\}")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# Filled in automatically, never asked for
BUILTINS = {
    "user": "the ticket user's name (in a ticket)",
    "mention": "mentions the ticket user (in a ticket)",
    "staff": "your name",
    "server": "the server's name",
    "channel": "the channel the command was run in",
}

DESTINATIONS = {
    "here": "the channel the command is run in",
    "reply": "a reply to the ticket's user (only works in a ticket)",
    "anonreply": "an anonymous reply to the ticket's user (only works in a ticket)",
}

LEVELS = ["supporter", "moderator", "administrator", "owner"]


class Cancelled(Exception):
    pass


def find_variables(text: str) -> List[str]:
    seen = []
    for name in VAR_RE.findall(text):
        low = name.lower()
        if low not in BUILTINS and low not in seen:
            seen.append(low)
    return seen


def fill(text: str, values: Dict[str, str]) -> str:
    def sub(m):
        key = m.group(1).lower()
        return values.get(key, m.group(0))

    return VAR_RE.sub(sub, text)


class MessageCommands(commands.Cog, name="Message Commands"):
    """Make your own commands that send messages with variables."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.registered: Dict[str, commands.Command] = {}
        self.data: Dict[str, dict] = {}

    async def cog_load(self):
        for doc in await self.db.find({"type": "msgcmd"}).to_list(None):
            self.data[doc["name"]] = doc
            self._register(doc)

    async def cog_unload(self):
        for name in list(self.registered):
            self.bot.remove_command(name)
        self.registered.clear()

    # ---- dynamic commands ------------------------------------------------ #
    def _register(self, doc: dict) -> bool:
        name = doc["name"]
        existing = self.bot.get_command(name)
        if existing is not None and name not in self.registered:
            logger.warning("Message command %s clashes with an existing command; not registered.", name)
            return False
        if name in self.registered:
            self.bot.remove_command(name)

        async def callback(cog, ctx, *, values: str = None):
            await cog.run(ctx, name, values)

        level = PermissionLevel[doc.get("level", "supporter").upper()]
        variables = ", ".join(v["name"] for v in doc.get("variables", [])) or "none"
        cmd = commands.Command(
            callback,
            name=name,
            help=f"Custom message command. Variables: {variables}.\n"
            f"Run it on its own to be asked for each one, or fill them in order separated by `|`.",
            usage="[value | value ...]",
            checks=[has_permissions_predicate(level)],
        )
        cmd.cog = self
        self.bot.add_command(cmd)
        self.registered[name] = cmd
        return True

    def _unregister(self, name: str):
        if name in self.registered:
            self.bot.remove_command(name)
            self.registered.pop(name, None)

    def _name_problem(self, name: str, allow: Optional[str] = None) -> Optional[str]:
        if not NAME_RE.match(name):
            return "Use 1-32 lowercase letters, numbers, `-` or `_` (no spaces)."
        if name == allow:
            return None
        if name in self.data:
            return f"`{name}` already exists. Use `msgcmd edit {name}` to change it."
        if self.bot.get_command(name) is not None:
            return f"`{name}` is already a bot command. Pick another name."
        if name in (self.bot.config.get("aliases") or {}) or name in (self.bot.config.get("snippets") or {}):
            return f"`{name}` is already an alias or snippet. Pick another name."
        return None

    # ---- conversation helper -------------------------------------------- #
    async def _ask(self, ctx, prompt: str, *, footer: str = None, cleanup: list = None, timeout: int = 300) -> str:
        embed = discord.Embed(description=prompt, color=self.bot.main_color)
        embed.set_footer(text=footer or "Type cancel to stop.")
        q = await ctx.send(embed=embed)
        try:
            msg = await self.bot.wait_for(
                "message", check=lambda m: m.author == ctx.author and m.channel == ctx.channel, timeout=timeout
            )
        except asyncio.TimeoutError:
            raise Cancelled("Timed out. Nothing was sent.")
        if cleanup is not None:
            cleanup.extend([q, msg])
        content = msg.content.strip()
        if content.lower() == "cancel":
            raise Cancelled("Cancelled. Nothing was sent.")
        return content

    async def _cleanup(self, ctx, messages: list):
        if not messages:
            return
        try:
            await ctx.channel.delete_messages(messages)
        except (discord.HTTPException, AttributeError):
            for m in messages:
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass

    # ---- running a command ---------------------------------------------- #
    def _builtins(self, ctx) -> Dict[str, str]:
        thread = getattr(ctx, "thread", None)
        recipient = getattr(thread, "recipient", None)
        guild = self.bot.guild
        return {
            "user": getattr(recipient, "display_name", None) or getattr(recipient, "name", "") or "",
            "mention": recipient.mention if recipient else "",
            "staff": getattr(ctx.author, "display_name", None) or ctx.author.name,
            "server": guild.name if guild else "",
            "channel": getattr(ctx.channel, "name", "") or "",
        }

    async def run(self, ctx, name: str, inline: Optional[str]):
        doc = self.data.get(name)
        if doc is None:
            return
        dest = doc.get("destination", "here")
        if dest in ("reply", "anonreply") and getattr(ctx, "thread", None) is None:
            return await ctx.send(embed=self._err(f"`{ctx.clean_prefix}{name}` replies to a ticket's user, so run it inside a ticket."))

        values = self._builtins(ctx)
        variables = doc.get("variables", [])
        cleanup: list = []
        interactive = inline is None
        try:
            if inline is not None and variables:
                parts = [p.strip() for p in inline.split("|")]
                if len(parts) != len(variables):
                    names = " | ".join(v["name"] for v in variables)
                    return await ctx.send(
                        embed=self._err(f"This command needs {len(variables)} value(s), in this order: `{names}`")
                    )
                for var, part in zip(variables, parts):
                    values[var["name"]] = part
            else:
                for i, var in enumerate(variables, 1):
                    values[var["name"]] = await self._ask(
                        ctx,
                        f"**{i}/{len(variables)}** · {var['question']}",
                        footer=f"Variable {{{var['name']}}} · type cancel to stop",
                        cleanup=cleanup,
                    )

            text = fill(doc["text"], values)
            if len(text) > 2000:
                raise Cancelled(f"The filled-in message is {len(text)} characters; Discord's limit is 2000.")

            if interactive:
                where = {"here": "this channel", "reply": "the ticket's user", "anonreply": "the ticket's user (anonymously)"}.get(
                    dest, f"<#{dest}>"
                )
                preview = discord.Embed(title="Preview", description=text, color=self.bot.main_color)
                preview.add_field(name="Sends to", value=where)
                preview.set_footer(text="Type yes to send, or anything else to cancel.")
                p = await ctx.send(embed=preview)
                cleanup.append(p)
                answer = await self._ask(ctx, "Send it?", footer="yes / no", cleanup=cleanup, timeout=120)
                if answer.lower() not in ("yes", "y", "send"):
                    raise Cancelled("Not sent.")

            await self._send(ctx, dest, text)
        except Cancelled as e:
            await self._cleanup(ctx, cleanup)
            return await ctx.send(embed=self._err(str(e)), delete_after=15)
        await self._cleanup(ctx, cleanup)

    async def _send(self, ctx, dest: str, text: str):
        if dest == "here":
            await ctx.send(text)
        elif dest in ("reply", "anonreply"):
            cmd = self.bot.get_command("reply" if dest == "reply" else "areply")
            await ctx.invoke(cmd, msg=text)
        else:
            channel = self.bot.get_channel(int(dest))
            if channel is None:
                raise Cancelled("The channel this command sends to no longer exists. Change it with `msgcmd edit`.")
            try:
                await channel.send(text)
            except discord.Forbidden:
                raise Cancelled(f"I don't have permission to send messages in {channel.mention}.")
            await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=f"Sent in {channel.mention}."), delete_after=10)

    # ---- management ----------------------------------------------------- #
    @commands.group(name="msgcmd", aliases=["messagecommand"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def msgcmd(self, ctx):
        """Make your own commands that send messages with variables."""
        await ctx.send_help(ctx.command)

    async def _wizard(self, ctx, old: Optional[dict]) -> Optional[dict]:
        skip = " Type `skip` to keep the current one." if old else ""
        doc = dict(old) if old else {"type": "msgcmd"}
        try:
            # 1. name
            if old is None:
                while True:
                    name = (await self._ask(ctx, "**1/5 · Command name**\nWhat should the command be called? e.g. `outage` makes `?outage`.")).lower().lstrip("?")
                    problem = self._name_problem(name)
                    if problem is None:
                        break
                    await ctx.send(embed=self._err(problem))
                doc["name"] = name
                doc["_id"] = f"msgcmd-{name}"

            # 2. message
            builtins = "\n".join(f"`{{{k}}}` {v}" for k, v in BUILTINS.items())
            while True:
                text = await self._ask(
                    ctx,
                    "**2/5 · Message**\nPaste the message. Put each blank in curly braces, like:\n"
                    "> Hi, we have an outage from {start} to {end}.\n\n"
                    f"These are filled in automatically:\n{builtins}{skip}",
                )
                if old and text.lower() == "skip":
                    text = old["text"]
                if len(text) > 1900:
                    await ctx.send(embed=self._err("Keep the message under 1900 characters so there's room for the variables."))
                    continue
                break
            doc["text"] = text

            # 3. variables + questions
            names = find_variables(text)
            old_q = {v["name"]: v["question"] for v in (old or {}).get("variables", [])}
            variables = []
            if names:
                listed = ", ".join(f"`{{{n}}}`" for n in names)
                await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=f"**3/5 · Variables**\nI found: {listed}"))
                for n in names:
                    default = old_q.get(n) or f"What should `{{{n}}}` be?"
                    q = await self._ask(
                        ctx,
                        f"What should I ask for `{{{n}}}`?\nType `skip` to use: *{default}*",
                    )
                    variables.append({"name": n, "question": default if q.lower() == "skip" else q})
            else:
                await ctx.send(
                    embed=discord.Embed(
                        color=self.bot.main_color,
                        description="**3/5 · Variables**\nNo `{variables}` found, so the command will send the message as it is.",
                    )
                )
            doc["variables"] = variables

            # 4. destination
            opts = "\n".join(f"`{k}`: {v}" for k, v in DESTINATIONS.items())
            while True:
                dest = await self._ask(
                    ctx, f"**4/5 · Where should it send?**\n{opts}\nOr mention a channel to always send there.{skip}"
                )
                low = dest.lower()
                if old and low == "skip":
                    break
                if low in DESTINATIONS:
                    doc["destination"] = low
                    break
                try:
                    channel = await commands.TextChannelConverter().convert(ctx, dest)
                    doc["destination"] = str(channel.id)
                    break
                except commands.BadArgument:
                    await ctx.send(embed=self._err("Type `here`, `reply`, `anonreply`, or mention a channel."))

            # 5. level
            while True:
                level = (
                    await self._ask(
                        ctx,
                        f"**5/5 · Who can use it?**\n`supporter`, `moderator`, `administrator` or `owner`.{skip}",
                    )
                ).lower()
                if old and level == "skip":
                    break
                if level in LEVELS:
                    doc["level"] = level
                    break
                await ctx.send(embed=self._err("Choose `supporter`, `moderator`, `administrator` or `owner`."))
            doc.setdefault("level", "supporter")
            doc.setdefault("destination", "here")
        except Cancelled as e:
            await ctx.send(embed=self._err(str(e).replace("Nothing was sent.", "Nothing was changed.")))
            return None
        return doc

    @msgcmd.command(name="create", aliases=["add", "new"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def msgcmd_create(self, ctx):
        """Create a message command, step by step."""
        doc = await self._wizard(ctx, None)
        if doc is None:
            return
        await self.db.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        self.data[doc["name"]] = doc
        self._register(doc)
        await ctx.send(embed=self._summary(ctx, doc, f"Created `{ctx.clean_prefix}{doc['name']}` ✅"))

    @msgcmd.command(name="edit")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def msgcmd_edit(self, ctx, name: str.lower):
        """Change a message command."""
        name = name.lower().lstrip("?")
        old = self.data.get(name)
        if old is None:
            return await ctx.send(embed=self._err(f"No message command called `{name}`."))
        doc = await self._wizard(ctx, old)
        if doc is None:
            return
        await self.db.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        self.data[name] = doc
        self._register(doc)
        await ctx.send(embed=self._summary(ctx, doc, f"Updated `{ctx.clean_prefix}{name}` ✅"))

    @msgcmd.command(name="delete", aliases=["remove", "del"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def msgcmd_delete(self, ctx, name: str.lower):
        """Delete a message command."""
        name = name.lower().lstrip("?")
        if name not in self.data:
            return await ctx.send(embed=self._err(f"No message command called `{name}`."))
        await self.db.delete_one({"_id": f"msgcmd-{name}"})
        self.data.pop(name, None)
        self._unregister(name)
        await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=f"Deleted `{ctx.clean_prefix}{name}`."))

    @msgcmd.command(name="list")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def msgcmd_list(self, ctx):
        """List message commands."""
        if not self.data:
            return await ctx.send(
                embed=self._err(f"No message commands yet. Create one with `{ctx.clean_prefix}msgcmd create`.")
            )
        lines = []
        for name in sorted(self.data):
            d = self.data[name]
            vars_ = ", ".join(v["name"] for v in d.get("variables", [])) or "no variables"
            lines.append(f"`{ctx.clean_prefix}{name}`: {vars_}")
        await ctx.send(
            embed=discord.Embed(title=f"Message commands ({len(lines)})", description="\n".join(lines)[:4000], color=self.bot.main_color)
        )

    @msgcmd.command(name="show", aliases=["info"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def msgcmd_show(self, ctx, name: str.lower):
        """Show a message command's message and settings."""
        name = name.lower().lstrip("?")
        doc = self.data.get(name)
        if doc is None:
            return await ctx.send(embed=self._err(f"No message command called `{name}`."))
        await ctx.send(embed=self._summary(ctx, doc, f"{ctx.clean_prefix}{name}"))

    # ---- embeds ---------------------------------------------------------- #
    def _summary(self, ctx, doc: dict, title: str) -> discord.Embed:
        embed = discord.Embed(title=title, description=doc["text"][:4000], color=self.bot.main_color)
        if doc.get("variables"):
            embed.add_field(
                name="Variables",
                value="\n".join(f"`{{{v['name']}}}`: {v['question']}" for v in doc["variables"])[:1024],
                inline=False,
            )
        dest = doc.get("destination", "here")
        embed.add_field(name="Sends to", value=DESTINATIONS.get(dest, f"<#{dest}>") if dest in DESTINATIONS else f"<#{dest}>")
        embed.add_field(name="Who can use it", value=doc.get("level", "supporter").title())
        if doc.get("variables"):
            example = " | ".join(f"<{v['name']}>" for v in doc["variables"])
            embed.add_field(
                name="Use it",
                value=f"`{ctx.clean_prefix}{doc['name']}` to be asked, or `{ctx.clean_prefix}{doc['name']} {example}`",
                inline=False,
            )
        return embed

    def _err(self, text: str) -> discord.Embed:
        return discord.Embed(title="Error", description=text, color=self.bot.error_color)


async def setup(bot):
    await bot.add_cog(MessageCommands(bot))
