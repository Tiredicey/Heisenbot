import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import webbrowser
from pathlib import Path

from aiohttp import web

from .bot import Bot
from .config import CACHE, CLIPS, DATA
from .media import make_starter_clips, probe

WEB = Path(__file__).resolve().parent / "web"
SAFE = re.compile(r"[^a-z0-9_\-]")


def safe_name(s):
    return SAFE.sub("", (s or "").strip().lower().replace(" ", "_"))[:40]


def inside(base, p):
    p = Path(p).resolve()
    return p if str(p).startswith(str(Path(base).resolve())) and p.exists() else None


def _secret():
    f = DATA / "session.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip().encode()


def create_app(bot=None, password=None):
    bot = bot or Bot()
    password = password if password is not None else os.environ.get("HEISENBOT_PASSWORD", "")
    key = _secret()
    fails = {}
    app = web.Application(client_max_size=200 * 1024 * 1024)
    r = app.router

    def token(ts):
        return f"{ts}.{hmac.new(key, f'{ts}|{password}'.encode(), hashlib.sha256).hexdigest()}"

    def authed(req):
        if not password:
            return True
        c = req.cookies.get("hb_session", "")
        ts, _, _ = c.partition(".")
        return ts.isdigit() and time.time() - int(ts) < 30 * 86400 and hmac.compare_digest(c, token(ts))

    @web.middleware
    async def auth(request, handler):
        if authed(request) or request.path in ("/login", "/web/style.css", "/healthz"):
            return await handler(request)
        if request.path.startswith("/api/"):
            return web.json_response({"ok": False, "error": "Log in first"}, status=401)
        raise web.HTTPFound("/login")

    async def login_page(req):
        if req.method == "POST":
            ip = req.headers.get("CF-Connecting-IP") or req.remote or "?"
            n, t0 = fails.get(ip, (0, time.time()))
            if n >= 8 and time.time() - t0 < 900:
                return web.Response(text=LOGIN.replace("{msg}", "Too many tries. Wait 15 minutes."), content_type="text/html")
            form = await req.post()
            if hmac.compare_digest(str(form.get("password", "")).encode(), password.encode()):
                fails.pop(ip, None)
                resp = web.HTTPFound("/")
                resp.set_cookie("hb_session", token(str(int(time.time()))), max_age=30 * 86400, httponly=True,
                                samesite="Strict", secure=req.secure or req.headers.get("X-Forwarded-Proto") == "https")
                raise resp
            fails[ip] = (n + 1, t0 if n else time.time())
            await asyncio.sleep(1.5)
            return web.Response(text=LOGIN.replace("{msg}", "Wrong password."), content_type="text/html")
        return web.Response(text=LOGIN.replace("{msg}", ""), content_type="text/html")

    async def healthz(_):
        return web.Response(text="ok")

    def ok(data=None, **kw):
        return web.json_response({"ok": True, **({"data": data} if data is not None else {}), **kw})

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    app.middlewares.append(auth)
    app.middlewares.append(errors)

    async def index(_):
        return web.FileResponse(WEB / "index.html")

    async def status(_):
        return ok(bot.status())

    async def get_config(_):
        return ok(bot.cfg.data)

    async def put_config(req):
        return ok(bot.cfg.update(await req.json()))

    async def clips(_):
        out = []
        for reaction, files in sorted(bot.bank.index().items()):
            for f in files:
                info = await asyncio.to_thread(probe, f)
                out.append({"reaction": reaction, "name": f.name, "path": str(f.relative_to(CLIPS)).replace("\\", "/"),
                            "size": f.stat().st_size, "duration": round(info["duration"], 2),
                            "audio": info["audio"], "width": info["width"], "height": info["height"]})
        return ok(out)

    async def upload(req):
        reader = await req.multipart()
        reaction, saved = "misc", []
        async for part in reader:
            if part.name == "reaction":
                reaction = safe_name(await part.text()) or "misc"
            elif part.name == "files" and part.filename:
                ext = Path(part.filename).suffix.lower()
                if ext not in (".mp4", ".mov", ".webm", ".mkv"):
                    continue
                folder = CLIPS / reaction
                folder.mkdir(parents=True, exist_ok=True)
                dest = folder / (safe_name(Path(part.filename).stem) or "clip")
                dest = dest.with_suffix(ext)
                i = 1
                while dest.exists():
                    dest = folder / f"{dest.stem.rstrip('_0123456789')}_{i}{ext}"
                    i += 1
                with dest.open("wb") as fh:
                    while chunk := await part.read_chunk(1 << 16):
                        fh.write(chunk)
                saved.append(dest.name)
        if reaction not in bot.cfg["reactions"]:
            rs = dict(bot.cfg["reactions"])
            rs[reaction] = {"keywords": [reaction], "lines": [""]}
            bot.cfg.update({"reactions": rs})
        bot.log("ok", f"Added {len(saved)} clip(s) to {reaction}")
        return ok(saved)

    async def delete_clip(req):
        p = inside(CLIPS, CLIPS / req.match_info["path"])
        if not p:
            raise web.HTTPNotFound()
        p.unlink()
        return ok()

    async def clip_file(req):
        p = inside(CLIPS, CLIPS / req.match_info["path"])
        if not p:
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    async def render_file(req):
        p = inside(CACHE, CACHE / "renders" / Path(req.match_info["name"]).name)
        if not p:
            p = inside(CACHE, CACHE / Path(req.match_info["name"]).name)
        if not p:
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    async def starter(req):
        reader = await req.multipart()
        part = await reader.next()
        if not part or not part.filename:
            raise ValueError("Pick a picture of your Walter model")
        dest = CACHE / ("starter_source" + (Path(part.filename).suffix.lower() or ".png"))
        with dest.open("wb") as fh:
            while chunk := await part.read_chunk(1 << 16):
                fh.write(chunk)
        made = await asyncio.to_thread(make_starter_clips, dest, CLIPS, overwrite=True)
        bot.log("ok", f"Built {len(made)} starter clips from your picture")
        return ok([m.name for m in made])

    async def test(req):
        body = await req.json()
        res = await bot.test(body.get("sender", "Tester"), body.get("text", ""))
        return ok(res)

    async def action(req):
        a = req.match_info["action"]
        body = await req.json() if req.can_read_body else {}
        if a == "open":
            await bot.open_browser(headless=False, url=body.get("url") or None)
        elif a == "capture":
            return ok(await bot.capture_thread())
        elif a == "close":
            await bot.close_browser()
        elif a == "start":
            await bot.start()
        elif a == "stop":
            await bot.stop()
        elif a == "diagnose":
            return ok(await bot.diagnose())
        elif a == "toggle":
            bot.engine.enabled = not bot.engine.enabled
        elif a == "snooze":
            bot.set_snooze(body.get("mode", "off"), body.get("minutes", 0), str(body.get("label", ""))[:20])
        elif a == "care_clear":
            bot.engine.care_until = 0
            bot.log("ok", "Care guard cleared")
        else:
            raise web.HTTPNotFound()
        return ok(bot.status())

    async def poll_events(req):
        since = float(req.query.get("since", 0) or 0)
        return ok([e for e in list(bot.events) if e["t"] > since][-120:])

    async def remote_shot(_):
        m = bot.messenger
        if not (m and m.open):
            raise web.HTTPNotFound()
        img = await m.snapshot()
        return web.Response(body=img, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def remote_act(req):
        m = bot.messenger
        if not (m and m.open):
            raise ValueError("Open the browser first")
        body = await req.json()
        await m.remote(body.pop("action", ""), **body)
        await asyncio.sleep(0.4)
        return ok({"url": m.current_url(), "needs_login": m.needs_login()})

    async def login_export(_):
        m = bot.messenger
        if not (m and m.open):
            raise ValueError("Open the browser and log in first")
        state = await m.export_login()
        return web.Response(body=json.dumps(state).encode(), content_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="walter_login.json"'})

    async def login_import(req):
        if not (bot.messenger and bot.messenger.open):
            await bot.open_browser(headless=True)
        reader = await req.multipart()
        part = await reader.next()
        raw = await part.read(decode=False) if part else b""
        n = await bot.messenger.import_login(json.loads(raw.decode("utf-8")))
        bot.log("ok", f"Imported login ({n} cookies)")
        return ok(bot.status())

    async def tunnel(_):
        return ok(await tunnel_url())

    async def voices(_):
        import edge_tts

        vs = await edge_tts.list_voices()
        return ok(sorted({v["ShortName"] for v in vs if v["Locale"].startswith(("en-", "fil-"))}))

    async def shutdown(app_):
        await bot.close_browser()

    async def startup(app_):
        loop = asyncio.get_running_loop()
        default = loop.get_exception_handler()

        def quiet(lp, ctx):
            if isinstance(ctx.get("exception"), (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                return
            if default:
                default(lp, ctx)
            else:
                lp.default_exception_handler(ctx)

        loop.set_exception_handler(quiet)
        asyncio.get_running_loop().create_task(announce(bot))
        asyncio.get_running_loop().create_task(bot.autostart())

    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    r.add_route("*", "/login", login_page)
    r.add_get("/healthz", healthz)
    r.add_get("/api/log", poll_events)
    r.add_get("/api/remote/shot", remote_shot)
    r.add_post("/api/remote/act", remote_act)
    r.add_get("/api/tunnel", tunnel)
    r.add_get("/api/login/export", login_export)
    r.add_post("/api/login/import", login_import)
    r.add_get("/", index)
    r.add_static("/web", WEB)
    r.add_get("/api/status", status)
    r.add_get("/api/config", get_config)
    r.add_put("/api/config", put_config)
    r.add_get("/api/clips", clips)
    r.add_post("/api/clips", upload)
    r.add_delete("/api/clips/{path:.+}", delete_clip)
    r.add_get("/clip/{path:.+}", clip_file)
    r.add_get("/render/{name}", render_file)
    r.add_post("/api/starter", starter)
    r.add_post("/api/test", test)
    r.add_post("/api/bot/{action}", action)
    r.add_get("/api/voices", voices)
    app["bot"] = bot
    return app


async def tunnel_url():
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            async with s.get("http://127.0.0.1:20241/quicktunnel") as r:
                host = (await r.json(content_type=None)).get("hostname")
                return f"https://{host}" if host else ""
    except Exception:
        return ""


async def announce(bot):
    last = ""
    while True:
        url = await tunnel_url()
        if url and url != last:
            last = url
            bot.log("ok", f"Dashboard address: {url}")
            await bot.notify("Walter dashboard address", url, "default")
        await asyncio.sleep(30)


LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Heisenbot login</title><link rel="stylesheet" href="/web/style.css"></head>
<body class="login-body"><main class="card login-card"><div class="brand"><div class="element"><small>35</small><b>Br</b></div>
<div class="element"><small>56</small><b>Ba</b></div><div class="brand-text"><h1>Heisenbot</h1><span>Say the password.</span></div></div>
<form method="post"><label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button class="btn primary">Enter the lab</button><p class="hint">{msg}</p></form></main></body></html>"""


def serve(host=None, port=8765, open_browser=True):
    import os

    host = host or os.environ.get("HEISENBOT_HOST", "127.0.0.1")
    app = create_app()
    if open_browser:
        async def _open(_):
            asyncio.get_running_loop().call_later(1.0, webbrowser.open, f"http://127.0.0.1:{port}")
        app.on_startup.append(_open)
    print(f"Walter control room: http://{host}:{port}")
    web.run_app(app, host=host, port=port, print=None)
