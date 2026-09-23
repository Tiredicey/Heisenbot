import asyncio
import os
import random
import re

from playwright.async_api import async_playwright

from .config import PROFILE
from .engine import Message

HOME = "https://www.facebook.com/messages/"

EXTRACT_JS = r"""
({sel, limit}) => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const comps = Array.from(document.querySelectorAll(sel.composer)).filter(vis);
  const comp = comps[comps.length - 1];
  let pane = null;
  if (comp) {
    const cr = comp.getBoundingClientRect();
    const cx = cr.left + cr.width / 2;
    let best = null;
    for (const el of document.querySelectorAll("div")) {
      const oy = getComputedStyle(el).overflowY;
      if (!(oy === "auto" || oy === "scroll") || el.scrollHeight <= el.clientHeight + 4) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 150 || r.height < 120 || r.left > cx || r.right < cx || r.top > cr.top) continue;
      if (!el.querySelector(sel.text)) continue;
      if (!best || r.width * r.height < best.a) best = {el, a: r.width * r.height};
    }
    pane = best && best.el;
  }
  if (!pane) pane = document.querySelector(sel.container) || document.body;
  const box = pane.getBoundingClientRect();
  const mid = box.left + box.width / 2;
  const skip = /^(\d{1,2}:\d{2}(\s?[ap]m)?|today|yesterday|sent|seen|delivered|edited|enter|you sent|original message|replied to .{0,60}|.{0,40} (added|removed|left|named|changed|set|pinned|unsent|created|joined) .{0,80})$/i;
  const leafOf = n => !Array.from(n.children).some(ch => (ch.innerText || "").trim());
  const nodes = Array.from(pane.querySelectorAll(sel.text + ", span, h4, h5")).filter(n =>
    vis(n) && leafOf(n) && !n.closest('[aria-hidden="true"]') && !(comp && comp.contains(n)));
  const bubbleFonts = nodes.filter(n => n.matches(sel.text)).map(n => parseFloat(getComputedStyle(n).fontSize)).filter(Boolean);
  const bubbleFont = bubbleFonts.length ? Math.max(...bubbleFonts) : 15;
  const clean = t => t.replace(/\s*(…|\.\.\.)\s*$/, "").trim();
  const alts = Array.from(pane.querySelectorAll("img[alt]")).map(im => clean(im.alt.replace(/'s profile picture$/i, "")))
    .filter(a => a && a.length < 60 && !/seen by|sticker|gif|emoji/i.test(a));
  const full = h => alts.find(a => a.toLowerCase().startsWith(h.toLowerCase())) || h;
  const out = [];
  let current = "";
  const used = new Set();
  for (const n of nodes) {
    if (used.has(n)) continue;
    const t = (n.innerText || "").trim();
    if (!t || skip.test(t)) continue;
    const r = n.getBoundingClientRect();
    const center = r.left + r.width / 2;
    const font = parseFloat(getComputedStyle(n).fontSize) || bubbleFont;
    const isBubble = n.matches(sel.text) && font >= bubbleFont - 1.5;
    if (!isBubble) {
      if (font < bubbleFont - 1 && t.length < 60 && r.left < mid && !/\d{1,2}:\d{2}/.test(t)) current = full(clean(t));
      continue;
    }
    if (Math.abs(center - mid) < box.width * 0.1 && r.width < box.width * 0.7) continue;
    const outgoing = center > mid + box.width * 0.06;
    if (outgoing) { out.push({text: t, sender: "You", outgoing: true, media: false}); continue; }
    let sender = current;
    if (!sender) {
      let el = n;
      for (let i = 0; i < 9 && el && el !== pane && !sender; i++) {
        el = el.parentElement;
        if (!el) break;
        const img = Array.from(el.querySelectorAll("img[alt]")).find(im => {
          const ir = im.getBoundingClientRect();
          return ir.width > 0 && ir.width <= 48 && im.alt && im.alt.length < 60 && ir.left < r.left && !/seen by|sticker|gif|emoji/i.test(im.alt);
        });
        if (img) sender = clean(img.alt.replace(/'s profile picture$/i, ""));
      }
      if (sender) current = sender;
    }
    out.push({text: t, sender: sender || "Someone", outgoing: false, media: false});
  }
  return {rows: out.slice(-limit), pane: pane !== document.body && pane !== document.querySelector(sel.container), composer: !!comp};
}
"""


class Messenger:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.pw = None
        self.ctx = None
        self.page = None
        self.snapshot_prev = []

    @property
    def open(self):
        return self.page is not None and not self.page.is_closed()

    async def start(self, headless=None, url=None):
        if self.open:
            await self.page.bring_to_front()
            return
        headless = self.cfg["headless"] if headless is None else headless
        if os.environ.get("HEISENBOT_SERVER") or (os.name == "posix" and not os.environ.get("DISPLAY")
                                                   and not os.uname().sysname == "Darwin"):
            headless = True
        self.pw = await async_playwright().start()
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.ctx = await self.pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--autoplay-policy=no-user-gesture-required"],
            ignore_default_args=["--enable-automation"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        self.page.set_default_timeout(20000)
        await self.page.goto(url or self.cfg["thread_url"] or HOME, wait_until="domcontentloaded")

    async def stop(self):
        for obj in (self.ctx, self.pw):
            try:
                if obj is self.pw and obj:
                    await obj.stop()
                elif obj:
                    await obj.close()
            except Exception:
                pass
        self.pw = self.ctx = self.page = None

    def current_url(self):
        return self.page.url if self.open else ""

    async def logged_in(self):
        if not self.open:
            return False
        if re.search(r"/login|checkpoint|recover", self.page.url):
            return False
        return await self.page.locator(self.cfg["selectors"]["composer"]).count() > 0 or "/messages" in self.page.url

    async def goto_thread(self):
        url = self.cfg["thread_url"]
        if url and not self.page.url.startswith(url.split("?")[0]):
            await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.locator(self.cfg["selectors"]["composer"]).first.wait_for(state="visible", timeout=60000)

    async def read_raw(self, limit=60):
        for _ in range(3):
            try:
                return await self.page.evaluate(EXTRACT_JS, {"sel": self.cfg["selectors"], "limit": limit})
            except Exception as e:
                msg = str(e).lower()
                if "context was destroyed" in msg or "navigat" in msg:
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)
                    continue
                self.log("warn", f"read failed: {e}")
                break
        return {"rows": [], "pane": False, "composer": False}

    async def read(self, limit=60):
        return (await self.read_raw(limit))["rows"]

    @staticmethod
    def _key(r):
        return (r["sender"], r["text"], r["outgoing"])

    @staticmethod
    def new_tail(prev, cur):
        for k in range(min(len(prev), 8), 0, -1):
            tail = prev[-k:]
            for end in range(len(cur), k - 1, -1):
                if cur[end - k:end] == tail:
                    return list(range(end, len(cur)))
        return None

    async def prime(self):
        self.snapshot_prev = [self._key(r) for r in await self.read()]

    async def poll(self):
        rows = await self.read()
        if not rows:
            return []
        keys = [self._key(r) for r in rows]
        if not self.snapshot_prev:
            self.snapshot_prev = keys
            return []
        idx = self.new_tail(self.snapshot_prev, keys)
        self.snapshot_prev = keys
        if idx is None:
            self.log("info", "Chat view changed a lot; re-synced without replying to old messages")
            return []
        return [Message(id=str(i), sender=rows[i]["sender"], text=rows[i]["text"])
                for i in idx if not rows[i]["outgoing"] and rows[i]["text"]]

    async def _composer(self):
        c = self.page.locator(self.cfg["selectors"]["composer"]).last
        await c.wait_for(state="visible")
        await c.click()
        return c

    async def send_text(self, text):
        c = await self._composer()
        for chunk in re.findall(r".{1,40}", text, flags=re.S):
            await c.type(chunk, delay=random.randint(18, 45))
        await asyncio.sleep(random.uniform(0.2, 0.6))
        await self.page.keyboard.press("Enter")

    async def send_file(self, path, caption=""):
        await self._composer()
        inputs = self.page.locator(self.cfg["selectors"]["file_input"])
        n = await inputs.count()
        if n == 0:
            raise RuntimeError("attachment input not found; open the chat and try Diagnose")
        target = inputs.last
        for i in range(n):
            acc = (await inputs.nth(i).get_attribute("accept")) or ""
            if "video" in acc or "*" in acc or acc == "":
                target = inputs.nth(i)
        await target.set_input_files(str(path))
        size_mb = path.stat().st_size / 1_048_576
        await asyncio.sleep(min(12, 1.8 + size_mb * 0.8))
        c = await self._composer()
        if caption:
            await c.type(caption, delay=random.randint(15, 35))
        await self.page.keyboard.press("Enter")
        await asyncio.sleep(1.0)

    async def export_login(self):
        return await self.ctx.storage_state()

    async def import_login(self, state):
        cookies = [c for c in state.get("cookies", []) if re.search(r"(facebook|messenger)\.com$", c.get("domain", ""))]
        if not any(c.get("name") == "c_user" for c in cookies):
            raise ValueError("That file has no Facebook login in it")
        await self.ctx.add_cookies(cookies)
        await self.page.goto(self.cfg["thread_url"] or HOME, wait_until="domcontentloaded")
        return len(cookies)

    def needs_login(self):
        return bool(self.open and re.search(r"/login|checkpoint|two_step|recover|/r\.php", self.page.url))

    async def snapshot(self, quality=60):
        return await self.page.screenshot(type="jpeg", quality=quality)

    async def remote(self, action, **kw):
        p = self.page
        vp = p.viewport_size or {"width": 1280, "height": 900}
        if action == "click":
            x, y = float(kw["x"]) * vp["width"], float(kw["y"]) * vp["height"]
            await p.mouse.click(x, y, delay=random.randint(40, 110))
        elif action == "scroll":
            await p.mouse.wheel(0, float(kw.get("dy", 400)))
        elif action == "type":
            await p.keyboard.type(str(kw.get("text", ""))[:500], delay=random.randint(25, 60))
        elif action == "key":
            key = str(kw.get("key", ""))
            if key not in ("Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
                raise ValueError("unsupported key")
            await p.keyboard.press(key)
        elif action == "goto":
            url = str(kw.get("url", "")).strip() or HOME
            if not re.match(r"^https://(www\.|m\.)?(facebook|messenger)\.com/", url):
                raise ValueError("Only facebook.com links are allowed")
            await p.goto(url, wait_until="domcontentloaded")
        elif action == "back":
            await p.go_back()
        elif action == "reload":
            await p.reload(wait_until="domcontentloaded")
        else:
            raise ValueError("unknown action")

    async def screenshot(self, path):
        await self.page.screenshot(path=str(path))
        return path
