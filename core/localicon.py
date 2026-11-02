"""
Make embed icons hosted on private/LAN addresses show up in Discord.

Discord's servers download embed images themselves, so an icon URL like
https://10.1.10.51:6719/avatar.png shows as a broken image. When an embed's
author/footer icon points at a private address (or a local file path), the bot
downloads the image itself, uploads it with the message, and points the embed
at ``attachment://`` instead.
"""

import io
import ipaddress
import os
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
import discord

from core.models import getLogger

logger = getLogger(__name__)

_CACHE: Dict[str, Tuple[float, bytes, str]] = {}
_CACHE_SECONDS = 600
_MAX_BYTES = 8 * 1024 * 1024


def is_local(url: Optional[str]) -> bool:
    if not url:
        return False
    url = str(url)
    if url.startswith("/") or url.startswith("file://"):
        return True
    host = urlparse(url).hostname or ""
    if host in ("localhost",) or host.endswith((".local", ".lan", ".internal", ".home")):
        return True
    if "." not in host and ":" not in host:
        return bool(host)  # bare hostname like "ringnet"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


async def _fetch(bot, url: str) -> Optional[Tuple[bytes, str]]:
    cached = _CACHE.get(url)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1], cached[2]
    try:
        if url.startswith("/") or url.startswith("file://"):
            path = url[len("file://") :] if url.startswith("file://") else url
            with open(path, "rb") as fp:
                data = fp.read(_MAX_BYTES + 1)
            name = os.path.basename(path)
        else:
            session = getattr(bot, "session", None)
            own = session is None or session.closed
            if own:
                session = aiohttp.ClientSession()
            try:
                # Local servers commonly use self-signed certificates.
                async with session.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    data = await resp.content.read(_MAX_BYTES + 1)
            finally:
                if own:
                    await session.close()
            name = os.path.basename(urlparse(url).path)
    except Exception as e:
        logger.warning("Could not load local icon %s: %s", url, e)
        return None
    if len(data) > _MAX_BYTES:
        logger.warning("Local icon %s is too large to upload.", url)
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    filename = f"icon{ext}"
    _CACHE[url] = (time.monotonic(), data, filename)
    return data, filename


async def localize_icons(bot, embed: Optional[discord.Embed]) -> Optional[discord.File]:
    """If the embed's author/footer icon is local, swap in an uploaded copy.

    Returns the discord.File to send with the message, or None.
    """
    if embed is None:
        return None
    targets = []
    if embed.author and is_local(embed.author.icon_url):
        targets.append(("author", embed.author.icon_url))
    if embed.footer and is_local(embed.footer.icon_url):
        targets.append(("footer", embed.footer.icon_url))
    if not targets:
        return None

    url = targets[0][1]  # author and footer icons are the same anon avatar
    fetched = await _fetch(bot, url)
    if fetched is None:
        for where, _ in targets:
            if where == "author":
                embed.set_author(name=embed.author.name, url=embed.author.url)
            else:
                embed.set_footer(text=embed.footer.text)
        return None

    data, filename = fetched
    ref = f"attachment://{filename}"
    for where, u in targets:
        if u != url:
            continue
        if where == "author":
            embed.set_author(name=embed.author.name, url=embed.author.url, icon_url=ref)
        else:
            embed.set_footer(text=embed.footer.text, icon_url=ref)
    return discord.File(io.BytesIO(data), filename=filename)
