"""
Suggestions for Modmail.

Members press a "Make a suggestion" button, fill in a short form, and the bot
posts their suggestion for everyone to vote on (with a discussion thread).
Staff mark suggestions as under review, approved, denied or done, and the
member gets a DM about it.

Setup (Administrator):
    ?suggestions setup #channel          where suggestions go; posts the button there
    ?suggestions panel [#channel]        post the button again (or in another channel)
    ?suggestions show                    all settings and the form's questions
    ?suggestions set <setting> <value>   change wording or options
    ?suggestions question ...            change the form's questions
    ?suggestions enable / disable        open or close suggestions

Staff (Moderator):
    ?suggestion approve <number> [reason]
    ?suggestion deny <number> [reason]
    ?suggestion consider <number> [reason]
    ?suggestion done <number> [reason]
    ?suggestion reopen <number>
    ?suggestion delete <number>
    ?suggestion list [status]

Members:
    ?suggest <idea>                      quick suggestion without the form

Everything can also be managed from the web dashboard (Suggestions).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel, getLogger

logger = getLogger(__name__)

BUTTON_ID = "mmsuggest:new"

STATUSES = {
    "open": {"label": "Open", "color": None, "verb": "is open again"},
    "considering": {"label": "Under review", "color": 0xF1C40F, "verb": "is now under review"},
    "approved": {"label": "Approved", "color": 0x57F287, "verb": "was approved"},
    "denied": {"label": "Denied", "color": 0xED4245, "verb": "was denied"},
    "done": {"label": "Done", "color": 0x1ABC9C, "verb": "is done"},
}
FINAL = {"approved", "denied", "done"}

DEFAULT_QUESTIONS = [
    {"label": "What's your suggestion?", "placeholder": "e.g. Add a channel for phone photos", "style": "short", "required": True},
    {"label": "Tell us more", "placeholder": "What should it do, and how would it work?", "style": "long", "required": True},
    {"label": "Why would this help?", "placeholder": "Optional", "style": "long", "required": False},
]

DEFAULTS = {
    "enabled": True,
    "channel_id": None,  # where suggestions are posted
    "panel_channel_id": None,  # where the button message is
    "panel_message_id": None,
    "panel_title": "Got an idea?",
    "panel_text": "Press the button below to suggest something for {server}. Staff read every suggestion, and everyone can vote.",
    "button_label": "Make a suggestion",
    "form_title": "Make a suggestion",
    "thanks": "Thanks! Your suggestion was posted in {channel}.",
    "dm_text": "Your suggestion **#{number}** in **{server}** {status}.",
    "votes": True,
    "vote_up": "👍",
    "vote_down": "👎",
    "threads": True,
    "dm_updates": True,
    "anonymous": False,
    "sticky_panel": True,
    "cooldown": 300,  # seconds between suggestions per member
    "questions": DEFAULT_QUESTIONS,
    "next_number": 1,
}

# What each setting is, for ?suggestions show/set and the dashboard.
SETTING_HELP = {
    "panel_title": "Title of the button message.",
    "panel_text": "Text of the button message. {server} is the server name.",
    "button_label": "The button's label (up to 80 characters).",
    "form_title": "Title at the top of the form (up to 45 characters).",
    "thanks": "Shown to the member (only to them) after they submit. {channel} is the suggestions channel.",
    "dm_text": "DM sent when staff change a suggestion. {number}, {server} and {status} (e.g. \"was approved\"); the reason is added below.",
    "votes": "Add vote reactions to each suggestion (on/off).",
    "vote_up": "Emoji for voting yes.",
    "vote_down": "Emoji for voting no.",
    "threads": "Start a discussion thread on each suggestion (on/off).",
    "dm_updates": "DM the member when staff change their suggestion (on/off).",
    "anonymous": "Hide who made each suggestion (on/off).",
    "sticky_panel": "Keep the button message at the bottom of the suggestions channel (on/off).",
    "cooldown": "Minutes a member must wait between suggestions (0 for none).",
}
BOOL_KEYS = {"votes", "threads", "dm_updates", "anonymous", "sticky_panel"}
LIMITS = {"panel_title": 256, "panel_text": 2000, "button_label": 80, "form_title": 45, "thanks": 500, "dm_text": 1000, "vote_up": 64, "vote_down": 64}
MAX_QUESTIONS = 5


class SuggestionError(Exception):
    """A problem to show to the person (in Discord or on the dashboard)."""


def to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("on", "yes", "true", "1", "enable", "enabled"):
        return True
    if text in ("off", "no", "false", "0", "disable", "disabled"):
        return False
    raise SuggestionError("Use on or off.")


class SuggestionForm(discord.ui.Modal):
    def __init__(self, cog: "Suggestions"):
        super().__init__(title=cog.settings["form_title"][:45] or "Make a suggestion", timeout=900)
        self.cog = cog
        self.inputs: List[discord.ui.TextInput] = []
        self.labels: List[str] = []
        for q in cog.settings["questions"][:MAX_QUESTIONS]:
            field = discord.ui.TextInput(
                label=q["label"][:45],
                placeholder=(q.get("placeholder") or None) and q["placeholder"][:100],
                style=discord.TextStyle.paragraph if q.get("style") == "long" else discord.TextStyle.short,
                required=bool(q.get("required", True)),
                max_length=1000 if q.get("style") == "long" else 200,
            )
            self.inputs.append(field)
            self.labels.append(q["label"][:45])
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        answers = [{"label": label, "value": str(q.value or "").strip()} for label, q in zip(self.labels, self.inputs)]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            doc = await self.cog.create(interaction.user, answers)
        except SuggestionError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        channel = self.cog.bot.get_channel(int(doc["channel_id"]))
        await interaction.followup.send(self.cog.fill(self.cog.settings["thanks"], channel=channel.mention if channel else "the suggestions channel"), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error("Suggestion form failed.", exc_info=error)
        try:
            await interaction.followup.send("Something went wrong posting your suggestion. Please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


class Suggestions(commands.Cog):
    """Members suggest ideas with a form; everyone votes; staff respond."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.settings: dict = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
        self.last_used: Dict[int, float] = {}

    async def cog_load(self):
        doc = await self.db.find_one({"_id": "config"})
        if doc:
            self.settings.update({k: v for k, v in doc.items() if k in DEFAULTS})

    async def save_settings(self):
        await self.db.replace_one({"_id": "config"}, {"_id": "config", **self.settings}, upsert=True)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @property
    def guild(self) -> Optional[discord.Guild]:
        channel = self.channel()
        return channel.guild if channel else self.bot.guild

    def channel(self) -> Optional[discord.TextChannel]:
        cid = self.settings.get("channel_id")
        return self.bot.get_channel(int(cid)) if cid else None

    def fill(self, text: str, **values) -> str:
        values.setdefault("server", self.guild.name if self.guild else "the server")
        for k, v in values.items():
            text = text.replace("{" + k + "}", str(v))
        return text

    def check_channel(self, channel: discord.TextChannel, *, panel=False):
        perms = channel.permissions_for(channel.guild.me)
        need = ["view_channel", "send_messages", "embed_links"]
        if not panel:
            if self.settings["votes"]:
                need += ["add_reactions", "read_message_history"]
            if self.settings["threads"]:
                need += ["create_public_threads"]
        missing = [n for n in need if not getattr(perms, n, False)]
        if missing:
            names = ", ".join(n.replace("_", " ").title() for n in missing)
            raise SuggestionError(f"The bot needs {names} in #{channel.name}.")

    def embed_for(self, doc: dict) -> discord.Embed:
        status = STATUSES.get(doc.get("status", "open"), STATUSES["open"])
        answers = doc.get("answers") or []
        title = answers[0]["value"] if answers else "Suggestion"
        embed = discord.Embed(title=title[:256], color=status["color"] or self.bot.main_color)
        parts = [f"**{a['label']}**\n{a['value']}" for a in answers[1:] if a.get("value")]
        embed.description = "\n\n".join(parts)[:4000] or None
        if not self.settings["anonymous"] and doc.get("author_name"):
            embed.set_author(name=doc["author_name"], icon_url=doc.get("author_avatar") or None)
        line = f"**{status['label']}**"
        if doc.get("reason"):
            line += f": {doc['reason']}"
        embed.add_field(name="Status", value=line[:1024], inline=False)
        embed.set_footer(text=f"Suggestion #{doc['number']}")
        embed.timestamp = datetime.fromisoformat(doc["created"]) if doc.get("created") else None
        return embed

    def panel_embed(self) -> discord.Embed:
        return discord.Embed(
            title=self.settings["panel_title"][:256],
            description=self.fill(self.settings["panel_text"])[:4000],
            color=self.bot.main_color,
        )

    def panel_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=self.settings["button_label"][:80] or "Make a suggestion",
                                        style=discord.ButtonStyle.primary, custom_id=BUTTON_ID, emoji="💡"))
        return view

    def link(self, doc: dict) -> str:
        guild = self.bot.get_channel(int(doc["channel_id"]))
        gid = guild.guild.id if guild else "@me"
        return f"https://discord.com/channels/{gid}/{doc['channel_id']}/{doc['message_id']}"

    async def get(self, number) -> dict:
        try:
            number = int(str(number).lstrip("#"))
        except ValueError:
            raise SuggestionError("Give the suggestion's number, e.g. `12`.")
        doc = await self.db.find_one({"_id": f"s-{number}"})
        if doc is None:
            raise SuggestionError(f"There's no suggestion #{number}.")
        return doc

    async def recent(self, status: Optional[str] = None, limit: int = 50) -> List[dict]:
        query = {"type": "suggestion"}
        if status:
            query["status"] = status
        docs = await self.db.find(query).to_list(None)
        docs.sort(key=lambda d: d["number"], reverse=True)
        return docs[:limit]

    # ------------------------------------------------------------------ #
    # Actions (used by commands, the button and the dashboard)
    # ------------------------------------------------------------------ #
    async def post_panel(self, channel: discord.TextChannel) -> discord.Message:
        self.check_channel(channel, panel=True)
        await self.remove_panel()
        message = await channel.send(embed=self.panel_embed(), view=self.panel_view())
        self.settings["panel_channel_id"] = str(channel.id)
        self.settings["panel_message_id"] = str(message.id)
        await self.save_settings()
        return message

    async def remove_panel(self):
        cid, mid = self.settings.get("panel_channel_id"), self.settings.get("panel_message_id")
        if not (cid and mid):
            return
        channel = self.bot.get_channel(int(cid))
        if channel is not None:
            try:
                await channel.get_partial_message(int(mid)).delete()
            except discord.HTTPException:
                pass
        self.settings["panel_message_id"] = None

    async def refresh_panel(self):
        """Show new wording on the existing button message."""
        cid, mid = self.settings.get("panel_channel_id"), self.settings.get("panel_message_id")
        channel = self.bot.get_channel(int(cid)) if cid else None
        if channel is None or not mid:
            return
        try:
            await channel.get_partial_message(int(mid)).edit(embed=self.panel_embed(), view=self.panel_view())
        except discord.HTTPException as e:
            logger.warning("Couldn't update the suggestions button message: %s", e)

    async def setup_channel(self, channel: discord.TextChannel) -> discord.Message:
        self.check_channel(channel)
        self.settings["channel_id"] = str(channel.id)
        await self.save_settings()
        return await self.post_panel(channel)

    async def create(self, member: discord.abc.User, answers: List[dict]) -> dict:
        if not self.settings["enabled"]:
            raise SuggestionError("Suggestions are closed right now.")
        channel = self.channel()
        if channel is None:
            raise SuggestionError("Suggestions aren't set up yet. Ask staff to run `suggestions setup`.")
        if str(member.id) in self.bot.blocked_users:
            raise SuggestionError("You can't make suggestions right now.")
        answers = [a for a in answers if a.get("value")]
        if not answers:
            raise SuggestionError("Write your suggestion first.")
        wait = self.settings["cooldown"] - (time.monotonic() - self.last_used.get(member.id, -1e9))
        if self.settings["cooldown"] and wait > 0:
            mins = max(1, round(wait / 60))
            raise SuggestionError(f"You can make another suggestion in {mins} minute{'s' if mins != 1 else ''}.")
        self.check_channel(channel)

        number = int(self.settings.get("next_number") or 1)
        while await self.db.find_one({"_id": f"s-{number}"}):
            number += 1
        self.settings["next_number"] = number + 1
        await self.save_settings()
        doc = {
            "_id": f"s-{number}", "type": "suggestion", "number": number,
            "author_id": str(member.id), "author_name": getattr(member, "display_name", str(member)),
            "author_avatar": str(member.display_avatar.url) if getattr(member, "display_avatar", None) else None,
            "answers": answers, "status": "open", "reason": None, "channel_id": str(channel.id),
            "message_id": None, "thread_id": None, "created": datetime.now(timezone.utc).isoformat(),
        }
        message = await channel.send(embed=self.embed_for(doc))
        doc["message_id"] = str(message.id)
        self.last_used[member.id] = time.monotonic()
        if self.settings["votes"]:
            for e in (self.settings["vote_up"], self.settings["vote_down"]):
                if e:
                    try:
                        await message.add_reaction(e)
                    except discord.HTTPException as err:
                        logger.warning("Couldn't add vote reaction %s: %s", e, err)
        if self.settings["threads"]:
            try:
                thread = await message.create_thread(name=f"#{number} {answers[0]['value']}"[:100], auto_archive_duration=10080)
                doc["thread_id"] = str(thread.id)
            except discord.HTTPException as err:
                logger.warning("Couldn't start a thread for suggestion #%s: %s", number, err)
        await self.db.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        # Keep the button easy to find under the newest suggestion.
        if self.settings["sticky_panel"] and self.settings.get("panel_channel_id") == str(channel.id):
            try:
                await self.post_panel(channel)
            except (SuggestionError, discord.HTTPException) as err:
                logger.warning("Couldn't move the suggestions button down: %s", err)
        return doc

    async def set_status(self, number, status: str, reason: Optional[str] = None) -> dict:
        if status not in STATUSES:
            raise SuggestionError("Status must be open, considering, approved, denied or done.")
        doc = await self.get(number)
        doc["status"] = status
        doc["reason"] = (reason or "").strip()[:900] or None
        doc["updated"] = datetime.now(timezone.utc).isoformat()
        await self.db.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        channel = self.bot.get_channel(int(doc["channel_id"]))
        if channel is not None and doc.get("message_id"):
            try:
                await channel.get_partial_message(int(doc["message_id"])).edit(embed=self.embed_for(doc))
            except discord.HTTPException as e:
                logger.warning("Couldn't update suggestion #%s: %s", doc["number"], e)
        if doc.get("thread_id"):
            thread = self.bot.get_channel(int(doc["thread_id"]))
            if isinstance(thread, discord.Thread):
                try:
                    await thread.edit(archived=status in FINAL, locked=status in FINAL)
                except discord.HTTPException:
                    pass
        if self.settings["dm_updates"]:
            await self._dm_author(doc)
        return doc

    async def _dm_author(self, doc: dict):
        user = self.bot.get_user(int(doc["author_id"]))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(doc["author_id"]))
            except discord.HTTPException:
                return
        text = self.fill(self.settings["dm_text"], number=doc["number"], status=STATUSES[doc["status"]]["verb"])
        embed = discord.Embed(description=text, color=STATUSES[doc["status"]]["color"] or self.bot.main_color)
        title = (doc.get("answers") or [{}])[0].get("value")
        if title:
            embed.add_field(name="Your suggestion", value=title[:1024], inline=False)
        if doc.get("reason"):
            embed.add_field(name="Reason", value=doc["reason"][:1024], inline=False)
        if doc.get("message_id"):
            embed.add_field(name="​", value=f"[View it]({self.link(doc)})", inline=False)
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            pass  # their DMs are closed

    async def delete(self, number):
        doc = await self.get(number)
        await self.db.delete_one({"_id": doc["_id"]})
        channel = self.bot.get_channel(int(doc["channel_id"]))
        if channel is not None and doc.get("message_id"):
            try:
                await channel.get_partial_message(int(doc["message_id"])).delete()
            except discord.HTTPException:
                pass
        if doc.get("thread_id"):
            thread = self.bot.get_channel(int(doc["thread_id"]))
            if thread is not None:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass
        return doc

    async def update_settings(self, changes: dict):
        """Validate and save several settings at once (all or nothing)."""
        new = dict(self.settings)
        for key, value in changes.items():
            if key == "questions":
                new["questions"] = self.clean_questions(value)
            elif key == "enabled":
                new["enabled"] = to_bool(value)
            elif key in BOOL_KEYS:
                new[key] = to_bool(value)
            elif key == "cooldown":
                try:
                    minutes = float(value)
                except (TypeError, ValueError):
                    raise SuggestionError("Cooldown is a number of minutes.")
                if not 0 <= minutes <= 1440:
                    raise SuggestionError("Cooldown must be between 0 and 1440 minutes.")
                new["cooldown"] = int(minutes * 60)
            elif key in LIMITS:
                text = str(value or "").strip()
                if not text and key not in ("vote_up", "vote_down"):
                    raise SuggestionError(f"{key.replace('_', ' ').capitalize()} can't be empty.")
                if len(text) > LIMITS[key]:
                    raise SuggestionError(f"{key.replace('_', ' ').capitalize()} can be up to {LIMITS[key]} characters.")
                new[key] = text
            else:
                raise SuggestionError(f"There's no setting called `{key}`.")
        self.settings = new
        await self.save_settings()
        if changes.keys() & {"panel_title", "panel_text", "button_label"}:
            await self.refresh_panel()

    @staticmethod
    def clean_questions(questions) -> List[dict]:
        if not isinstance(questions, list) or not questions:
            raise SuggestionError("The form needs at least one question.")
        if len(questions) > MAX_QUESTIONS:
            raise SuggestionError(f"Discord forms can have up to {MAX_QUESTIONS} questions.")
        out = []
        for i, q in enumerate(questions):
            label = str((q or {}).get("label", "")).strip()
            if not label:
                raise SuggestionError(f"Question {i + 1} needs a label.")
            if len(label) > 45:
                raise SuggestionError(f"Question {i + 1} is too long (Discord allows 45 characters).")
            placeholder = str(q.get("placeholder") or "").strip()
            if len(placeholder) > 100:
                raise SuggestionError(f"Question {i + 1}'s hint is too long (100 characters max).")
            out.append({"label": label, "placeholder": placeholder, "style": "long" if q.get("style") == "long" else "short",
                        "required": True if i == 0 else bool(q.get("required", True))})
        return out

    # ------------------------------------------------------------------ #
    # The button
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        if (interaction.data or {}).get("custom_id") != BUTTON_ID:
            return
        reason = None
        if not self.settings["enabled"]:
            reason = "Suggestions are closed right now."
        elif self.channel() is None:
            reason = "Suggestions aren't set up yet."
        elif str(interaction.user.id) in self.bot.blocked_users:
            reason = "You can't make suggestions right now."
        else:
            wait = self.settings["cooldown"] - (time.monotonic() - self.last_used.get(interaction.user.id, -1e9))
            if self.settings["cooldown"] and wait > 0:
                mins = max(1, round(wait / 60))
                reason = f"You can make another suggestion in {mins} minute{'s' if mins != 1 else ''}."
        if reason:
            return await interaction.response.send_message(reason, ephemeral=True)
        await interaction.response.send_modal(SuggestionForm(self))

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def _ok(self, ctx, text, title=None):
        await ctx.send(embed=discord.Embed(title=title, description=text, color=self.bot.main_color))

    async def _err(self, ctx, text):
        await ctx.send(embed=discord.Embed(title="Error", description=text, color=self.bot.error_color))

    @commands.command(name="suggest")
    @checks.has_permissions(PermissionLevel.REGULAR)
    @commands.guild_only()
    async def suggest(self, ctx, *, idea: str):
        """
        Make a quick suggestion without the form.

        **Example:**
        `{prefix}suggest Add a channel for phone photos`
        """
        try:
            doc = await self.create(ctx.author, [{"label": "Suggestion", "value": idea[:200]}] +
                                    ([{"label": "Details", "value": idea}] if len(idea) > 200 else []))
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            await self._ok(ctx, f"Posted as [suggestion #{doc['number']}]({self.link(doc)}).")

    @commands.group(name="suggestions", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def suggestions(self, ctx):
        """Set up suggestions: a button members press to suggest ideas."""
        await ctx.send_help(ctx.command)

    @suggestions.command(name="setup")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def suggestions_setup(self, ctx, channel: discord.TextChannel):
        """
        Choose where suggestions go, and post the button there.

        **Example:**
        `{prefix}suggestions setup #suggestions`
        """
        try:
            await self.setup_channel(channel)
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        p = self.bot.prefix
        await self._ok(ctx, f"Suggestions now go to {channel.mention}, and the button is posted there.\n\n"
                            f"Change the wording with `{p}suggestions set`, or the form's questions with `{p}suggestions question`. "
                            f"See everything with `{p}suggestions show`.", "Suggestions are set up")

    @suggestions.command(name="panel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def suggestions_panel(self, ctx, channel: discord.TextChannel = None):
        """Post the button message again, here or in another channel (the old one is removed)."""
        channel = channel or self.channel() or ctx.channel
        try:
            if self.channel() is None:
                raise SuggestionError(f"Run `{self.bot.prefix}suggestions setup #channel` first.")
            await self.post_panel(channel)
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, f"Posted the button in {channel.mention}.")

    @suggestions.command(name="show")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def suggestions_show(self, ctx):
        """Show every setting and the form's questions."""
        s = self.settings
        ch = self.channel()
        embed = discord.Embed(title="Suggestions", color=self.bot.main_color,
                              description=f"**{'On' if s['enabled'] else 'Off'}** · posting in {ch.mention if ch else 'nowhere yet'}")
        for key in SETTING_HELP:
            value = s[key]
            if key in BOOL_KEYS:
                value = "on" if value else "off"
            elif key == "cooldown":
                value = f"{round(value / 60, 1):g} minutes"
            embed.add_field(name=key, value=f"{str(value)[:200]}", inline=key in BOOL_KEYS or key in ("cooldown", "vote_up", "vote_down"))
        qs = "\n".join(f"{i}. {q['label']} ({q['style']}{', optional' if not q['required'] else ''})" for i, q in enumerate(s["questions"], 1))
        embed.add_field(name="Form questions", value=qs, inline=False)
        embed.set_footer(text=f"Change one with {self.bot.prefix}suggestions set <setting> <value>")
        await ctx.send(embed=embed)

    @suggestions.command(name="set")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def suggestions_set(self, ctx, setting: str.lower = None, *, value: str = None):
        """
        Change wording or an option. Run it with no setting to see the list.

        **Examples:**
        `{prefix}suggestions set panel_title Have an idea for RingNet?`
        `{prefix}suggestions set threads off`
        `{prefix}suggestions set cooldown 10`
        """
        if not setting or value is None:
            lines = [f"`{k}`: {v}" for k, v in SETTING_HELP.items()]
            return await self._ok(ctx, "\n".join(lines), "Settings you can change")
        try:
            await self.update_settings({setting: value})
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, f"Saved `{setting}`.")

    @suggestions.command(name="enable")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def suggestions_enable(self, ctx):
        """Open suggestions."""
        await self.update_settings({"enabled": True})
        await self._ok(ctx, "Suggestions are open.")

    @suggestions.command(name="disable")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def suggestions_disable(self, ctx):
        """Close suggestions. The button stays, but tells members they're closed."""
        await self.update_settings({"enabled": False})
        await self._ok(ctx, "Suggestions are closed. Members pressing the button are told so.")

    @suggestions.group(name="question", aliases=["questions"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def question(self, ctx):
        """
        Change the form's questions (up to 5). The first answer is the suggestion's title.

        `{prefix}suggestions question add short|long <question> | <hint>`
        `{prefix}suggestions question edit <number> <question> | <hint>`
        `{prefix}suggestions question optional <number>` / `required <number>`
        `{prefix}suggestions question remove <number>`
        `{prefix}suggestions question reset`
        """
        qs = "\n".join(f"{i}. **{q['label']}** ({q['style']}{', optional' if not q['required'] else ''})"
                       + (f"\n  _{q['placeholder']}_" if q.get("placeholder") else "") for i, q in enumerate(self.settings["questions"], 1))
        await self._ok(ctx, qs + f"\n\nSee `{self.bot.prefix}help suggestions question` to change them.", "Form questions")

    async def _questions(self, ctx, new):
        try:
            await self.update_settings({"questions": new})
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        await self.question.callback(self, ctx)

    def _q_index(self, number) -> int:
        if not 1 <= number <= len(self.settings["questions"]):
            raise SuggestionError(f"Pick a question from 1 to {len(self.settings['questions'])}.")
        return number - 1

    @question.command(name="add")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def question_add(self, ctx, style: str.lower, *, text: str):
        """Add a question: `short` (one line) or `long` (paragraph), then the question, then `|` and an optional hint."""
        if style not in ("short", "long"):
            return await self._err(ctx, "Start with `short` (one line) or `long` (a paragraph).")
        label, _, hint = text.partition("|")
        await self._questions(ctx, [*self.settings["questions"], {"label": label.strip(), "placeholder": hint.strip(), "style": style, "required": True}])

    @question.command(name="edit")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def question_edit(self, ctx, number: int, *, text: str):
        """Reword a question (and its hint after `|`)."""
        try:
            i = self._q_index(number)
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        label, sep, hint = text.partition("|")
        qs = [dict(q) for q in self.settings["questions"]]
        qs[i]["label"] = label.strip()
        if sep:
            qs[i]["placeholder"] = hint.strip()
        await self._questions(ctx, qs)

    @question.command(name="optional")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def question_optional(self, ctx, number: int):
        """Let members skip a question (not the first one)."""
        try:
            i = self._q_index(number)
            if i == 0:
                raise SuggestionError("The first question is the suggestion's title, so it's always required.")
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        qs = [dict(q) for q in self.settings["questions"]]
        qs[i]["required"] = False
        await self._questions(ctx, qs)

    @question.command(name="required")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def question_required(self, ctx, number: int):
        """Make a question required."""
        try:
            i = self._q_index(number)
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        qs = [dict(q) for q in self.settings["questions"]]
        qs[i]["required"] = True
        await self._questions(ctx, qs)

    @question.command(name="remove")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def question_remove(self, ctx, number: int):
        """Remove a question."""
        try:
            i = self._q_index(number)
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        await self._questions(ctx, [q for j, q in enumerate(self.settings["questions"]) if j != i])

    @question.command(name="reset")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def question_reset(self, ctx):
        """Go back to the default questions."""
        await self._questions(ctx, [dict(q) for q in DEFAULT_QUESTIONS])

    # ---- staff ---------------------------------------------------------- #
    @commands.group(name="suggestion", aliases=["sg"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion(self, ctx):
        """Respond to suggestions: approve, deny, consider, done, reopen, delete, list."""
        await ctx.send_help(ctx.command)

    async def _status(self, ctx, number, status, reason):
        try:
            doc = await self.set_status(number, status, reason)
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        title = (doc.get("answers") or [{}])[0].get("value", "")
        await self._ok(ctx, f"[Suggestion #{doc['number']}]({self.link(doc)}) {STATUSES[status]['verb']}.\n> {title[:200]}"
                            + ("\nThe member was sent a DM." if self.settings["dm_updates"] and status != "open" else ""))

    @suggestion.command(name="approve")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion_approve(self, ctx, number: str, *, reason: str = None):
        """Approve a suggestion, with an optional reason the member sees."""
        await self._status(ctx, number, "approved", reason)

    @suggestion.command(name="deny")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion_deny(self, ctx, number: str, *, reason: str = None):
        """Deny a suggestion, with an optional reason the member sees."""
        await self._status(ctx, number, "denied", reason)

    @suggestion.command(name="consider", aliases=["review"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion_consider(self, ctx, number: str, *, reason: str = None):
        """Mark a suggestion as under review."""
        await self._status(ctx, number, "considering", reason)

    @suggestion.command(name="done", aliases=["implemented"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion_done(self, ctx, number: str, *, reason: str = None):
        """Mark a suggestion as done."""
        await self._status(ctx, number, "done", reason)

    @suggestion.command(name="reopen")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion_reopen(self, ctx, number: str):
        """Set a suggestion back to open."""
        await self._status(ctx, number, "open", None)

    @suggestion.command(name="delete")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion_delete(self, ctx, number: str):
        """Delete a suggestion and its discussion thread."""
        try:
            doc = await self.delete(number)
        except SuggestionError as e:
            return await self._err(ctx, str(e))
        await self._ok(ctx, f"Deleted suggestion #{doc['number']}.")

    @suggestion.command(name="list")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def suggestion_list(self, ctx, status: str.lower = None):
        """List recent suggestions, optionally only one status (open, considering, approved, denied, done)."""
        if status and status not in STATUSES:
            return await self._err(ctx, "Status must be open, considering, approved, denied or done.")
        docs = await self.recent(status, 15)
        if not docs:
            return await self._ok(ctx, "No suggestions" + (f" marked {STATUSES[status]['label'].lower()}." if status else " yet."))
        lines = [f"[#{d['number']}]({self.link(d)}) **{STATUSES[d['status']]['label']}**: {(d['answers'][0]['value'] if d.get('answers') else '')[:80]}" for d in docs]
        await self._ok(ctx, "\n".join(lines), "Recent suggestions")


async def setup(bot):
    await bot.add_cog(Suggestions(bot))
