import asyncio
import json
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .config import CACHE, LOGS, Config
from .engine import ClipBank, Engine, Message
from .media import render, speech_text


class LoginRequired(Exception):
    pass


class Bot:
    def __init__(self, cfg=None):
        self.cfg = cfg or Config()
        self.bank = ClipBank()
        self.engine = Engine(self.cfg, self.bank)
        self.events = deque(maxlen=400)
        self.messenger = None
        self.task = None
        self.state = "idle"
        self.sent = 0
        self.fail_sends = 0
        self.started_at = None
        self.lock = asyncio.Lock()
        self.logfile = LOGS / f"{datetime.now():%Y-%m-%d}.jsonl"

    async def notify(self, title, text, priority="default"):
        topic = self.cfg["ntfy_topic"]
        if not topic:
            return
        try:
            import aiohttp

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                await s.post(f"https://ntfy.sh/{topic}", data=text.encode(),
                             headers={"Title": title, "Priority": priority, "Tags": "test_tube"})
        except Exception as e:
            print(f"[warn] notify failed: {e}", flush=True)

    def cleanup(self, keep_days=3, max_files=300):
        folder = CACHE / "renders"
        if not folder.exists():
            return 0
        files = sorted(folder.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        cutoff = time.time() - keep_days * 86400
        gone = 0
        for i, f in enumerate(files):
            if i >= max_files or f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                gone += 1
        return gone

    def set_snooze(self, mode, minutes, label=""):
        minutes = max(0, min(24 * 60, int(minutes)))
        if mode == "off" or minutes == 0:
            self.cfg.update({"snooze": {"mode": "off", "until": 0, "label": ""}})
            self.log("ok", "Walter is back to normal")
            return "Walter is back to normal."
        until = time.time() + minutes * 60
        self.cfg.update({"snooze": {"mode": mode, "until": until, "label": label}})
        end = datetime.fromtimestamp(until).strftime("%I:%M %p").lstrip("0")
        text = f"Walter {'is paused' if mode == 'pause' else 'only answers tags'} until {end}."
        self.log("ok", text)
        return text

    def parse_phone(self, text):
        t = text.strip().lower()
        m = re.match(r"^(pause|stop|quiet|mentions|tags|class|meeting|date|out|resume|on|normal|status)\s*(\d+)?\s*([mh]|min|mins|hr|hrs|hour|hours)?", t)
        if not m:
            return None
        word, num, unit = m.group(1), m.group(2), m.group(3) or "m"
        preset = {"class": ("mentions", 120), "meeting": ("mentions", 90), "date": ("pause", 240), "out": ("pause", 120)}
        if word in ("resume", "on", "normal"):
            return ("off", 0)
        if word == "status":
            return ("status", 0)
        mode, default = preset.get(word, ("pause" if word in ("pause", "stop", "quiet") else "mentions", 60))
        mins = int(num) * (60 if unit.startswith("h") else 1) if num else default
        return (mode, mins)

    async def phone_loop(self):
        import aiohttp

        since = str(int(time.time()))
        while True:
            topic = self.cfg["ntfy_topic"]
            if not (topic and self.cfg["phone_control"]):
                await asyncio.sleep(30)
                continue
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                    async with s.get(f"https://ntfy.sh/{topic}-cmd/json", params={"poll": "1", "since": since}) as r:
                        body = await r.text()
                for line in body.splitlines():
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") != "message":
                        continue
                    since = ev.get("id", since)
                    cmd = self.parse_phone(ev.get("message", ""))
                    if not cmd:
                        await self.notify("Walter", "Try: pause 2h, class, date, mentions 90m, resume, status", "low")
                        continue
                    if cmd[0] == "status":
                        mode, until = self.engine.snooze_state()
                        st = self.status()
                        msg = f"{st['state']}, {'dry run' if st['dry_run'] else 'live'}, {st['sent']} sent"
                        if mode != "off":
                            msg += f", {mode} until {datetime.fromtimestamp(until).strftime('%I:%M %p').lstrip('0')}"
                        await self.notify("Walter status", msg, "low")
                    else:
                        await self.notify("Walter", self.set_snooze(*cmd), "low")
            except Exception as e:
                print(f"[warn] phone control: {e}", flush=True)
            await asyncio.sleep(15)

    async def autostart(self):
        asyncio.get_running_loop().create_task(self.phone_loop())
        self.cleanup()
        if self.cfg["live"] and self.cfg["thread_url"]:
            self.log("info", "Resuming after restart")
            try:
                await self.start()
            except Exception as e:
                self.log("error", f"Auto resume failed: {e}")

    def log(self, level, text, **extra):
        ev = {"t": time.time(), "level": level, "text": text, **extra}
        self.events.append(ev)
        with self.logfile.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        print(f"[{level}] {text}", flush=True)

    def status(self):
        return {
            "state": self.state,
            "enabled": self.engine.enabled,
            "dry_run": self.cfg["dry_run"],
            "sent": self.sent,
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
            "browser": bool(self.messenger and self.messenger.open),
            "needs_login": bool(self.messenger and self.messenger.needs_login()),
            "live": self.cfg["live"],
            "snooze": {**dict(zip(("mode", "until"), self.engine.snooze_state())), "label": self.cfg["snooze"].get("label", "")},
            "care_until": self.engine.care_until if self.engine.care_until > time.time() else 0,
            "url": self.messenger.current_url() if self.messenger and self.messenger.open else "",
            "thread_url": self.cfg["thread_url"],
            "clips": {k: len(v) for k, v in self.bank.index().items()},
        }

    async def open_browser(self, headless=False, url=None):
        from .messenger import Messenger

        if not self.messenger:
            self.messenger = Messenger(self.cfg, self.log)
        if self.messenger.open:
            await self.messenger.start()
            return
        await self.messenger.start(headless=headless, url=url)
        self.log("info", "Browser opened. Log in with the burner account, then open the group chat.")

    async def capture_thread(self):
        if not (self.messenger and self.messenger.open):
            raise RuntimeError("Open the browser first")
        url = self.messenger.current_url().split("?")[0]
        if "/messages/" not in url and "/t/" not in url:
            raise RuntimeError("Open the group chat in the browser window first")
        self.cfg.update({"thread_url": url})
        self.log("ok", f"Group chat saved: {url}")
        return url

    async def close_browser(self):
        await self.stop(keep_live=True)
        if self.messenger:
            await self.messenger.stop()
        self.log("info", "Browser closed")

    async def start(self):
        if self.task and not self.task.done():
            return
        if not self.cfg["thread_url"]:
            raise RuntimeError("Save the group chat link first (Setup step 2)")
        if not (self.messenger and self.messenger.open):
            await self.open_browser(headless=self.cfg["headless"], url=self.cfg["thread_url"])
        self.cfg.update({"live": True})
        self.task = asyncio.create_task(self._loop())

    async def stop(self, keep_live=False):
        if not keep_live:
            self.cfg.update({"live": False})
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        self.state = "idle"
        self.started_at = None

    async def _loop(self):
        m = self.messenger
        self.state = "connecting"
        try:
            if m.needs_login():
                raise LoginRequired()
            await m.goto_thread()
            await asyncio.sleep(2)
            await m.prime()
            self.state = "listening"
            self.started_at = time.time()
            self.log("ok", "Listening. Old messages are ignored; only new ones trigger Walter.")
            await self.notify("Walter is live", "Listening to the group chat.", "low")
            fails = 0
            last_clean = time.time()
            while True:
                if m.needs_login():
                    raise LoginRequired()
                if time.time() - last_clean > 21600:
                    self.cleanup()
                    last_clean = time.time()
                try:
                    for msg in await m.poll():
                        await self.handle(msg)
                    fails = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    fails += 1
                    self.log("warn", f"poll error: {e}")
                    if fails >= 5:
                        self.log("warn", "Reloading chat after repeated errors")
                        try:
                            if not m.open:
                                await m.stop()
                                await m.start(url=self.cfg["thread_url"])
                            else:
                                await m.page.reload(wait_until="domcontentloaded")
                            await m.goto_thread()
                            await m.prime()
                        except Exception as re_err:
                            self.log("warn", f"reload failed: {re_err}")
                        fails = 0
                await asyncio.sleep(self.cfg["poll_ms"] / 1000)
        except asyncio.CancelledError:
            self.log("info", "Listener stopped")
            raise
        except LoginRequired:
            self.state = "needs_login"
            self.log("error", "Facebook wants you to log in or pass a check. Open the dashboard and use Remote browser.")
            await self.notify("Walter needs you", "Facebook asked for a login or security check. Open the dashboard, Remote browser.", "high")
            await self._wait_login()
        except Exception as e:
            self.state = "error"
            self.log("error", f"Listener crashed: {e}")
            await self.notify("Walter crashed", str(e)[:200], "high")
            await asyncio.sleep(30)
            if self.cfg["live"]:
                self.log("info", "Restarting listener")
                self.task = asyncio.create_task(self._loop())

    async def _wait_login(self):
        while self.cfg["live"]:
            await asyncio.sleep(10)
            m = self.messenger
            if m and m.open and not m.needs_login() and await m.logged_in():
                self.log("ok", "Login looks good again, resuming")
                self.task = asyncio.create_task(self._loop())
                return

    async def handle(self, msg, simulate=False):
        decision, why = self.engine.decide(msg)
        self.log("msg", f"{msg.sender}: {msg.text}", why=why)
        if not decision:
            return None
        if decision.command:
            return await self._command(decision, simulate)
        clip = self.bank.pick(decision.reaction)
        if not clip:
            self.log("warn", f"No clips in clips/{decision.reaction}/ . Add MP4s there.")
            return None
        self.engine.commit(msg.sender)
        async with self.lock:
            try:
                out = await asyncio.to_thread(
                    render, clip, CACHE / "renders", msg.sender, msg.text, decision.line,
                    speech_text(self.cfg, decision), self.cfg.data)
            except Exception as e:
                self.log("error", f"Render failed: {e}")
                return None
            info = {"reaction": decision.reaction, "reason": decision.reason, "clip": clip.name,
                    "file": out.name, "line": decision.line}
            if simulate or self.cfg["dry_run"]:
                self.log("fire", f"[{'test' if simulate else 'dry run'}] {decision.reaction} for {msg.sender} ({decision.reason})", **info)
                return info
            try:
                await self.messenger.send_file(out)
                self.sent += 1
                self.log("fire", f"Sent {decision.reaction} to chat for {msg.sender} ({decision.reason})", **info)
            except Exception as e:
                self.log("error", f"Send failed: {e}")
                self.fail_sends += 1
                if self.fail_sends == 3:
                    await self.notify("Walter cannot send", f"3 sends failed in a row. Last error: {str(e)[:150]}", "high")
            else:
                self.fail_sends = 0
            return info

    async def _command(self, d, simulate):
        if d.command == "off":
            text = "Walter is cooking in the lab. (bot off)"
        elif d.command == "on":
            text = "I am awake."
        elif d.command == "list":
            text = "Reactions: " + ", ".join(f"{k} ({len(v)})" for k, v in sorted(self.bank.index().items()))
        else:
            text = self.engine.help_text()
        self.log("fire", f"command {d.command}", reply=text)
        if not simulate and not self.cfg["dry_run"] and self.messenger and self.messenger.open:
            await self.messenger.send_text(text)
        return {"command": d.command, "reply": text}

    async def test(self, sender, text):
        saved = (self.engine.last_fire, dict(self.engine.user_last), list(self.engine.history))
        self.engine.last_fire, self.engine.user_last = 0, {}
        self.engine.history.clear()
        try:
            return await self.handle(Message(id="test", sender=sender or "Tester", text=text), simulate=True)
        finally:
            self.engine.last_fire, self.engine.user_last = saved[0], saved[1]
            self.engine.history.extend(saved[2])

    async def diagnose(self):
        if not (self.messenger and self.messenger.open):
            raise RuntimeError("Open the browser first")
        m = self.messenger
        sel = self.cfg["selectors"]
        report = {"url": m.current_url(), "logged_in": await m.logged_in()}
        for k in ("composer", "file_input"):
            report[k] = await m.page.locator(sel[k]).count()
        raw = await m.read_raw(limit=8)
        rows = raw["rows"]
        report["pane"] = raw["pane"]
        report["row"] = len(rows)
        report["last_messages"] = [{"sender": r["sender"], "text": r["text"][:80], "outgoing": r["outgoing"]} for r in rows]
        shot = await m.screenshot(CACHE / "diagnose.png")
        report["screenshot"] = Path(shot).name
        seen = " | ".join(f"{'You' if r['outgoing'] else r['sender']}: {r['text'][:40]}" for r in rows[-5:]) or "nothing"
        self.log("info", f"Diagnose: chat pane={'found' if raw['pane'] else 'NOT found'} messages={len(rows)} composer={report['composer']} uploads={report['file_input']}. Walter sees: {seen}")
        return report
