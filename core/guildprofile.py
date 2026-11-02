"""
Per-server display name and profile picture for the bot.

    ?config set bot_display_name_server DialStyle Support
    ?config set bot_profile_picture_server https://example.com/avatar.png
    ?config remove bot_display_name_server       (back to the normal name)
    ?config remove bot_profile_picture_server    (back to the normal avatar)

These only change how the bot looks in this server; its global name and avatar
(set in the Discord Developer Portal) stay the same everywhere else.
The picture can be a public URL, a private/LAN URL, or a file path on the
server, because the bot downloads it and uploads it to Discord itself.
"""

import base64
from typing import List

import discord
from discord.http import Route

from core.localicon import _fetch
from core.models import getLogger

logger = getLogger(__name__)

NAME_KEY = "bot_display_name_server"
AVATAR_KEY = "bot_profile_picture_server"
KEYS = {NAME_KEY, AVATAR_KEY}


def _guilds(bot) -> List[discord.Guild]:
    out = []
    for g in (bot.guild, bot.modmail_guild):
        if g is not None and g not in out:
            out.append(g)
    return out


async def _avatar_data_uri(bot, source: str) -> str:
    fetched = await _fetch(bot, source)
    if fetched is None:
        raise ValueError(f"Couldn't download the image from `{source}`.")
    data, _ = fetched
    try:
        mime = discord.utils._get_mime_type_for_image(data)
    except ValueError:
        raise ValueError("That file isn't a PNG, JPG, GIF or WEBP image.")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


async def apply_guild_profile(bot, key: str) -> str:
    """Push one setting to Discord. Returns a short status message for the user."""
    value = bot.config.get(key)
    payload = {}
    if key == NAME_KEY:
        name = (str(value).strip() if value else "") or None
        if name and len(name) > 32:
            raise ValueError("Display names can be at most 32 characters.")
        payload["nick"] = name
    elif key == AVATAR_KEY:
        payload["avatar"] = await _avatar_data_uri(bot, str(value).strip()) if value else None
    else:
        return ""

    guilds = _guilds(bot)
    if not guilds:
        raise ValueError("The bot isn't in its configured server yet.")

    done, errors = [], []
    for g in guilds:
        try:
            await bot.http.request(Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=g.id), json=payload)
            done.append(g.name)
        except discord.Forbidden:
            hint = " (give the bot **Change Nickname**)" if key == NAME_KEY else ""
            errors.append(f"{g.name}: Discord refused the change{hint}")
        except discord.HTTPException as e:
            if e.status == 429:
                errors.append(f"{g.name}: Discord is rate limiting profile changes; try again in a few minutes")
            else:
                errors.append(f"{g.name}: {e.text or e}")
            logger.warning("Failed to update bot profile in %s: %s", g, e)

    what = "Display name" if key == NAME_KEY else "Profile picture"
    action = "updated" if value else "reset to the bot's default"
    lines = []
    if done:
        lines.append(f"{what} {action} in: {', '.join(done)}.")
    lines.extend(errors)
    if errors and not done:
        raise ValueError("\n".join(errors))
    return "\n".join(lines)
