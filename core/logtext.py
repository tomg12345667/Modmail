"""
Plain-text (.txt) transcripts of Modmail thread logs.

Used instead of web log links: `?logs`, `?logs key`, `?loglink` and the
log channel's "Thread Closed" message attach these as .txt files.
"""

import io
from datetime import datetime
from typing import Iterable

import discord

# Discord's default upload limit is 10 MiB; stay safely under it.
MAX_FILE_BYTES = 9 * 1024 * 1024

_TYPE_TAGS = {
    "anonymous": "anonymous reply",
    "note": "internal note",
    "persistent_note": "persistent note",
    "internal": "internal",
    "system": "system",
}


def _fmt_time(ts) -> str:
    if not ts:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return str(ts)


def _person(p) -> str:
    if not p:
        return "Unknown"
    name = p.get("name", "Unknown")
    disc = p.get("discriminator")
    if disc and disc != "0":
        name += f"#{disc}"
    return f"{name} ({p.get('id', '?')})"


def render_log_text(doc: dict) -> str:
    lines = []
    bar = "=" * 70
    lines.append(bar)
    lines.append(f"Modmail log {doc.get('key', '')}")
    if doc.get("title"):
        lines.append(f"Title:      {doc['title']}")
    lines.append(f"Recipient:  {_person(doc.get('recipient'))}")
    creator = doc.get("creator") or {}
    if creator and creator.get("id") != (doc.get("recipient") or {}).get("id"):
        lines.append(f"Opened by:  {_person(creator)}")
    lines.append(f"Opened:     {_fmt_time(doc.get('created_at'))}")
    if doc.get("open"):
        lines.append("Status:     OPEN")
    else:
        lines.append(f"Closed:     {_fmt_time(doc.get('closed_at'))}")
        lines.append(f"Closed by:  {_person(doc.get('closer'))}")
        if doc.get("close_message"):
            lines.append(f"Close msg:  {doc['close_message']}")
    lines.append(bar)
    lines.append("")

    messages = doc.get("messages") or []
    if not messages:
        lines.append("(no messages)")
    for m in messages:
        author = m.get("author") or {}
        role = "[MOD]" if author.get("mod") else "[USER]"
        tag = _TYPE_TAGS.get(m.get("type"), "")
        header = f"[{_fmt_time(m.get('timestamp'))}] {role} {_person(author)}"
        if tag:
            header += f" ({tag})"
        if m.get("edited"):
            header += " (edited)"
        lines.append(header)
        content = m.get("content") or ""
        if content:
            lines.extend("    " + ln for ln in content.splitlines())
        for att in m.get("attachments") or []:
            lines.append(f"    [attachment] {att.get('filename', 'file')}: {att.get('url', '')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_many(docs: Iterable[dict]) -> str:
    return "\n\n".join(render_log_text(d) for d in docs)


def make_file(text: str, filename: str) -> discord.File:
    data = text.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        note = "\n\n[... transcript truncated: too large for a Discord upload ...]\n".encode()
        data = data[: MAX_FILE_BYTES - len(note)] + note
    return discord.File(io.BytesIO(data), filename=filename)


def log_file(doc: dict) -> discord.File:
    return make_file(render_log_text(doc), f"modmail-log-{doc.get('key', 'unknown')}.txt")
