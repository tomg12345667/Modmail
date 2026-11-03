"""
Embed Builder plugin for Modmail.

Build, preview, post and edit embeds with buttons, and save them as templates.

Commands (Administrator level by default):
    ?embed create [#channel]              open the builder
    ?embed load <template> [#channel]     open the builder from a saved template
    ?embed edit <message link>            edit an embed the bot already posted
    ?embed json [#channel] <json>         open the builder from JSON (e.g. Discohook)
    ?embed export <message link>          get a message's embed as JSON
    ?embed post <template> [#channel]     post a saved template straight away
    ?embed templates                      list saved templates
    ?embed deltemplate <name>             delete a template
"""

from __future__ import annotations

import copy
import io
import json
import re
from typing import List, Optional, Tuple

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel, getLogger

try:  # Present on bots with private-network image support; optional otherwise.
    from core.localicon import _fetch as _fetch_local, is_local as _is_local
except Exception:  # pragma: no cover
    _fetch_local = None
    _is_local = None

logger = getLogger(__name__)

# Discord's limits
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELDS = 25
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048
MAX_AUTHOR = 256
MAX_TOTAL = 6000
MAX_CONTENT = 2000
MODAL_MAX = 4000  # a single modal text box can hold at most 4000 characters

URL_RE = re.compile(r"^https?://\S+$", re.I)

COLOR_NAMES = {
    "red": 0xED4245,
    "green": 0x57F287,
    "blue": 0x3498DB,
    "blurple": 0x5865F2,
    "yellow": 0xFEE75C,
    "gold": 0xF1C40F,
    "orange": 0xE67E22,
    "purple": 0x9B59B6,
    "pink": 0xEB459E,
    "teal": 0x1ABC9C,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
    "white": 0xFFFFFF,
    "black": 0x010101,  # pure 0 means "no colour" to Discord
}

# Where image URLs can live inside an embed dict: (top-level key, sub-key, label)
IMAGE_SLOTS = [
    ("image", "url", "image"),
    ("thumbnail", "url", "thumbnail"),
    ("author", "icon_url", "author_icon"),
    ("footer", "icon_url", "footer_icon"),
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def blank_state() -> dict:
    return {"content": "", "embed": {}}


def parse_color(text: str) -> Optional[int]:
    text = (text or "").strip().lower()
    if not text or text in ("none", "default"):
        return None
    if text in COLOR_NAMES:
        return COLOR_NAMES[text]
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            r, g, b = (int(p) for p in parts)
            return (r << 16) + (g << 8) + b
        raise ValueError("RGB colours look like `88, 101, 242`.")
    hexpart = text.lstrip("#")
    if hexpart.startswith("0x"):
        hexpart = hexpart[2:]
    if len(hexpart) == 3 and all(c in "0123456789abcdef" for c in hexpart):
        hexpart = "".join(c * 2 for c in hexpart)
    if len(hexpart) == 6 and all(c in "0123456789abcdef" for c in hexpart):
        return int(hexpart, 16)
    raise ValueError(f"`{text}` isn't a colour. Use a hex code like `#5865F2` or a name like `blue`.")


def format_color(value) -> str:
    return f"#{value:06X}" if isinstance(value, int) else ""


def check_url(url: str, what: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if URL_RE.match(url):
        return url
    if _is_local is not None and _is_local(url):
        return url  # file path or LAN address; uploaded by the bot when posting
    raise ValueError(f"The {what} must be a link starting with `https://`.")


def embed_is_empty(d: dict) -> bool:
    return not any(
        [
            d.get("title"),
            d.get("description"),
            d.get("fields"),
            (d.get("image") or {}).get("url"),
            (d.get("thumbnail") or {}).get("url"),
            (d.get("author") or {}).get("name"),
            (d.get("footer") or {}).get("text"),
        ]
    )


def embed_length(d: dict) -> int:
    n = len(d.get("title") or "") + len(d.get("description") or "")
    n += len((d.get("footer") or {}).get("text") or "") + len((d.get("author") or {}).get("name") or "")
    for f in d.get("fields") or []:
        n += len(f.get("name") or "") + len(f.get("value") or "")
    return n


def validate(state: dict) -> None:
    d = state.get("embed") or {}
    problems = []
    if len(state.get("content") or "") > MAX_CONTENT:
        problems.append(f"Message text is over {MAX_CONTENT} characters.")
    if len(d.get("title") or "") > MAX_TITLE:
        problems.append(f"Title is over {MAX_TITLE} characters.")
    if len(d.get("description") or "") > MAX_DESCRIPTION:
        problems.append(f"Description is over {MAX_DESCRIPTION} characters.")
    if len(d.get("fields") or []) > MAX_FIELDS:
        problems.append(f"An embed can have at most {MAX_FIELDS} fields.")
    for i, f in enumerate(d.get("fields") or [], 1):
        if not f.get("name") or not f.get("value"):
            problems.append(f"Field {i} needs both a name and a value.")
        if len(f.get("name") or "") > MAX_FIELD_NAME or len(f.get("value") or "") > MAX_FIELD_VALUE:
            problems.append(f"Field {i} is too long.")
    if len((d.get("footer") or {}).get("text") or "") > MAX_FOOTER:
        problems.append("Footer is too long.")
    if len((d.get("author") or {}).get("name") or "") > MAX_AUTHOR:
        problems.append("Author name is too long.")
    if embed_length(d) > MAX_TOTAL:
        problems.append(f"All the embed text together is over Discord's {MAX_TOTAL}-character limit.")
    if embed_is_empty(d) and not (state.get("content") or "").strip():
        problems.append("There's nothing to post yet. Add some text first.")
    if problems:
        raise ValueError("\n".join(f"- {p}" for p in problems))


def clean_embed_dict(d: dict) -> dict:
    """Drop empty parts so Discord accepts the embed."""
    d = copy.deepcopy(d)
    for key in ("title", "description", "url"):
        if not d.get(key):
            d.pop(key, None)
    for key, sub, _ in IMAGE_SLOTS:
        part = d.get(key)
        if isinstance(part, dict):
            part = {k: v for k, v in part.items() if v and not k.startswith("proxy_")}
            if key in ("image", "thumbnail") and not part.get("url"):
                part = {}
            if key == "author" and not part.get("name"):
                part = {}
            if key == "footer" and not part.get("text"):
                part = {}
            if part:
                d[key] = part
            else:
                d.pop(key, None)
    if d.get("color") is None:
        d.pop("color", None)
    if not d.get("fields"):
        d.pop("fields", None)
    if not d.get("timestamp"):
        d.pop("timestamp", None)
    for k in ("type", "video", "provider", "proxy_url", "height", "width"):
        d.pop(k, None)
    for key in ("image", "thumbnail"):
        if key in d:
            d[key] = {"url": d[key]["url"]}
    return d


def state_from_message(message: discord.Message) -> dict:
    d = message.embeds[0].to_dict() if message.embeds else {}
    return {"content": message.content or "", "embed": clean_embed_dict(d)}


def state_from_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"That isn't valid JSON ({e.msg}, line {e.lineno}).")
    if isinstance(data, dict) and isinstance(data.get("messages"), list) and data["messages"]:
        data = data["messages"][0].get("data", {})  # Discohook share format
    if isinstance(data, list):
        data = {"embeds": data}
    if not isinstance(data, dict):
        raise ValueError("The JSON should be an object, like `{\"title\": \"Hello\"}`.")
    if "embeds" in data or "content" in data:
        embeds = data.get("embeds") or []
        embed = embeds[0] if embeds else {}
        content = data.get("content") or ""
    else:
        embed, content = data, ""
    if not isinstance(embed, dict):
        raise ValueError("The embed in that JSON isn't an object.")
    if isinstance(embed.get("color"), str):
        embed["color"] = parse_color(embed["color"])
    state = {"content": str(content), "embed": clean_embed_dict(embed)}
    discord.Embed.from_dict(copy.deepcopy(state["embed"]))  # catches malformed structures early
    return state


async def render(bot, state: dict, *, preview: bool = False) -> Tuple[str, Optional[discord.Embed], List[discord.File]]:
    """Build the message content, embed and any files to upload."""
    d = clean_embed_dict(state.get("embed") or {})
    if d.get("timestamp") is True:
        d["timestamp"] = discord.utils.utcnow().isoformat()

    files: List[discord.File] = []
    if _is_local is not None:
        for key, sub, label in IMAGE_SLOTS:
            url = (d.get(key) or {}).get(sub)
            if not url or not _is_local(url):
                continue
            fetched = await _fetch_local(bot, url)
            if fetched is None:
                d[key].pop(sub, None)
                if key in ("image", "thumbnail"):
                    d.pop(key, None)
                continue
            data, fname = fetched
            ext = fname.rsplit(".", 1)[-1] if "." in fname else "png"
            name = f"{label}.{ext}"
            files.append(discord.File(io.BytesIO(data), filename=name))
            d[key][sub] = f"attachment://{name}"

    embed = None if embed_is_empty(d) else discord.Embed.from_dict(d)
    if preview and embed is None:
        embed = discord.Embed(description="*Empty embed. Use the buttons below to add content.*", color=0x2B2D31)
    return state.get("content") or "", embed, files


# --------------------------------------------------------------------------- #
# Modals
# --------------------------------------------------------------------------- #
class _BaseModal(discord.ui.Modal):
    def __init__(self, builder: "Builder", title: str):
        super().__init__(title=title, timeout=600)
        self.builder = builder

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        msg = str(error) if isinstance(error, ValueError) else "Something went wrong saving that."
        if not isinstance(error, ValueError):
            logger.error("Embed builder modal error", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class TextModal(_BaseModal):
    def __init__(self, builder):
        super().__init__(builder, "Text")
        d, s = builder.state["embed"], builder.state
        desc = d.get("description") or ""
        self.desc_too_long = len(desc) > MODAL_MAX
        self.title_in = discord.ui.TextInput(
            label="Title", required=False, max_length=MAX_TITLE, default=d.get("title") or None
        )
        self.url_in = discord.ui.TextInput(
            label="Title link (optional)", required=False, placeholder="https://...", default=d.get("url") or None
        )
        self.desc_in = discord.ui.TextInput(
            label="Description" if not self.desc_too_long else "Description (too long to edit here; kept)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MODAL_MAX,
            default=(desc or None) if not self.desc_too_long else None,
        )
        self.content_in = discord.ui.TextInput(
            label="Message text above the embed (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_CONTENT,
            default=s.get("content") or None,
            placeholder="e.g. @everyone",
        )
        for item in (self.title_in, self.url_in, self.desc_in, self.content_in):
            self.add_item(item)

    async def on_submit(self, interaction):
        d = self.builder.state["embed"]
        d["title"] = self.title_in.value.strip()
        d["url"] = check_url(self.url_in.value, "title link")
        if not self.desc_too_long or self.desc_in.value.strip():
            d["description"] = self.desc_in.value
        self.builder.state["content"] = self.content_in.value
        await self.builder.refresh(interaction)


class ColorModal(_BaseModal):
    def __init__(self, builder):
        super().__init__(builder, "Colour")
        self.color_in = discord.ui.TextInput(
            label="Colour (hex, name, or blank for none)",
            required=False,
            max_length=20,
            placeholder="#5865F2, blue, 88,101,242",
            default=format_color(builder.state["embed"].get("color")) or None,
        )
        self.add_item(self.color_in)

    async def on_submit(self, interaction):
        self.builder.state["embed"]["color"] = parse_color(self.color_in.value)
        await self.builder.refresh(interaction)


class AuthorModal(_BaseModal):
    def __init__(self, builder):
        super().__init__(builder, "Author (top line)")
        a = builder.state["embed"].get("author") or {}
        self.name_in = discord.ui.TextInput(label="Name", required=False, max_length=MAX_AUTHOR, default=a.get("name") or None)
        self.icon_in = discord.ui.TextInput(label="Icon image link", required=False, default=a.get("icon_url") or None)
        self.url_in = discord.ui.TextInput(label="Name link", required=False, default=a.get("url") or None)
        for item in (self.name_in, self.icon_in, self.url_in):
            self.add_item(item)

    async def on_submit(self, interaction):
        self.builder.state["embed"]["author"] = {
            "name": self.name_in.value.strip(),
            "icon_url": check_url(self.icon_in.value, "author icon"),
            "url": check_url(self.url_in.value, "author link"),
        }
        await self.builder.refresh(interaction)


class FooterModal(_BaseModal):
    def __init__(self, builder):
        super().__init__(builder, "Footer")
        f = builder.state["embed"].get("footer") or {}
        self.text_in = discord.ui.TextInput(
            label="Footer text",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_FOOTER,
            default=f.get("text") or None,
        )
        self.icon_in = discord.ui.TextInput(label="Footer icon link", required=False, default=f.get("icon_url") or None)
        self.time_in = discord.ui.TextInput(
            label="Show timestamp? (yes/no)",
            required=False,
            max_length=3,
            default="yes" if builder.state["embed"].get("timestamp") else "no",
        )
        for item in (self.text_in, self.icon_in, self.time_in):
            self.add_item(item)

    async def on_submit(self, interaction):
        d = self.builder.state["embed"]
        d["footer"] = {"text": self.text_in.value, "icon_url": check_url(self.icon_in.value, "footer icon")}
        want_time = self.time_in.value.strip().lower() in ("y", "yes", "true", "on", "1")
        if want_time and not d.get("timestamp"):
            d["timestamp"] = True
        elif not want_time:
            d.pop("timestamp", None)
        await self.builder.refresh(interaction)


class ImagesModal(_BaseModal):
    def __init__(self, builder):
        super().__init__(builder, "Images")
        d = builder.state["embed"]
        self.image_in = discord.ui.TextInput(
            label="Large image link (bottom)", required=False, default=(d.get("image") or {}).get("url") or None
        )
        self.thumb_in = discord.ui.TextInput(
            label="Thumbnail link (small, top right)",
            required=False,
            default=(d.get("thumbnail") or {}).get("url") or None,
        )
        self.add_item(self.image_in)
        self.add_item(self.thumb_in)

    async def on_submit(self, interaction):
        d = self.builder.state["embed"]
        d["image"] = {"url": check_url(self.image_in.value, "image link")}
        d["thumbnail"] = {"url": check_url(self.thumb_in.value, "thumbnail link")}
        await self.builder.refresh(interaction)


class FieldModal(_BaseModal):
    def __init__(self, builder, index: Optional[int] = None, from_panel: bool = True):
        super().__init__(builder, "Add field" if index is None else f"Edit field {index + 1}")
        self.index = index
        self.from_panel = from_panel
        f = builder.state["embed"]["fields"][index] if index is not None else {}
        self.name_in = discord.ui.TextInput(label="Field name", max_length=MAX_FIELD_NAME, default=f.get("name") or None)
        self.value_in = discord.ui.TextInput(
            label="Field value",
            style=discord.TextStyle.paragraph,
            max_length=MAX_FIELD_VALUE,
            default=f.get("value") or None,
        )
        self.inline_in = discord.ui.TextInput(
            label="Side by side with other fields? (yes/no)",
            required=False,
            max_length=3,
            default="yes" if f.get("inline") else "no",
        )
        for item in (self.name_in, self.value_in, self.inline_in):
            self.add_item(item)

    async def on_submit(self, interaction):
        fields = self.builder.state["embed"].setdefault("fields", [])
        field = {
            "name": self.name_in.value,
            "value": self.value_in.value,
            "inline": self.inline_in.value.strip().lower() in ("y", "yes", "true", "on", "1"),
        }
        if self.index is None:
            if len(fields) >= MAX_FIELDS:
                raise ValueError(f"An embed can have at most {MAX_FIELDS} fields.")
            fields.append(field)
        else:
            fields[self.index] = field
        if self.from_panel:
            await self.builder.refresh(interaction)
        else:  # opened from the small "field actions" message
            await interaction.response.edit_message(content="Field updated.", view=None)
            await self.builder.refresh_panel()


class TemplateModal(_BaseModal):
    def __init__(self, builder):
        super().__init__(builder, "Save as template")
        self.name_in = discord.ui.TextInput(label="Template name", max_length=50, placeholder="e.g. rules")
        self.add_item(self.name_in)

    async def on_submit(self, interaction):
        name = self.name_in.value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,50}", name):
            raise ValueError("Use only letters, numbers, `-` and `_` (no spaces) in template names.")
        await self.builder.cog.save_template(name, self.builder.state, interaction.user)
        await interaction.response.send_message(
            f"Saved as template `{name}`. Post it any time with `{self.builder.ctx.clean_prefix}embed post {name}`.",
            ephemeral=True,
        )


# --------------------------------------------------------------------------- #
# Builder panel
# --------------------------------------------------------------------------- #
class FieldSelect(discord.ui.Select):
    def __init__(self, builder: "Builder"):
        fields = builder.state["embed"].get("fields") or []
        options = [
            discord.SelectOption(label=f"{i + 1}. {(f.get('name') or '(no name)')[:90]}", value=str(i))
            for i, f in enumerate(fields)
        ]
        if not options:
            options = [discord.SelectOption(label="No fields yet", value="-1")]
        super().__init__(
            placeholder="Edit or remove a field...",
            options=options[:25],
            disabled=not fields,
            row=3,
        )
        self.builder = builder

    async def callback(self, interaction):
        index = int(self.values[0])
        if index < 0:
            return await interaction.response.defer()
        view = FieldActions(self.builder, index)
        await interaction.response.send_message(f"Field {index + 1}: what would you like to do?", view=view, ephemeral=True)


class FieldActions(discord.ui.View):
    def __init__(self, builder: "Builder", index: int):
        super().__init__(timeout=120)
        self.builder, self.index = builder, index

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        await interaction.response.send_modal(FieldModal(self.builder, self.index, from_panel=False))
        self.stop()

    @discord.ui.button(label="Move up", style=discord.ButtonStyle.secondary)
    async def up(self, interaction, button):
        fields = self.builder.state["embed"]["fields"]
        if self.index > 0:
            fields[self.index - 1], fields[self.index] = fields[self.index], fields[self.index - 1]
        await interaction.response.edit_message(content="Moved.", view=None)
        await self.builder.refresh_panel()
        self.stop()

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def remove(self, interaction, button):
        fields = self.builder.state["embed"]["fields"]
        if 0 <= self.index < len(fields):
            fields.pop(self.index)
        await interaction.response.edit_message(content="Field removed.", view=None)
        await self.builder.refresh_panel()
        self.stop()


class Builder(discord.ui.View):
    def __init__(self, cog, ctx, state, *, channel=None, target: Optional[discord.Message] = None):
        super().__init__(timeout=1800)
        self.cog, self.ctx, self.state = cog, ctx, state
        self.state.setdefault("embed", {})
        self.channel = channel
        self.target = target
        self.message: Optional[discord.Message] = None
        self.post.label = "Save changes" if target else "Post"
        self._rebuild_select()

    def _rebuild_select(self):
        for item in list(self.children):
            if isinstance(item, FieldSelect):
                self.remove_item(item)
        self.add_item(FieldSelect(self))

    def header(self) -> str:
        where = (
            f"Editing [this message]({self.target.jump_url})"
            if self.target
            else f"Will post in {self.channel.mention}"
        )
        n = len(self.state["embed"].get("fields") or [])
        text = f"**Embed builder** · {where} · {n}/{MAX_FIELDS} fields · {embed_length(self.state['embed'])}/{MAX_TOTAL} characters"
        content = self.state.get("content") or ""
        if content:
            shown = content if len(content) <= 1500 else content[:1500] + "…"
            text += f"\n**Message text:**\n{shown}"
        return text

    async def _payload(self):
        _, embed, files = await render(self.cog.bot, self.state, preview=True)
        self._rebuild_select()
        return {"content": self.header(), "embed": embed, "attachments": files, "view": self}

    async def refresh(self, interaction: discord.Interaction):
        payload = await self._payload()
        await interaction.response.edit_message(**payload)

    async def refresh_panel(self):
        if self.message is not None:
            await self.message.edit(**await self._payload())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the person who opened this builder can use it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(content=self.header() + "\n*Builder closed (timed out).*", view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        msg = str(error) if isinstance(error, ValueError) else "Something went wrong. Check the bot logs."
        if not isinstance(error, ValueError):
            logger.error("Embed builder error", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # Row 0: content
    @discord.ui.button(label="Text", emoji="📝", style=discord.ButtonStyle.secondary, row=0)
    async def text(self, interaction, button):
        await interaction.response.send_modal(TextModal(self))

    @discord.ui.button(label="Colour", emoji="🎨", style=discord.ButtonStyle.secondary, row=0)
    async def color(self, interaction, button):
        await interaction.response.send_modal(ColorModal(self))

    @discord.ui.button(label="Author", emoji="👤", style=discord.ButtonStyle.secondary, row=0)
    async def author(self, interaction, button):
        await interaction.response.send_modal(AuthorModal(self))

    @discord.ui.button(label="Footer", emoji="🔻", style=discord.ButtonStyle.secondary, row=0)
    async def footer(self, interaction, button):
        await interaction.response.send_modal(FooterModal(self))

    @discord.ui.button(label="Images", emoji="🖼️", style=discord.ButtonStyle.secondary, row=0)
    async def images(self, interaction, button):
        await interaction.response.send_modal(ImagesModal(self))

    # Row 1: fields & templates
    @discord.ui.button(label="Add field", emoji="➕", style=discord.ButtonStyle.secondary, row=1)
    async def add_field(self, interaction, button):
        if len(self.state["embed"].get("fields") or []) >= MAX_FIELDS:
            raise ValueError(f"An embed can have at most {MAX_FIELDS} fields.")
        await interaction.response.send_modal(FieldModal(self))

    @discord.ui.button(label="Save template", emoji="💾", style=discord.ButtonStyle.secondary, row=1)
    async def save_template(self, interaction, button):
        await interaction.response.send_modal(TemplateModal(self))

    @discord.ui.button(label="Export JSON", emoji="📄", style=discord.ButtonStyle.secondary, row=1)
    async def export(self, interaction, button):
        await interaction.response.send_message(file=self.cog.json_file(self.state, "embed.json"), ephemeral=True)

    # Row 2: finish
    @discord.ui.button(label="Post", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def post(self, interaction, button):
        validate(self.state)
        await interaction.response.defer()
        content, embed, files = await render(self.cog.bot, self.state)
        try:
            if self.target is not None:
                kwargs = {"content": content or None, "embeds": [embed] + self.target.embeds[1:] if embed else self.target.embeds[1:]}
                if files:
                    kwargs["attachments"] = files
                sent = await self.target.edit(**kwargs)
                done = f"✅ Saved changes to [this message]({sent.jump_url})."
            else:
                sent = await self.channel.send(content=content or None, embed=embed, files=files)
                done = f"✅ Posted in {self.channel.mention}: [jump to message]({sent.jump_url})."
        except discord.Forbidden:
            where = self.target.channel.mention if self.target else self.channel.mention
            raise ValueError(f"I don't have permission to send messages or embeds in {where}.")
        self.stop()
        await interaction.edit_original_response(content=done, embed=None, attachments=[], view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Embed builder cancelled.", embed=None, attachments=[], view=None)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class EmbedBuilder(commands.Cog):
    """Build, post, edit and save embeds with an interactive builder."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)

    # ---- templates ------------------------------------------------------- #
    async def save_template(self, name: str, state: dict, author) -> None:
        data = copy.deepcopy(state)
        await self.db.replace_one(
            {"_id": f"template:{name}"},
            {"_id": f"template:{name}", "type": "template", "name": name, "data": data, "author": str(author.id)},
            upsert=True,
        )

    async def get_template(self, name: str) -> Optional[dict]:
        doc = await self.db.find_one({"_id": f"template:{name.lower()}"})
        return copy.deepcopy(doc["data"]) if doc else None

    @staticmethod
    def json_file(state: dict, filename: str) -> discord.File:
        out = {"content": state.get("content") or None, "embeds": [clean_embed_dict(state.get("embed") or {})]}
        if out["embeds"][0].get("timestamp") is True:
            out["embeds"][0]["timestamp"] = discord.utils.utcnow().isoformat()
        text = json.dumps(out, indent=2, ensure_ascii=False)
        return discord.File(io.BytesIO(text.encode()), filename=filename)

    async def open_builder(self, ctx, state, *, channel=None, target=None):
        view = Builder(self, ctx, state, channel=channel or ctx.channel, target=target)
        _, embed, files = await render(self.bot, view.state, preview=True)
        view.message = await ctx.send(view.header(), embed=embed, files=files, view=view)

    # ---- commands -------------------------------------------------------- #
    @commands.group(name="embed", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_group(self, ctx):
        """Build, post and edit embeds."""
        await ctx.send_help(ctx.command)

    @embed_group.command(name="create", aliases=["new", "make"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_create(self, ctx, *, channel: discord.TextChannel = None):
        """
        Open the embed builder.

        `channel` is where it will be posted (defaults to this channel).
        """
        await self.open_builder(ctx, blank_state(), channel=channel)

    @embed_group.command(name="load", aliases=["template"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_load(self, ctx, name: str, *, channel: discord.TextChannel = None):
        """
        Open the builder starting from a saved template.

        Change anything you like before posting; the template itself is only
        changed if you press Save template again with the same name.
        """
        state = await self.get_template(name)
        if state is None:
            return await ctx.send(embed=self._error(f"No template called `{name}`. See `{ctx.clean_prefix}embed templates`."))
        await self.open_builder(ctx, state, channel=channel)

    @embed_group.command(name="edit")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_edit(self, ctx, *, message: discord.Message):
        """
        Edit an embed the bot already posted.

        `message` may be a message link, or `channelID-messageID`.
        """
        if message.author.id != self.bot.user.id:
            return await ctx.send(embed=self._error("I can only edit messages that I posted myself."))
        await self.open_builder(ctx, state_from_message(message), target=message)

    @embed_group.command(name="json", aliases=["import"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_json(self, ctx, channel: Optional[discord.TextChannel] = None, *, data: str = None):
        """
        Open the builder from JSON (for example from Discohook).

        Paste the JSON after the command, or attach a .json file.
        """
        if not data and ctx.message.attachments:
            data = (await ctx.message.attachments[0].read()).decode("utf-8", "replace")
        if not data:
            return await ctx.send(embed=self._error("Paste some JSON after the command or attach a .json file."))
        try:
            state = state_from_json(data)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            return await ctx.send(embed=self._error(str(e) or "That JSON doesn't look like an embed."))
        await self.open_builder(ctx, state, channel=channel)

    @embed_group.command(name="export")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_export(self, ctx, *, message: discord.Message):
        """Get a message's text and first embed as JSON."""
        await ctx.send(file=self.json_file(state_from_message(message), f"embed-{message.id}.json"))

    @embed_group.command(name="post", aliases=["send"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_post(self, ctx, name: str, channel: Optional[discord.TextChannel] = None):
        """Post a saved template straight away (defaults to this channel)."""
        state = await self.get_template(name)
        if state is None:
            return await ctx.send(embed=self._error(f"No template called `{name}`. See `{ctx.clean_prefix}embed templates`."))
        channel = channel or ctx.channel
        try:
            validate(state)
            content, embed, files = await render(self.bot, state)
            sent = await channel.send(content=content or None, embed=embed, files=files)
        except ValueError as e:
            return await ctx.send(embed=self._error(str(e)))
        except discord.Forbidden:
            return await ctx.send(embed=self._error(f"I don't have permission to post in {channel.mention}."))
        if channel != ctx.channel:
            await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=f"Posted `{name}`: [jump]({sent.jump_url})"))

    @embed_group.command(name="templates", aliases=["list"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_templates(self, ctx):
        """List saved embed templates."""
        docs = await self.db.find({"type": "template"}).to_list(None)
        if not docs:
            return await ctx.send(
                embed=self._error("No templates yet. Use the 💾 Save template button in the builder.")
            )
        names = sorted(d["name"] for d in docs)
        desc = "\n".join(f"`{n}`" for n in names)
        embed = discord.Embed(title=f"Embed templates ({len(names)})", description=desc[:4000], color=self.bot.main_color)
        embed.set_footer(text=f"Post: {ctx.clean_prefix}embed post <name> · Edit first: {ctx.clean_prefix}embed load <name>")
        await ctx.send(embed=embed)

    @embed_group.command(name="deltemplate", aliases=["removetemplate"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def embed_deltemplate(self, ctx, name: str):
        """Delete a saved template."""
        result = await self.db.delete_one({"_id": f"template:{name.lower()}"})
        if not result.deleted_count:
            return await ctx.send(embed=self._error(f"No template called `{name}`."))
        await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=f"Deleted template `{name}`."))

    def _error(self, text: str) -> discord.Embed:
        return discord.Embed(title="Error", description=text, color=self.bot.error_color)


async def setup(bot):
    await bot.add_cog(EmbedBuilder(bot))
