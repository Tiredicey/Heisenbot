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
  document.querySelectorAll("[data-hb-pane]").forEach(el => { if (el !== pane) delete el.dataset.hbPane; });
  if (pane !== document.body) pane.dataset.hbPane = "1";
  const box = pane.getBoundingClientRect();
  const mid = box.left + box.width / 2;
  const TIME = /^((mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s*)?((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(,\s*\d{4})?,?\s*)?(at\s+)?\d{1,2}:\d{2}\s?([ap]\.?m\.?)?$/i;
  const SKIP = /^(today|yesterday|sent|seen|seen by .{1,80}|delivered|edited|enter|you sent|original message:?|see more|see less|active now|typing\.*|.{1,60} (?:deleted|unsent|removed) a message|you (?:replied|deleted|unsent|forwarded|removed|added|named|changed|pinned|reacted|left).{0,160}|\d+ (?:new )?messages?)$/i;
  const SYSVERB = /^.{1,60} (added|removed|left|named|changed|set|pinned|unsent|deleted|created|joined|started|ended|missed|reacted|turned|updated|made|is now) .{0,160}$/i;
  const LEAD = /^(.+?) (forwarded a message|replied to .{1,60}|sent an attachment|sent a photo|sent a video)\.?$/i;
  const UI = new Set(["add", "reply", "forward", "react", "more", "remove", "edit", "edited", "pin", "pinned", "unpin", "copy", "delete", "report", "seen", "sent", "enter", "like", "love", "haha", "wow", "sad", "angry", "view", "open", "search", "mute", "call", "video", "info", "message", "messages", "chat", "members", "send", "close", "back", "you", "see more", "see less", "active now", "original message", "chat info", "customize chat", "media", "files", "links", "notifications", "group options", "chat members", "privacy & support"]);
  const clean = t => t.replace(/\s*(…|\.\.\.)\s*$/, "").replace(/\s*See more\s*$/i, "").trim();
  const nameLike = t => t.length >= 2 && t.length <= 60 && /^[\p{Lu}\d][\p{L}\p{M}\d.'’\- ]*$/u.test(t) && t.split(/\s+/).length <= 6 && !UI.has(t.toLowerCase()) && !TIME.test(t);
  const altOf = el => clean((el.getAttribute("alt") || el.getAttribute("aria-label") || "").replace(/'s profile picture$/i, "").replace(/^profile picture of /i, ""));
  const avatarSel = "img[alt], svg[aria-label], [role='img'][aria-label]";
  const alts = Array.from(pane.querySelectorAll(avatarSel)).map(altOf).filter(nameLike);
  const full = h => alts.find(a => a.toLowerCase().startsWith(h.toLowerCase())) || h;
  const skipNode = n => !vis(n) || n.closest('[aria-hidden="true"]') || (comp && comp.contains(n));
  const textEls = Array.from(pane.querySelectorAll(sel.text)).filter(n => !skipNode(n));
  const textSet = new Set(textEls);
  const outer = textEls.filter(n => { for (let p = n.parentElement; p && p !== pane; p = p.parentElement) if (textSet.has(p)) return false; return true; });
  const outerSet = new Set(outer);
  const sizes = {};
  for (const n of outer) { const t = (n.innerText || "").trim(); if (t.length < 2) continue; const f = Math.round(parseFloat(getComputedStyle(n).fontSize) || 15); sizes[f] = (sizes[f] || 0) + Math.min(t.length, 200); }
  const bubbleFont = +(Object.entries(sizes).sort((a, b) => b[1] - a[1])[0] || [15])[0];
  const avatarNear = (n, r) => {
    const row = n.closest("[role='row']");
    const scopes = [];
    if (row && pane.contains(row) && row !== pane) scopes.push(row);
    else for (let el = n.parentElement, i = 0; el && el !== pane && i < 6; el = el.parentElement, i++) { if (el.getBoundingClientRect().height > r.height + 140) break; scopes.push(el); }
    for (const sc of scopes) {
      const hit = Array.from(sc.querySelectorAll(avatarSel)).find(im => { const ir = im.getBoundingClientRect(); return ir.width > 0 && ir.width <= 56 && ir.right <= r.left + 4 && nameLike(altOf(im)); });
      if (hit) return altOf(hit);
    }
    return "";
  };
  const out = [];
  let current = "", cand = null;
  const nodes = Array.from(pane.querySelectorAll(sel.text + ", span, h4, h5, a"));
  for (const n of nodes) {
    if (skipNode(n)) continue;
    const isBubbleEl = outerSet.has(n);
    if (!isBubbleEl) {
      let inside = false;
      for (let p = n.parentElement; p && p !== pane; p = p.parentElement) if (outerSet.has(p)) { inside = true; break; }
      if (inside || Array.from(n.children).some(ch => (ch.innerText || "").trim())) continue;
    }
    const raw = (n.innerText || "").trim();
    const t = clean(raw);
    if (!t || TIME.test(t) || SKIP.test(t)) continue;
    const r = n.getBoundingClientRect();
    const gapL = r.left - box.left, gapR = box.right - r.right;
    const centered = Math.abs(gapL - gapR) < box.width * 0.1 && r.width < box.width * 0.6;
    const font = parseFloat(getComputedStyle(n).fontSize) || bubbleFont;
    const lead = t.length < 140 ? t.match(LEAD) : null;
    if (lead) { const who = lead[1].trim(); current = /^you$/i.test(who) ? "" : full(who); cand = null; continue; }
    if (centered && (SYSVERB.test(t) || t.length < 90)) { current = ""; cand = null; continue; }
    if (!isBubbleEl || font < bubbleFont - 1.5) {
      if (t.length < 60 && r.left < mid && nameLike(t)) cand = {name: full(t), bottom: r.bottom};
      continue;
    }
    const outgoing = gapR < gapL;
    if (outgoing) { out.push({text: t, sender: "You", outgoing: true, media: false}); current = ""; cand = null; continue; }
    if (cand && r.top >= cand.bottom - 8 && r.top - cand.bottom < 90) current = cand.name;
    cand = null;
    const av = avatarNear(n, r);
    if (av) current = full(av);
    out.push({text: t, sender: current || "Someone", outgoing: false, media: false});
  }
  return {rows: out.slice(-limit), pane: pane !== document.body && pane !== document.querySelector(sel.container), composer: !!comp};
}
"""

EXPAND_JS = r"""
() => {
  const p = document.querySelector("[data-hb-pane]");
  if (!p) return 0;
  let n = 0;
  for (const el of p.querySelectorAll("[role='button'], span, div")) {
    if (el.children.length > 1) continue;
    const t = (el.innerText || "").trim();
    if (!/^see more$/i.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    (el.closest("[role='button']") || el).click();
    n++;
    if (n >= 15) break;
  }
  return n;
}
"""


SCROLL_JS = r"""
(dir) => {
  const p = document.querySelector("[data-hb-pane]");
  if (!p) return null;
  if (dir === "bottom") p.scrollTop = p.scrollHeight;
  else p.scrollTop = Math.max(0, p.scrollTop - Math.max(200, p.clientHeight * 0.85));
  return {top: p.scrollTop, height: p.scrollHeight};
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
        self.resynced = []

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

    async def expand(self):
        try:
            if not await self.page.evaluate("() => !!document.querySelector('[data-hb-pane]')"):
                await self.read_raw(limit=1)
            n = await self.page.evaluate(EXPAND_JS)
            if n:
                await asyncio.sleep(0.6)
            return n
        except Exception:
            return 0

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
            self.resynced = [Message(id=f"r{i}", sender=r["sender"], text=r["text"]) for i, r in enumerate(rows) if not r["outgoing"] and r["text"]]
            return []
        return [Message(id=str(i), sender=rows[i]["sender"], text=rows[i]["text"])
                for i in idx if not rows[i]["outgoing"] and rows[i]["text"]]

    async def scan_history(self, max_steps=120, pause=1.3, stop=None):
        seen, order = set(), []

        def take(rows):
            fresh = []
            for r in rows:
                k = self._key(r)
                if k not in seen and not r["outgoing"] and r["text"]:
                    seen.add(k)
                    fresh.append(Message(id=f"h{len(order) + len(fresh)}", sender=r["sender"], text=r["text"]))
            return fresh

        await self.expand()
        batch = take((await self.read_raw(limit=500))["rows"])
        order.extend(batch)
        still, last = 0, None
        for _ in range(max_steps):
            if stop and stop():
                break
            pos = await self.page.evaluate(SCROLL_JS, "up")
            if pos is None:
                break
            await asyncio.sleep(pause)
            await self.expand()
            rows = (await self.read_raw(limit=500))["rows"]
            fresh = take(rows)
            order[:0] = fresh
            now = (pos["top"], pos["height"])
            still = still + 1 if (pos["top"] <= 0 and now == last and not fresh) else 0
            last = now
            if still >= 3:
                break
        await self.page.evaluate(SCROLL_JS, "bottom")
        await asyncio.sleep(0.8)
        return order

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
