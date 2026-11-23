"""
Reaction roles and auto reactions for Modmail.

Reaction roles: members react to a message to get a role, and remove their
reaction to lose it.

    ?rr post #channel Title | description   post a new role menu (an embed the bot keeps up to date)
    ?rr add <message> <emoji> <@role>       add a role to a menu, or to any message (link or ID)
    ?rr remove <message> <emoji>            take a role off a menu
    ?rr mode <message> <mode>               normal, unique (only one role at a time) or keep (no removing)
    ?rr list                                every menu on this server
    ?rr delete <message>                    stop a message giving roles

Auto reactions: the bot reacts to every new message in a channel.

    ?autoreact set #channel 👍 👎          react with these emojis in that channel
    ?autoreact bots #channel on|off         also react to bots' messages (off by default)
    ?autoreact remove #channel              stop reacting there
    ?autoreact list                         every channel with auto reactions

Both can also be managed from the web dashboard (Reactions).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel, getLogger

logger = getLogger(__name__)

MODES = {
    "normal": "React to get the role, remove your reaction to lose it.",
    "unique": "Members can only have one role from this menu; picking another swaps it.",
    "keep": "React to get the role. Removing the reaction doesn't take it away.",
}
MAX_ROLES = 20  # Discord allows 20 different reactions on a message
MAX_AUTO = 10
CUSTOM_RE = re.compile(r"^<(a?):([A-Za-z0-9_~]{2,32}):(\d{15,21})>$")
NAME_RE = re.compile(r"^:?([A-Za-z0-9_~]{2,32}):?$")


class ReactionError(Exception):
    """A problem to show to the person (in Discord or on the dashboard)."""


def emoji_key(emoji) -> str:
    """One stable key per emoji: the ID for custom emojis, the character(s) for normal ones."""
    if isinstance(emoji, (discord.PartialEmoji, discord.Emoji)) and emoji.id:
        return str(emoji.id)
    return str(getattr(emoji, "name", emoji))


class Reactions(commands.Cog):
    """Reaction roles and auto reactions."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.menus: Dict[int, dict] = {}  # message id -> menu
        self.auto: Dict[int, dict] = {}  # channel id -> auto reaction settings

    async def cog_load(self):
        for doc in await self.db.find({}).to_list(None):
            if doc.get("type") == "menu":
                self.menus[int(doc["message_id"])] = doc
            elif doc.get("type") == "auto":
                self.auto[int(doc["channel_id"])] = doc

    # ------------------------------------------------------------------ #
    # Shared helpers (used by the commands and the dashboard)
    # ------------------------------------------------------------------ #
    def resolve_emoji(self, guild: Optional[discord.Guild], text: str):
        """Turn what someone typed into something the bot can react with."""
        text = str(text or "").strip()
        if not text:
            raise ReactionError("Pick an emoji.")
        m = CUSTOM_RE.match(text)
        if m:
            emoji = self.bot.get_emoji(int(m.group(3)))
            if emoji is None:
                raise ReactionError(f"The bot can't use {text}: it has to be in the server that emoji belongs to.")
            return emoji
        m = NAME_RE.match(text)
        if m and guild is not None:
            emoji = discord.utils.get(guild.emojis, name=m.group(1))
            if emoji is not None:
                return emoji
            if not any(ord(c) > 127 for c in text):
                raise ReactionError(f"There's no emoji called :{m.group(1)}: on {guild.name}. Use a normal emoji like 🎮, or one of the server's own.")
        if not any(ord(c) > 127 for c in text) or len(text) > 16:
            raise ReactionError(f"`{text}` isn't an emoji.")
        return text  # a normal emoji; Discord checks it when the bot reacts

    def check_role(self, guild: discord.Guild, role: discord.Role):
        if role is None or role.guild != guild:
            raise ReactionError("Pick a role from the same server as the message.")
        if role.is_default():
            raise ReactionError("@everyone can't be given out.")
        if role.managed:
            raise ReactionError(f"{role.name} belongs to an integration or bot, so it can't be given out.")
        me = guild.me
        if not me.guild_permissions.manage_roles:
            raise ReactionError("The bot needs the Manage Roles permission.")
        if role >= me.top_role:
            raise ReactionError(f"Move the bot's role above {role.name} in Server Settings > Roles, so it can give it out.")

    def describe(self, menu: dict, guild: Optional[discord.Guild]) -> str:
        lines = []
        for r in menu["roles"]:
            role = guild.get_role(int(r["role_id"])) if guild else None
            lines.append(f"{r['emoji']} {role.mention if role else '(deleted role)'}")
        return "\n".join(lines) or "No roles yet."

    async def fetch_message(self, channel_id: int, message_id: int) -> discord.Message:
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            raise ReactionError("The bot can't see that channel.")
        try:
            return await channel.fetch_message(int(message_id))
        except discord.NotFound:
            raise ReactionError("That message was deleted.")
        except discord.Forbidden:
            raise ReactionError(f"The bot can't read messages in #{channel.name}.")

    async def save_menu(self, menu: dict):
        await self.db.replace_one({"_id": menu["_id"]}, menu, upsert=True)
        self.menus[int(menu["message_id"])] = menu

    async def refresh_embed(self, menu: dict, message: Optional[discord.Message] = None):
        """Menus the bot posted list their roles; keep that list current."""
        if not menu.get("posted"):
            return
        try:
            message = message or await self.fetch_message(menu["channel_id"], menu["message_id"])
            embed = discord.Embed(title=menu.get("title") or "Pick your roles", color=self.bot.main_color)
            text = (menu.get("description") or "React below to get a role.").strip()
            embed.description = f"{text}\n\n{self.describe(menu, message.guild)}"
            if menu.get("mode") == "unique":
                embed.set_footer(text="You can have one of these roles at a time.")
            elif menu.get("mode") == "keep":
                embed.set_footer(text="Roles stay after you remove your reaction.")
            await message.edit(embed=embed)
        except (ReactionError, discord.HTTPException) as e:
            logger.warning("Couldn't update reaction role menu %s: %s", menu["message_id"], e)

    async def post_menu(self, channel: discord.TextChannel, title: str, description: str, mode: str = "normal") -> dict:
        if mode not in MODES:
            raise ReactionError("Mode must be normal, unique or keep.")
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.add_reactions):
            raise ReactionError(f"The bot needs Send Messages, Embed Links and Add Reactions in #{channel.name}.")
        message = await channel.send(embed=discord.Embed(title=title or "Pick your roles", description="Setting up..."))
        menu = {
            "_id": f"menu-{message.id}", "type": "menu", "guild_id": str(channel.guild.id),
            "channel_id": str(channel.id), "message_id": str(message.id), "posted": True,
            "title": (title or "Pick your roles")[:256], "description": (description or "")[:3000],
            "mode": mode, "roles": [],
        }
        await self.save_menu(menu)
        await self.refresh_embed(menu, message)
        return menu

    async def attach_menu(self, message: discord.Message) -> dict:
        """Use any existing message as a role menu."""
        menu = self.menus.get(message.id)
        if menu is None:
            if message.guild is None:
                raise ReactionError("The message has to be in a server channel.")
            # posted stays False: a message someone wrote themselves (or made with the embed plugin) is never rewritten.
            menu = {
                "_id": f"menu-{message.id}", "type": "menu", "guild_id": str(message.guild.id),
                "channel_id": str(message.channel.id), "message_id": str(message.id), "posted": False,
                "title": (message.embeds[0].title if message.embeds else None) or (message.content or "")[:80] or None,
                "description": None, "mode": "normal", "roles": [],
            }
        return menu

    async def add_role(self, message: discord.Message, emoji_text: str, role: discord.Role) -> dict:
        menu = await self.attach_menu(message)
        emoji = self.resolve_emoji(message.guild, emoji_text)
        self.check_role(message.guild, role)
        key = emoji_key(emoji)
        roles = [r for r in menu["roles"] if r["key"] != key]
        if len(roles) >= MAX_ROLES:
            raise ReactionError(f"A message can have at most {MAX_ROLES} different reactions.")
        try:
            await message.add_reaction(emoji)
        except discord.Forbidden:
            raise ReactionError(f"The bot needs Add Reactions and Read Message History in #{message.channel.name}.")
        except discord.HTTPException:
            raise ReactionError(f"Discord didn't accept {emoji_text} as an emoji.")
        roles.append({"key": key, "emoji": str(emoji), "role_id": str(role.id)})
        menu["roles"] = roles
        await self.save_menu(menu)
        await self.refresh_embed(menu, message)
        return menu

    async def remove_role(self, menu: dict, emoji_text: str) -> dict:
        guild = self.bot.get_guild(int(menu["guild_id"]))
        try:
            key = emoji_key(self.resolve_emoji(guild, emoji_text))
        except ReactionError:
            key = str(emoji_text).strip()
        entry = next((r for r in menu["roles"] if r["key"] == key or r["emoji"] == str(emoji_text).strip()), None)
        if entry is None:
            raise ReactionError("That emoji isn't on this menu.")
        menu["roles"] = [r for r in menu["roles"] if r is not entry]
        await self.save_menu(menu)
        try:
            message = await self.fetch_message(menu["channel_id"], menu["message_id"])
            await message.clear_reaction(entry["emoji"])
            await self.refresh_embed(menu, message)
        except (ReactionError, discord.HTTPException):
            pass
        return menu

    async def set_mode(self, menu: dict, mode: str) -> dict:
        if mode not in MODES:
            raise ReactionError("Mode must be normal, unique or keep.")
        menu["mode"] = mode
        await self.save_menu(menu)
        await self.refresh_embed(menu)
        return menu

    async def delete_menu(self, menu: dict, *, delete_message: bool = False):
        self.menus.pop(int(menu["message_id"]), None)
        await self.db.delete_one({"_id": menu["_id"]})
        try:
            message = await self.fetch_message(menu["channel_id"], menu["message_id"])
            if delete_message and message.author == self.bot.user:
                await message.delete()
            else:
                for r in menu["roles"]:
                    await message.clear_reaction(r["emoji"])
        except (ReactionError, discord.HTTPException):
            pass

    async def set_auto(self, channel: discord.TextChannel, emoji_texts: List[str], include_bots: Optional[bool] = None) -> dict:
        emojis = []
        for text in emoji_texts:
            if not str(text).strip():
                continue
            e = self.resolve_emoji(channel.guild, text)
            if str(e) not in emojis:
                emojis.append(str(e))
        if not emojis:
            raise ReactionError("Pick at least one emoji.")
        if len(emojis) > MAX_AUTO:
            raise ReactionError(f"Use at most {MAX_AUTO} emojis, or busy channels will hit Discord's limits.")
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.add_reactions and perms.read_message_history):
            raise ReactionError(f"The bot needs Add Reactions and Read Message History in #{channel.name}.")
        old = self.auto.get(channel.id, {})
        doc = {
            "_id": f"auto-{channel.id}", "type": "auto", "guild_id": str(channel.guild.id), "channel_id": str(channel.id),
            "emojis": emojis, "include_bots": bool(old.get("include_bots", False) if include_bots is None else include_bots),
        }
        await self.db.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        self.auto[channel.id] = doc
        return doc

    async def remove_auto(self, channel_id: int):
        if int(channel_id) not in self.auto:
            raise ReactionError("That channel doesn't have auto reactions.")
        self.auto.pop(int(channel_id), None)
        await self.db.delete_one({"_id": f"auto-{channel_id}"})

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        settings = self.auto.get(getattr(message.channel, "id", None))
        if not settings or message.author == self.bot.user:
            return
        if message.author.bot and not settings.get("include_bots"):
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        for e in settings["emojis"]:
            try:
                await message.add_reaction(e)
            except discord.HTTPException as err:
                logger.warning("Auto reaction %s in #%s failed: %s", e, message.channel, err)
                if isinstance(err, discord.Forbidden):
                    return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._on_reaction(payload, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._on_reaction(payload, added=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        menu = self.menus.pop(payload.message_id, None)
        if menu is not None:
            await self.db.delete_one({"_id": menu["_id"]})

    async def _on_reaction(self, payload, *, added: bool):
        menu = self.menus.get(payload.message_id)
        if menu is None or payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        key = emoji_key(payload.emoji)
        entry = next((r for r in menu["roles"] if r["key"] == key), None)
        if entry is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(int(entry["role_id"]))
        member = payload.member if added and payload.member else guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if role is None or member.bot:
            return
        mode = menu.get("mode", "normal")
        try:
            if added:
                if mode == "unique":
                    others = [guild.get_role(int(r["role_id"])) for r in menu["roles"] if r is not entry]
                    others = [r for r in others if r is not None and r in member.roles]
                    if others:
                        await member.remove_roles(*others, reason="Reaction roles: swapped to another role")
                    await self._clear_other_reactions(payload, member, entry)
                if role not in member.roles:
                    await member.add_roles(role, reason="Reaction roles")
            elif mode != "keep" and role in member.roles:
                await member.remove_roles(role, reason="Reaction roles: reaction removed")
        except discord.Forbidden:
            logger.warning("Reaction roles: no permission to change %s for %s (check Manage Roles and role order).", role, member)
        except discord.HTTPException as e:
            logger.warning("Reaction roles: couldn't change %s for %s: %s", role, member, e)

    async def _clear_other_reactions(self, payload, member, keep_entry):
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = channel.get_partial_message(payload.message_id)
            menu = self.menus.get(payload.message_id, {})
            for r in menu.get("roles", []):
                if r is not keep_entry:
                    try:
                        await message.remove_reaction(r["emoji"], member)
                    except discord.NotFound:
                        pass
        except discord.HTTPException:
            pass  # needs Manage Messages; roles were still swapped

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def _ok(self, ctx, text, title=None):
        await ctx.send(embed=discord.Embed(title=title, description=text, color=self.bot.main_color))

    async def _err(self, ctx, text):
        await ctx.send(embed=discord.Embed(title="Error", description=text, color=self.bot.error_color))

    async def _menu_for(self, ctx, message: discord.Message):
        menu = self.menus.get(message.id)
        if menu is None:
            raise ReactionError("That message isn't a reaction role menu. Add a role to it first with `rr add`.")
        return menu

    @commands.group(name="rr", aliases=["reactionroles", "reactionrole"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def rr(self, ctx):
        """Reaction roles: react to a message to get a role."""
        await ctx.send_help(ctx.command)

    @rr.command(name="post")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def rr_post(self, ctx, channel: discord.TextChannel, *, text: str = "Pick your roles"):
        """
        Post a new role menu in a channel.

        Write a title, and optionally `|` then a description.
        Then add roles with `{prefix}rr add <message link> <emoji> <@role>`.

        **Example:**
        `{prefix}rr post #roles Pick your pings | React to get notified about each topic.`
        """
        title, _, desc = text.partition("|")
        try:
            menu = await self.post_menu(channel, title.strip(), desc.strip())
        except ReactionError as e:
            return await self._err(ctx, str(e))
        link = f"https://discord.com/channels/{menu['guild_id']}/{menu['channel_id']}/{menu['message_id']}"
        await self._ok(ctx, f"Posted in {channel.mention}. Now add roles:\n`{self.bot.prefix}rr add {link} 🎮 @Gamer`", "Role menu posted")

    @rr.command(name="add")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def rr_add(self, ctx, message: discord.Message, emoji: str, *, role: discord.Role):
        """
        Give a role to anyone who reacts with an emoji.

        Works on a menu posted with `{prefix}rr post`, or on any message (use its link).

        **Example:**
        `{prefix}rr add https://discord.com/channels/... 🎮 @Gamer`
        """
        try:
            menu = await self.add_role(message, emoji, role)
        except ReactionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, f"Reacting with {menu['roles'][-1]['emoji']} now gives {role.mention}.\n\n{self.describe(menu, message.guild)}", "Role added")

    @rr.command(name="remove")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def rr_remove(self, ctx, message: discord.Message, emoji: str):
        """Take a role off a menu (members keep roles they already have)."""
        try:
            menu = await self.remove_role(await self._menu_for(ctx, message), emoji)
        except ReactionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, self.describe(menu, message.guild), "Role removed from the menu")

    @rr.command(name="mode")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def rr_mode(self, ctx, message: discord.Message, mode: str.lower):
        """
        How a menu behaves:

        `normal` - react to get the role, remove the reaction to lose it.
        `unique` - only one role from the menu at a time.
        `keep` - removing the reaction doesn't take the role away.
        """
        try:
            await self.set_mode(await self._menu_for(ctx, message), mode)
        except ReactionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, MODES[mode], f"Mode: {mode}")

    @rr.command(name="list")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def rr_list(self, ctx):
        """List every role menu."""
        if not self.menus:
            return await self._ok(ctx, f"No role menus yet. Start with `{self.bot.prefix}rr post #channel Title`.")
        embed = discord.Embed(title="Reaction role menus", color=self.bot.main_color)
        for menu in list(self.menus.values())[:25]:
            guild = self.bot.get_guild(int(menu["guild_id"]))
            link = f"https://discord.com/channels/{menu['guild_id']}/{menu['channel_id']}/{menu['message_id']}"
            embed.add_field(
                name=f"{menu.get('title') or 'Message'} ({menu.get('mode', 'normal')})",
                value=f"[Jump to message]({link}) in <#{menu['channel_id']}>\n{self.describe(menu, guild)}"[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    @rr.command(name="delete")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def rr_delete(self, ctx, message: discord.Message):
        """Stop a message giving roles. Menus the bot posted are deleted."""
        try:
            menu = await self._menu_for(ctx, message)
        except ReactionError as e:
            return await self._err(ctx, str(e))
        await self.delete_menu(menu, delete_message=True)
        await self._ok(ctx, "That message no longer gives roles. Members keep the roles they have.", "Menu deleted")

    @commands.group(name="autoreact", aliases=["autoreaction", "autoreactions"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def autoreact(self, ctx):
        """Auto reactions: the bot reacts to every new message in a channel."""
        await ctx.send_help(ctx.command)

    @autoreact.command(name="set", aliases=["add"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def autoreact_set(self, ctx, channel: discord.TextChannel, *emojis: str):
        """
        React to every new message in a channel with these emojis.

        **Example:**
        `{prefix}autoreact set #suggestions 👍 👎`
        """
        try:
            doc = await self.set_auto(channel, list(emojis))
        except ReactionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, f"New messages in {channel.mention} get {' '.join(doc['emojis'])}.", "Auto reactions on")

    @autoreact.command(name="bots")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def autoreact_bots(self, ctx, channel: discord.TextChannel, setting: str.lower):
        """Also react to messages from bots and webhooks in that channel (on/off)."""
        doc = self.auto.get(channel.id)
        if doc is None:
            return await self._err(ctx, f"{channel.mention} doesn't have auto reactions. Use `{self.bot.prefix}autoreact set` first.")
        await self.set_auto(channel, doc["emojis"], include_bots=setting in ("on", "yes", "true", "1"))
        await self._ok(ctx, f"Bots' messages in {channel.mention} {'get' if self.auto[channel.id]['include_bots'] else 'no longer get'} reactions.")

    @autoreact.command(name="remove", aliases=["stop", "delete"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def autoreact_remove(self, ctx, channel: discord.TextChannel):
        """Stop reacting in a channel."""
        try:
            await self.remove_auto(channel.id)
        except ReactionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, f"The bot no longer reacts in {channel.mention}.", "Auto reactions off")

    @autoreact.command(name="list")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def autoreact_list(self, ctx):
        """List channels with auto reactions."""
        if not self.auto:
            return await self._ok(ctx, f"No channels yet. Try `{self.bot.prefix}autoreact set #channel 👍`.")
        lines = [f"<#{d['channel_id']}> {' '.join(d['emojis'])}{' (bots too)' if d.get('include_bots') else ''}" for d in self.auto.values()]
        await self._ok(ctx, "\n".join(lines), "Auto reactions")


async def setup(bot):
    await bot.add_cog(Reactions(bot))
