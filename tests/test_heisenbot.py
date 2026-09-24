import asyncio
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from heisenbot.config import DEFAULTS, Config, validate
from heisenbot.engine import ClipBank, Engine, Message, normalize
from heisenbot.media import make_starter_clips, probe, render


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def cfg(tmp_path):
    c = Config(tmp_path / "config.json")
    c.update({"lurk_chance": 0.0, "cooldown_seconds": 10, "per_user_cooldown_seconds": 30, "max_per_hour": 3})
    return c


@pytest.fixture
def bank(tmp_path):
    img = tmp_path / "w.png"
    from PIL import Image

    Image.new("RGB", (64, 64), (30, 90, 45)).save(img)
    root = tmp_path / "clips"
    make_starter_clips(img, root, seconds=1.0, size=128, fps=12)
    return ClipBank(root)


def eng(cfg, bank, clock=None, seed=1):
    return Engine(cfg, bank, rng=random.Random(seed), clock=clock or Clock())


def m(text, sender="Francis Gerald"):
    return Message("x", sender, text)


def test_normalize_squashes_repeats():
    assert normalize("LMAOOOOO") == normalize("lmao") == "lmao"
    assert normalize("???") == "???"


def test_keyword_whole_word(cfg, bank):
    e = eng(cfg, bank)
    assert e.match("lmaooooo")[0] == "laugh"
    assert e.match("solidified") is None
    assert e.match("hahahahaha")[0] == "laugh"
    assert e.match("bruhhhh")[0] == "facepalm"
    assert e.match("that was a fail")[0] == "facepalm"


def test_longest_keyword_wins(cfg, bank):
    e = eng(cfg, bank)
    assert e.match("wala magawa sa buhay lol")[0] == "facepalm"
    assert e.match("say my name lol")[0] == "saymyname"


def test_emoji_keyword(cfg, bank):
    assert eng(cfg, bank).match("💀💀")[0] == "dead"


def test_mention_uses_mention_reaction(cfg, bank):
    d, why = eng(cfg, bank).decide(m("@Walter ano na"))
    assert d.reaction == cfg["mention_reaction"] and why == "mention"


def test_global_and_user_cooldown(cfg, bank):
    clk = Clock()
    e = eng(cfg, bank, clk)
    d, _ = e.decide(m("lmao"))
    e.commit(d.message.sender)
    assert e.decide(m("lmao", "Other"))[1] == "global cooldown"
    clk.t += 11
    assert e.decide(m("lmao"))[1] == "user cooldown"
    assert e.decide(m("lmao", "Other"))[0] is not None


def test_mention_bypasses_user_cooldown(cfg, bank):
    clk = Clock()
    e = eng(cfg, bank, clk)
    e.commit("Francis Gerald")
    clk.t += 11
    assert e.decide(m("@walter lol"))[0] is not None


def test_hourly_cap(cfg, bank):
    clk = Clock()
    e = eng(cfg, bank, clk)
    for _ in range(3):
        e.commit("A")
        clk.t += 11
    assert e.decide(m("lmao", "B"))[1] == "hourly cap"
    clk.t += 3600
    assert e.decide(m("lmao", "B"))[0] is not None


def test_lurk_roll_rate(cfg, bank):
    cfg.update({"lurk_chance": 0.03, "cooldown_seconds": 0, "per_user_cooldown_seconds": 0, "max_per_hour": 500})
    e = eng(cfg, bank, seed=7)
    hits = sum(1 for i in range(20000) if e.decide(m("hello there", f"u{i}"))[0])
    assert 450 < hits < 750


def test_own_messages_ignored(cfg, bank):
    assert eng(cfg, bank).decide(Message("x", "You", "lmao", outgoing=True))[0] is None


def test_commands(cfg, bank):
    e = eng(cfg, bank)
    assert e.decide(m("!walter off"))[0].command == "off"
    assert not e.enabled
    assert e.decide(m("lmao"))[1] == "sleeping"
    e.decide(m("!walter on"))
    assert e.enabled
    d, _ = e.decide(m("!walter rage stop that"))
    assert d.reaction == "rage" and d.line == "stop that"


def test_admin_lock(cfg, bank):
    cfg.update({"admins": ["Gawk Capstoney"]})
    e = eng(cfg, bank)
    assert e.decide(m("!walter off", "Random"))[0] is None or e.enabled
    e.decide(m("!walter off", "Gawk Capstoney"))
    assert not e.enabled


def test_quiet_hours_wraps_midnight(cfg, bank):
    from datetime import datetime

    cfg.update({"quiet_hours": {"enabled": True, "start": 23, "end": 6}})
    clk = Clock()
    clk.t = datetime(2026, 9, 23, 2, 0).timestamp()
    assert eng(cfg, bank, clk).decide(m("lmao"))[1] == "quiet hours"
    clk.t = datetime(2026, 9, 23, 12, 0).timestamp()
    assert eng(cfg, bank, clk).decide(m("lmao"))[0] is not None


def test_name_substitution(cfg, bank):
    cfg.update({"reactions": {**cfg["reactions"], "laugh": {"keywords": ["lmao"], "lines": ["hi {name}"]}}})
    d, _ = eng(cfg, bank).decide(m("lmao", "Francis Gerald"))
    assert d.line == "hi Francis"


def test_validate_clamps():
    v = validate({"lurk_chance": 5, "max_per_hour": 0, "unknown": 1, "tts_source": "bad"})
    assert v == {"lurk_chance": 1.0, "max_per_hour": 1}


def test_bank_index_and_no_repeat(bank):
    idx = bank.index()
    assert set(idx) >= {"laugh", "stare", "dead"}
    root = bank.root / "laugh"
    (root / "second.mp4").write_bytes((root / "starter_bounce.mp4").read_bytes())
    picks = {bank.pick("laugh").name for _ in range(6)}
    assert picks == {"starter_bounce.mp4", "second.mp4"}


def test_render_caption_no_tts(bank, tmp_path):
    clip = bank.index()["laugh"][0]
    out = render(clip, tmp_path / "out", "Francis Gerald", "wala magawa", "HELLO", "",
                 {**DEFAULTS, "tts": False})
    info = probe(out)
    assert info["video"] and info["audio"] and info["duration"] >= 0.9


def test_defaults_cover_core_reactions():
    assert {"laugh", "facepalm", "dance", "rage", "stare", "confusion"} <= set(DEFAULTS["reactions"])


def test_playwright_mock_chat(tmp_path, bank):
    pytest.importorskip("playwright")
    from heisenbot.messenger import Messenger

    async def go():
        c = Config(tmp_path / "c.json")
        logs = []
        mm = Messenger(c, lambda *a: logs.append(a))
        import heisenbot.messenger as mod

        mod.PROFILE = tmp_path / "profile"
        await mm.start(headless=True, url=(Path(__file__).parent / "mock_chat.html").as_uri())
        try:
            raw = await mm.read_raw()
            assert raw["pane"] and raw["composer"]
            texts = [(r["sender"], r["text"], r["outgoing"]) for r in raw["rows"]]
            assert texts == [("Kurt Atienza", "test", False), ("Kurt Atienza", "old huh", False),
                             ("You", "my own message lol", True)]
            await mm.prime()
            assert await mm.poll() == []
            await mm.page.evaluate("addIncoming('Francis Gerald', 'wala magawa sa buhay', 'Francis')")
            got = await mm.poll()
            assert [(x.sender, x.text) for x in got] == [("Francis Gerald", "wala magawa sa buhay")]
            assert await mm.poll() == []
            await mm.page.evaluate("addIncoming('Kurt Atienza', 'huh'); addIncoming('Kurt Atienza', 'huh')")
            got = await mm.poll()
            assert [x.text for x in got] == ["huh", "huh"]
            await mm.page.evaluate("navigateAway()")
            assert await mm.poll() == []
            await mm.page.evaluate("addIncoming('Kurt Atienza', 'after reload lmao')")
            assert [x.text for x in await mm.poll()] == ["after reload lmao"]
            clip = bank.index()["laugh"][0]
            await mm.send_file(clip)
            sent = await mm.page.evaluate("window.sent")
            assert sent[-1]["files"] == [clip.name]
            await mm.send_text("Say my name.")
            assert (await mm.page.evaluate("window.sent"))[-1]["text"] == "Say my name."
            assert await mm.poll() == []
        finally:
            await mm.stop()

    asyncio.run(go())

def test_thread_url_validation():
    with pytest.raises(ValueError):
        validate({"thread_url": "https://evil.example.com/x"})
    assert validate({"thread_url": "https://www.facebook.com/messages/t/123"})["thread_url"].endswith("/123")
    assert validate({"ntfy_topic": "walter bad/../x"})["ntfy_topic"] == "walterbadx"


def test_cleanup_prunes_old_renders(tmp_path, monkeypatch):
    import os
    import time as t

    import heisenbot.bot as botmod

    monkeypatch.setattr(botmod, "CACHE", tmp_path)
    monkeypatch.setattr(botmod, "LOGS", tmp_path)
    folder = tmp_path / "renders"
    folder.mkdir()
    old, new = folder / "old.mp4", folder / "new.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (t.time() - 5 * 86400,) * 2)
    b = botmod.Bot(Config(tmp_path / "c.json"))
    assert b.cleanup() == 1 and new.exists() and not old.exists()


def test_password_gate_and_remote(tmp_path, monkeypatch, bank):
    pytest.importorskip("playwright")
    import heisenbot.bot as botmod
    import heisenbot.messenger as mod
    import heisenbot.server as srv
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(botmod, "LOGS", tmp_path)
    monkeypatch.setattr(srv, "DATA", tmp_path)
    monkeypatch.setattr(mod, "PROFILE", tmp_path / "profile")
    monkeypatch.setattr(mod, "HOME", (Path(__file__).parent / "mock_chat.html").as_uri())

    async def go():
        b = botmod.Bot(Config(tmp_path / "c.json"))
        app = srv.create_app(b, password="blue-sky")
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/status")
            assert r.status == 401
            r = await c.get("/", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/login"
            r = await c.post("/login", data={"password": "wrong"}, allow_redirects=False)
            assert r.status == 200 and "Wrong password" in await r.text()
            r = await c.post("/login", data={"password": "blue-sky"}, allow_redirects=False)
            assert r.status == 302
            r = await c.get("/api/status")
            assert r.status == 200
            await b.open_browser(headless=True)
            r = await c.get("/api/remote/shot")
            assert r.status == 200 and (await r.read())[:2] == b"\xff\xd8"
            r = await c.post("/api/remote/act", json={"action": "goto", "url": "https://evil.example.com"})
            assert r.status == 400
            r = await c.post("/api/remote/act", json={"action": "click", "x": 0.5, "y": 0.1})
            assert r.status == 200
            r = await c.post("/api/remote/act", json={"action": "key", "key": "F12"})
            assert r.status == 400
            r = await c.get("/api/log?since=0")
            assert any("Browser opened" in e["text"] for e in (await r.json())["data"])
            state = {"cookies": [{"name": "c_user", "value": "1", "domain": ".facebook.com", "path": "/",
                                  "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"}]}
            from aiohttp import FormData

            fd = FormData()
            fd.add_field("file", json.dumps(state).encode(), filename="walter_login.json", content_type="application/json")
            b.cfg.data["thread_url"] = (Path(__file__).parent / "mock_chat.html").as_uri()
            r = await c.post("/api/login/import", data=fd)
            assert r.status == 200, await r.text()
            cookies = await b.messenger.ctx.cookies("https://www.facebook.com")
            assert any(k["name"] == "c_user" for k in cookies)
            bad = FormData()
            bad.add_field("file", b'{"cookies": []}', filename="x.json")
            r = await c.post("/api/login/import", data=bad)
            assert r.status == 400
            await b.close_browser()

    asyncio.run(go())


def test_live_flag_persists_and_autostart(tmp_path, monkeypatch):
    import heisenbot.bot as botmod

    monkeypatch.setattr(botmod, "LOGS", tmp_path)
    cfg = Config(tmp_path / "c.json")
    cfg.update({"live": True, "thread_url": "https://www.facebook.com/messages/t/1"})
    b = botmod.Bot(Config(tmp_path / "c.json"))
    called = []

    async def fake_start():
        called.append(True)

    b.start = fake_start
    asyncio.run(b.autostart())
    assert called == [True]
    asyncio.run(b.stop())
    assert Config(tmp_path / "c.json")["live"] is False


def test_care_guard_silences_then_expires(cfg, bank):
    clk = Clock()
    cfg.update({"care_minutes": 30, "cooldown_seconds": 0, "per_user_cooldown_seconds": 0})
    e = eng(cfg, bank, clk)
    d, why = e.decide(m("guys nasa ospital si lola, dead tired na ako"))
    assert d is None and why.startswith("care guard")
    assert e.decide(m("lmao", "Other"))[1] == "care guard active"
    assert e.decide(m("@walter", "Other"))[1] == "care guard active"
    clk.t += 31 * 60
    assert e.decide(m("lmao", "Other"))[0] is not None


def test_care_guard_whole_words_and_off(cfg, bank):
    e = eng(cfg, bank)
    assert not e.care_hit("hospitality class later")
    assert e.care_hit("PANIC ATTACK ako kanina")
    cfg.update({"care_guard": False})
    assert eng(cfg, bank).decide(m("lmao ospital"))[0] is not None


def test_snooze_modes(cfg, bank):
    clk = Clock()
    e = eng(cfg, bank, clk)
    cfg.update({"snooze": {"mode": "mentions", "until": clk.t + 600}})
    assert e.decide(m("lmao"))[1] == "mentions only"
    assert e.decide(m("@walter lmao"))[0] is not None
    cfg.update({"snooze": {"mode": "pause", "until": clk.t + 600}})
    assert e.decide(m("@walter"))[1] == "paused"
    assert e.decide(m("!walter off"))[0].command == "off"
    e.enabled = True
    clk.t += 601
    assert e.decide(m("lmao", "Z"))[0] is not None


def test_phone_command_parser(tmp_path, monkeypatch):
    import heisenbot.bot as botmod

    monkeypatch.setattr(botmod, "LOGS", tmp_path)
    b = botmod.Bot(Config(tmp_path / "c.json"))
    assert b.parse_phone("pause 2h") == ("pause", 120)
    assert b.parse_phone("Class") == ("mentions", 120)
    assert b.parse_phone("date") == ("pause", 240)
    assert b.parse_phone("mentions 45") == ("mentions", 45)
    assert b.parse_phone("resume") == ("off", 0)
    assert b.parse_phone("status") == ("status", 0)
    assert b.parse_phone("hello") is None
    assert "paused" in b.set_snooze("pause", 30)
    assert b.engine.snooze_state()[0] == "pause"
    b.set_snooze("off", 0)
    assert b.engine.snooze_state()[0] == "off"


def test_unknown_sender_line_cleanup():
    from heisenbot.engine import fill_line

    assert fill_line("{name}, what are you talking about?", "Someone", "x") == "What are you talking about?"
    assert fill_line("Say my name, {name}.", "Someone", "x") == "Say my name."
    assert fill_line("{name}. Stay out", "Charles Angelo", "x") == "Charles. Stay out"


def test_green_screen_is_replaced(tmp_path):
    import subprocess

    from heisenbot.media import ffmpeg_bin, green_ratio

    src = tmp_path / "gs.mp4"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x00d000:s=320x240:d=1",
                    "-f", "lavfi", "-i", "color=c=red:s=80x100:d=1", "-filter_complex", "[0][1]overlay=120:70",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    assert green_ratio(src) > 0.9
    out = render(src, tmp_path / "o", "Someone", "huh", "", "", {**DEFAULTS, "tts": False})
    assert green_ratio(out) < 0.2


from heisenbot.capstone import CapstoneDesk, extract_titles


@pytest.mark.parametrize("text,titles", [
    ("Title: Smart Attendance Tracker using RFID (IoT)", ["Smart Attendance Tracker using RFID"]),
    ('title ko: "BarangayConnect: A Web-Based Complaint System"', ["BarangayConnect: A Web-Based Complaint System"]),
    ("Proposed title - LipaEats: Food Waste App\nDescription: shelters\nDomain: Social Good", ["LipaEats: Food Waste App"]),
    ("Mga title namin:\n1. Clinic Queue Manager\n2) Library Seat Finder\n- TBD\n3. Parking Space Detector",
     ["Clinic Queue Manager", "Library Seat Finder", "Parking Space Detector"]),
    ("!title Tricycle Fare Estimator", ["Tricycle Fare Estimator"]),
    ("Capstone idea: AI Tutor for Filipino Math", ["AI Tutor for Filipino Math"]),
    ("Title 2: Dormitory Management System", ["Dormitory Management System"]),
    ("Our capstone titles:\n\u2022 Smart Waste Bin\n\u2022 Flood Alert SMS System\nok na ba", ["Smart Waste Bin", "Flood Alert SMS System"]),
    ("what title should we use?", []),
    ("Title: ?", []),
    ("anong title nyo guys", []),
    ("haha title daw lol", []),
    ("title: https://docs.google.com/x", []),
    ("Title: TBD", []),
    ("1. Clinic Queue\n2. Library", []),
])
def test_extract_titles(text, titles):
    assert [t["title"] for t in extract_titles(text)] == titles


def test_extract_fields():
    t = extract_titles("Proposed title - LipaEats: Food Waste App\nDescription: connects shelters\nDomain: Social Good")[0]
    assert t["domain"] == "Social Good" and t["summary"] == "connects shelters"
    assert extract_titles("Title: Smart Bin (IoT)")[0]["domain"] == "IoT"


def _fake_site(token="tok-0123456789abcdef0123456789"):
    from aiohttp import web

    store, calls = [], []

    async def intake(req):
        calls.append(req.method)
        if req.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"error": "Invalid intake token."}, status=401)
        if req.method == "GET":
            return web.json_response({"ok": True, "drafts": len(store)})
        body = await req.json()
        added, skipped = [], []
        for d in body["drafts"]:
            if len(d["title"]) < 3:
                return web.json_response({"error": "Title needs at least three characters and a letter."}, status=400)
            if d["title"].lower() in (s.lower() for s in store):
                skipped.append({"title": d["title"], "reason": "already posted"})
            else:
                store.append(d["title"])
                added.append(d["title"])
        return web.json_response({"added": added, "skipped": skipped}, status=201 if added else 200)

    app = web.Application()
    app.router.add_route("*", "/api/intake", intake)
    return app, store, calls, token


def test_capstone_desk_collects_and_syncs(tmp_path):
    from aiohttp import web

    async def go():
        app, store, calls, token = _fake_site()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            c = Config(tmp_path / "c.json")
            logs = []
            desk = CapstoneDesk(c, lambda *a, **k: logs.append(a), path=tmp_path / "cap.json")
            assert desk.collect(Message("1", "Kurt Atienza", "Title: Smart Clinic Queue")) != []
            assert desk.collect(Message("2", "Francis Gerald", "title: smart   clinic queue")) == []
            assert desk.collect(Message("3", "You", "Title: Own Message Idea", outgoing=True)) == []
            desk.add([{"title": "Already There Idea", "domain": "", "summary": ""}], "Someone")
            assert desk.items[-1]["author"] == ""
            store.append("Already There Idea")
            r = await desk.sync()
            assert r["error"] == "No intake token set" and desk.counts()["pending"] == 2
            c.update({"capstone": {"site_url": f"http://127.0.0.1:{port}", "token": "wrong-token"}})
            r = await desk.sync()
            assert r["failed"] == 2 and "Invalid intake token" in desk.last_error
            c.update({"capstone": {"token": token}})
            assert (await desk.check())["drafts"] == 1
            r = await desk.sync()
            assert r == {"added": 1, "skipped": 1, "failed": 0}
            assert {i["title"]: i["status"] for i in desk.items} == {"Smart Clinic Queue": "posted", "Already There Idea": "skipped"}
            assert store == ["Already There Idea", "Smart Clinic Queue"]
            again = CapstoneDesk(c, lambda *a, **k: None, path=tmp_path / "cap.json")
            assert again.counts()["posted"] == 1 and again.items[0]["author"] == "Kurt Atienza"
            assert await desk.sync() == {"added": 0, "skipped": 0, "failed": 0}
            c.update({"capstone": {"site_url": "http://127.0.0.1:1"}})
            desk.add([{"title": "Offline Idea Here", "domain": "", "summary": ""}])
            r = await desk.sync()
            assert r["failed"] == 1 and desk.find("Offline Idea Here")["status"] == "pending"
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_capstone_config_validation():
    with pytest.raises(ValueError):
        validate({"capstone": {"site_url": "http://evil.example.com"}})
    v = validate({"capstone": {"site_url": "https://gawk-capstone-sti-lipa.pages.dev/", "token": "abc def<>", "enabled": 0}})
    assert v["capstone"] == {"site_url": "https://gawk-capstone-sti-lipa.pages.dev", "token": "abcdef", "enabled": False}


def test_history_scan_on_mock_chat(tmp_path):
    pytest.importorskip("playwright")
    from heisenbot.messenger import Messenger

    async def go():
        c = Config(tmp_path / "c.json")
        import heisenbot.messenger as mod

        mod.PROFILE = tmp_path / "profile2"
        mm = Messenger(c, lambda *a: None)
        await mm.start(headless=True, url=(Path(__file__).parent / "mock_chat.html").as_uri())
        try:
            await mm.page.evaluate("addIncoming('Kurt Atienza', 'Title: Smart Clinic Queue', 'Kurt')")
            await mm.page.evaluate("for (let i = 0; i < 40; i++) addIncoming('Francis Gerald', 'filler message ' + i)")
            await mm.page.evaluate("addIncoming('Francis Gerald', 'Our titles:\\n1. Flood Alert SMS\\n2. Library Seat Finder')")
            msgs = await mm.scan_history(pause=0.2)
            texts = [x.text for x in msgs]
            assert texts[0] == "test" and "Title: Smart Clinic Queue" in texts and texts[-1].startswith("Our titles")
            assert len(texts) == len(set(texts)) and not any(t == "my own message lol" for t in texts)
            found = [t["title"] for m_ in msgs for t in extract_titles(m_.text)]
            assert found == ["Smart Clinic Queue", "Flood Alert SMS", "Library Seat Finder"]
            assert await mm.page.evaluate("(() => { const p = document.getElementById('pane'); return p.scrollHeight - p.scrollTop - p.clientHeight < 5; })()")
        finally:
            await mm.stop()

    asyncio.run(go())


@pytest.mark.parametrize("text,titles", [
    ("Title 1: IoT-Based Environmental Monitoring and Automated Stress Mitigation System for Layer Poultry Farms:\n\n1. Primary Academic Research\n\nCitation: Rawdhan (2025).",
     ["IoT-Based Environmental Monitoring and Automated Stress Mitigation System for Layer Poultry Farms"]),
    ("Code & Technical Plagiarism\n\nCodeProvenance (AST + Git Telemetry Engine)\n\nDifferentiator: Parses ASTs.\n\nMathEquate (LaTeX & Mathematical Proof Normalizer)\n\nDifferentiator: Parses proofs.",
     ["CodeProvenance: AST + Git Telemetry Engine", "MathEquate: LaTeX & Mathematical Proof Normalizer"]),
    ("Title: Student Incident Reporting and Disciplinary Records Management System  at STI LIPA",
     ["Student Incident Reporting and Disciplinary Records Management System at STI LIPA"]),
    ("Title:\nSmart Parking Finder", ["Smart Parking Finder"]),
    ('parang ganto Title: X, title ko: "X", Proposed title - X, !title X, or a list under Our titles', []),
    ("new update kay walter auto fill up sya sa site, yung mga titles nandito lang pa cache nya hehe", []),
])
def test_extract_real_group_chat_messages(text, titles):
    assert [t["title"] for t in extract_titles(text)] == titles


def test_reader_on_2026_layout(tmp_path):
    pytest.importorskip("playwright")
    from heisenbot.messenger import Messenger

    async def go():
        c = Config(tmp_path / "c.json")
        import heisenbot.messenger as mod

        mod.PROFILE = tmp_path / "profile3"
        mm = Messenger(c, lambda *a: None)
        await mm.start(headless=True, url=(Path(__file__).parent / "mock_chat_2026.html").as_uri())
        try:
            await mm.expand()
            rows = (await mm.read_raw())["rows"]
            senders = {r["sender"] for r in rows}
            texts = [r["text"] for r in rows]
            assert "Add" not in senders and "Someone" not in senders
            assert not any("deleted a message" in t or "7:10" in t or "changed the theme" in t for t in texts)
            assert rows[0] == {"text": "Needed nalang ng plus one", "sender": "Charles Angelo L. Rodelas", "outgoing": False, "media": False}
            fwd = next(r for r in rows if "CodeProvenance" in r["text"])
            assert fwd["sender"] == "Charles Angelo L. Rodelas" and "ZeroTrust-Similarity" in fwd["text"]
            poultry = next(r for r in rows if r["text"].startswith("Title 1:"))
            assert poultry["sender"] == "Francis Gerald Gapas Tañedo" and "Validates the hardware" in poultry["text"]
            titles = [t["title"] for r in rows for t in extract_titles(r["text"])]
            assert titles == ["CodeProvenance: AST + Git Telemetry Engine", "ZeroTrust-Similarity: LSH Cryptographic Document Hasher",
                              "IoT-Based Environmental Monitoring and Automated Stress Mitigation System for Layer Poultry Farms"]
            await mm.prime()
            await mm.page.evaluate("addIncoming('Kurt Atienza', 'Title: Student Incident Reporting System at STI LIPA')")
            got = await mm.poll()
            assert [(x.sender, x.text) for x in got] == [("Kurt Atienza", "Title: Student Incident Reporting System at STI LIPA")]
        finally:
            await mm.stop()

    asyncio.run(go())
