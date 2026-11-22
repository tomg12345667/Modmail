"""
Modmail web dashboard: one website for all your Modmail bots.

People sign in with Discord. The dashboard asks each bot (through the bot's
private, localhost-only API) what that person may do, and only shows those
bots and areas. Every change is checked again by the bot itself.

Run:  python app.py        (settings come from .env next to this file)
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse

from aiohttp import ClientSession, ClientTimeout, web

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(os.path.join(HERE, ".env"))
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8200").rstrip("/")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8200"))
BOTS_FILE = os.environ.get("BOTS_FILE", os.path.join(HERE, "bots.json"))
DISCORD_API = os.environ.get("DISCORD_API", "https://discord.com/api").rstrip("/")
DISCORD_AUTHORIZE = os.environ.get("DISCORD_AUTHORIZE", "https://discord.com/oauth2/authorize")
# Discord user IDs that can see every bot in the list even when one is offline.
DASH_OWNERS = {x.strip() for x in os.environ.get("DASH_OWNERS", "").split(",") if x.strip()}
SECURE = PUBLIC_URL.startswith("https://")
COOKIE = "mmdash"
SESSION_DAYS = 7


def check_settings():
    problems = []
    if not CLIENT_ID or not CLIENT_SECRET:
        problems.append("DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET are required.")
    if len(SESSION_SECRET) < 32:
        problems.append("SESSION_SECRET must be at least 32 characters.")
    if not os.path.exists(BOTS_FILE):
        problems.append(f"{BOTS_FILE} is missing. Run setup_dashboard.py first.")
    if problems:
        raise SystemExit("Dashboard can't start:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------- #
# Bots list (reloaded when bots.json changes)
# --------------------------------------------------------------------------- #
_bots_cache = {"mtime": None, "bots": []}


def bots():
    try:
        mtime = os.path.getmtime(BOTS_FILE)
    except OSError:
        return []
    if mtime != _bots_cache["mtime"]:
        with open(BOTS_FILE, encoding="utf-8") as fp:
            data = json.load(fp)
        _bots_cache.update(mtime=mtime, bots=[b for b in data if b.get("id") and b.get("url") and b.get("secret")])
    return _bots_cache["bots"]


def bot_by_id(bid):
    return next((b for b in bots() if b["id"] == bid), None)


# --------------------------------------------------------------------------- #
# Signed session cookie
# --------------------------------------------------------------------------- #
def _sign(data: bytes) -> str:
    return hmac.new(SESSION_SECRET.encode(), data, hashlib.sha256).hexdigest()


def make_session(user: dict) -> str:
    payload = {
        "uid": str(user["id"]),
        "name": user.get("global_name") or user.get("username") or "you",
        "avatar": (
            f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=64"
            if user.get("avatar")
            else f"https://cdn.discordapp.com/embed/avatars/{(int(user['id']) >> 22) % 6}.png"
        ),
        "csrf": secrets.token_urlsafe(24),
        "exp": int(time.time()) + SESSION_DAYS * 86400,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"{raw}.{_sign(raw.encode())}"


def read_session(request):
    value = request.cookies.get(COOKIE, "")
    if "." not in value:
        return None
    raw, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(raw.encode())):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except ValueError:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def set_cookie(resp, name, value, max_age):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=SECURE, samesite="Lax", path="/")


# --------------------------------------------------------------------------- #
# Middleware: security headers
# --------------------------------------------------------------------------- #
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' https: data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@web.middleware
async def headers(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


# --------------------------------------------------------------------------- #
# Pages + sign-in
# --------------------------------------------------------------------------- #
def _page(name):
    return web.FileResponse(os.path.join(STATIC, name), headers={"Cache-Control": "no-cache"})


async def index(request):
    return _page("app.html" if read_session(request) else "login.html")


async def login(request):
    state = secrets.token_urlsafe(24)
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "scope": "identify",
            "redirect_uri": f"{PUBLIC_URL}/callback",
            "state": state,
            "prompt": "none",
        }
    )
    resp = web.HTTPFound(f"{DISCORD_AUTHORIZE}?{q}")
    set_cookie(resp, "mmdash_state", state, 600)
    raise resp


def _error_page(message):
    html = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Sign-in problem</title><link rel=stylesheet href='/static/style.css'>"
        "<main class='signin'><div class='signin-box'><h1>Sign-in didn't work</h1>"
        f"<p>{html.escape(message)}</p>"
        "<p><a class='btn primary' href='/login'>Try again</a></p></div></main>"
    )
    return web.Response(text=html, content_type="text/html", status=400)


async def callback(request):
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    expected = request.cookies.get("mmdash_state", "")
    if request.query.get("error"):
        return _error_page("Discord sign-in was cancelled.")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return _error_page("The sign-in link expired or was opened in a different browser. Start again from this page.")
    timeout = ClientTimeout(total=15)
    async with ClientSession(timeout=timeout) as s:
        async with s.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{PUBLIC_URL}/callback",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        ) as r:
            if r.status != 200:
                return _error_page("Discord didn't accept the sign-in. Check the dashboard's client ID, secret and redirect URL.")
            token = (await r.json()).get("access_token")
        async with s.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status != 200:
                return _error_page("Couldn't read your Discord account.")
            user = await r.json()
    resp = web.HTTPFound("/")
    set_cookie(resp, COOKIE, make_session(user), SESSION_DAYS * 86400)
    resp.del_cookie("mmdash_state", path="/")
    raise resp


def _require(request, *, csrf=False):
    sess = read_session(request)
    if sess is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Sign in again."}), content_type="application/json")
    if csrf and not hmac.compare_digest(request.headers.get("X-CSRF", ""), sess["csrf"]):
        raise web.HTTPForbidden(text=json.dumps({"error": "Refresh the page and try again."}), content_type="application/json")
    return sess


async def logout(request):
    _require(request, csrf=True)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Talking to bots
# --------------------------------------------------------------------------- #
async def _bot_call(app, bot, uid, method, path, *, query="", body=None, timeout=60):
    url = bot["url"].rstrip("/") + path + (f"?{query}" if query else "")
    headers = {"X-Dash-Secret": bot["secret"], "X-Dash-User": str(uid)}
    if body is not None:
        headers["Content-Type"] = "application/json"
    async with app["http"].request(method, url, data=body, headers=headers, timeout=ClientTimeout(total=timeout)) as r:
        return r.status, await r.read()


async def session_info(request):
    sess = _require(request)

    async def probe(bot):
        try:
            status, raw = await _bot_call(request.app, bot, sess["uid"], "GET", "/api/me", timeout=5)
            if status != 200:
                return None
            me = json.loads(raw)
            if not any(me["can"].values()):
                return None
            return {"id": bot["id"], "name": bot.get("name") or me["bot"]["name"], "online": True, **me}
        except Exception:
            if sess["uid"] in DASH_OWNERS:
                return {"id": bot["id"], "name": bot.get("name") or bot["id"], "online": False, "level": "?", "can": {}, "bot": {}}
            return None

    results = await asyncio.gather(*(probe(b) for b in bots()))
    return web.json_response(
        {
            "user": {"id": sess["uid"], "name": sess["name"], "avatar": sess["avatar"]},
            "csrf": sess["csrf"],
            "bots": [r for r in results if r],
        }
    )


async def proxy(request):
    sess = _require(request, csrf=request.method != "GET")
    bot = bot_by_id(request.match_info["bot"])
    if bot is None:
        return web.json_response({"error": "That bot isn't in this dashboard."}, status=404)
    tail = request.match_info["tail"]
    if ".." in tail or tail.startswith("/"):
        return web.json_response({"error": "Bad path."}, status=400)
    body = await request.read() if request.method in ("POST", "PUT", "PATCH") else None
    try:
        status, raw = await _bot_call(
            request.app, bot, sess["uid"], request.method, f"/api/{tail}", query=request.query_string, body=body
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": f"{bot.get('name', 'The bot')} took too long to answer."}, status=504)
    except Exception:
        return web.json_response({"error": f"Can't reach {bot.get('name', 'the bot')}. Is it running?"}, status=502)
    return web.Response(body=raw, status=status, content_type="application/json")


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
async def _startup(app):
    app["http"] = ClientSession()


async def _cleanup(app):
    await app["http"].close()


def make_app():
    app = web.Application(middlewares=[headers], client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/login", login)
    app.router.add_get("/callback", callback)
    app.router.add_post("/logout", logout)
    app.router.add_get("/api/session", session_info)
    app.router.add_route("*", "/api/bot/{bot}/{tail:.*}", proxy)
    app.router.add_static("/static/", STATIC, show_index=False)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


if __name__ == "__main__":
    check_settings()
    print(f"Modmail dashboard on http://{HOST}:{PORT} (public address {PUBLIC_URL})")
    web.run_app(make_app(), host=HOST, port=PORT, print=None, access_log=None)
