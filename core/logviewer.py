"""
Minimal built-in log viewer for the JSON database.

The official Modmail logviewer reads from MongoDB, so it can't see JSON logs.
This tiny aiohttp web server serves ``/logs/<key>`` straight from the bot's
data so the log links Modmail posts keep working.

Enable it by setting LOG_VIEWER_PORT (e.g. 8000) and pointing LOG_URL at the
machine, e.g. LOG_URL=http://your-server-ip:8000
"""

import html
from datetime import datetime

from aiohttp import web

from core.models import getLogger

logger = getLogger(__name__)

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#313338;--panel:#2b2d31;--text:#dbdee1;--muted:#949ba4;--mod:#5865f2;--note:#f0b232;--sys:#23a55a}}
@media (prefers-color-scheme: light){{:root{{--bg:#fff;--panel:#f2f3f5;--text:#2e3338;--muted:#5c5e66}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
header{{background:var(--panel);padding:16px;border-bottom:1px solid #0003}}
header h1{{margin:0 0 4px;font-size:18px}} header p{{margin:2px 0;color:var(--muted);font-size:13px}}
main{{max-width:900px;margin:0 auto;padding:8px 16px 40px}}
.msg{{display:flex;gap:12px;padding:8px 0}} .msg img.av{{width:40px;height:40px;border-radius:50%;flex:none}}
.name{{font-weight:600}} .mod .name{{color:var(--mod)}} .note .name{{color:var(--note)}} .system .name{{color:var(--sys)}}
.time,.tag{{color:var(--muted);font-size:12px;margin-left:6px}} .content{{white-space:pre-wrap;word-wrap:break-word}}
.att img{{max-width:min(400px,100%);border-radius:6px;margin-top:4px;display:block}} .att a{{color:var(--mod)}}
.empty{{color:var(--muted);padding:40px 0;text-align:center}}
</style></head><body><header><h1>{title}</h1>{meta}</header><main>{body}</main></body></html>"""


def _fmt_time(ts):
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(str(ts)).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return html.escape(str(ts))


def render_log(doc: dict) -> str:
    e = html.escape
    recipient = doc.get("recipient") or {}
    closer = doc.get("closer") or {}
    title = f"Modmail log — {recipient.get('name', 'Unknown')}"
    meta = [
        f"<p>Log <b>{e(doc.get('key', ''))}</b> · User ID {e(str(recipient.get('id', '')))}</p>",
        f"<p>Opened {_fmt_time(doc.get('created_at'))}"
        + (f" · Closed {_fmt_time(doc.get('closed_at'))} by {e(closer.get('name', ''))}" if doc.get("closed_at") else " · Open")
        + "</p>",
    ]
    if doc.get("close_message"):
        meta.append(f"<p>Close message: {e(doc['close_message'])}</p>")

    rows = []
    for m in doc.get("messages") or []:
        a = m.get("author") or {}
        mtype = m.get("type", "thread_message")
        cls = ""
        if mtype in ("note", "internal"):
            cls = "note"
        elif mtype == "system":
            cls = "system"
        elif a.get("mod"):
            cls = "mod"
        tag = {"anonymous": "anonymous reply", "note": "note", "internal": "internal", "system": "system"}.get(mtype, "")
        atts = []
        for att in m.get("attachments") or []:
            url = e(str(att.get("url", "")))
            if att.get("is_image"):
                atts.append(f'<a href="{url}" target="_blank"><img src="{url}" alt="{e(str(att.get("filename","")))}"></a>')
            else:
                atts.append(f'<a href="{url}" target="_blank">{e(str(att.get("filename", "file")))}</a>')
        avatar = e(a.get("avatar_url") or "https://cdn.discordapp.com/embed/avatars/0.png")
        rows.append(
            f'<div class="msg {cls}"><img class="av" src="{avatar}" alt="">'
            f'<div><span class="name">{e(a.get("name", "Unknown"))}</span>'
            f'<span class="time">{_fmt_time(m.get("timestamp"))}</span>'
            + (f'<span class="tag">({tag})</span>' if tag else "")
            + (" <span class='tag'>(edited)</span>" if m.get("edited") else "")
            + f'<div class="content">{e(m.get("content") or "")}</div>'
            + (f'<div class="att">{"".join(atts)}</div>' if atts else "")
            + "</div></div>"
        )
    body = "".join(rows) or '<div class="empty">No messages in this log.</div>'
    return PAGE.format(title=e(title), meta="".join(meta), body=body)


class LogViewer:
    def __init__(self, bot):
        self.bot = bot
        self.runner = None

    async def handle(self, request: web.Request):
        key = request.match_info["key"]
        doc = await self.bot.api.logs.find_one({"key": key})
        if not doc:
            return web.Response(status=404, text="Log not found.")
        return web.Response(text=render_log(doc), content_type="text/html")

    async def start(self, host: str, port: int):
        if self.runner is not None:
            return
        prefix = (self.bot.config["log_url_prefix"] or "").strip("/")
        if prefix.upper() == "NONE":
            prefix = ""
        route = f"/{prefix}/{{key}}" if prefix else "/{key}"
        app = web.Application()
        app.router.add_get(route, self.handle)
        app.router.add_get("/", lambda r: web.Response(text="Modmail log viewer is running."))
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()
        logger.info("Log viewer listening on http://%s:%s%s", host, port, route.replace("{key}", "<key>"))

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
