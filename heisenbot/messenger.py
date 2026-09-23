import asyncio
import os
import random
import re

from playwright.async_api import async_playwright

from .config import PROFILE
from .engine import Message

HOME = "https://www.facebook.com/messages/"

EXTRACT_JS = r"""
({sel, mark, limit}) => {
  const main = document.querySelector(sel.container) || document.body;
  const rows = Array.from(main.querySelectorAll(sel.row));
  const box = main.getBoundingClientRect();
  const mid = box.left + box.width / 2;
  const out = [];
  let lastSender = window.__hbLastSender || "";
  const tail = rows.slice(-limit);
  for (const row of tail) {
    const texts = Array.from(row.querySelectorAll(sel.text))
      .filter(n => !n.closest('[role="button"] [aria-hidden="true"]'))
      .map(n => ({n, t: (n.innerText || "").trim()}))
      .filter(x => x.t);
    let sender = "";
    const img = Array.from(row.querySelectorAll("img[alt]")).find(i => {
      const r = i.getBoundingClientRect();
      return r.width > 0 && r.width <= 48 && i.alt && i.alt.length < 60 && !/seen|sticker|gif|emoji/i.test(i.alt);
    });
    if (img) sender = img.alt.trim();
    const header = Array.from(row.querySelectorAll("span, h4, h5")).find(s => {
      const t = (s.innerText || "").trim();
      return t && t.length < 50 && s.childElementCount === 0 && !s.closest(sel.text) &&
        !/^(\d{1,2}:\d{2}|sent|seen|delivered|edited|replied|you sent|enter)/i.test(t) && getComputedStyle(s).fontSize.replace("px","") < 14;
    });
    if (!sender && header) sender = header.innerText.trim();
    const bubbles = texts.filter(x => !sender || x.t !== sender);
    const body = bubbles.length ? bubbles[bubbles.length - 1] : null;
    const media = row.querySelector("video, img[src*='scontent'], a[href*='/attachment']");
    if (!body && !media) continue;
    const anchor = (body ? body.n : media);
    const r = anchor.getBoundingClientRect();
    const outgoing = r.width > 0 && (r.left + r.width / 2) > mid + box.width * 0.08 && !img;
    if (sender) lastSender = sender;
    else if (!outgoing) sender = lastSender;
    const seen = row.getAttribute(mark) === "1";
    row.setAttribute(mark, "1");
    out.push({text: body ? body.t : "", sender: outgoing ? "You" : (sender || "Someone"), outgoing, seen, media: !!media});
  }
  window.__hbLastSender = lastSender;
  return out;
}
"""


class Messenger:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.pw = None
        self.ctx = None
        self.page = None
        self.mark = "data-hb-seen"
        self.recent = []

    @property
    def open(self):
        return self.page is not None and not self.page.is_closed()

    async def start(self, headless=None, url=None):
        if self.open:
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

    async def read(self, limit=12):
        try:
            rows = await self.page.evaluate(EXTRACT_JS, {"sel": self.cfg["selectors"], "mark": self.mark, "limit": limit})
        except Exception as e:
            self.log("warn", f"read failed: {e}")
            return []
        return rows

    async def prime(self):
        await self.read(limit=400)

    async def poll(self):
        fresh = []
        for r in await self.read():
            if r["seen"] or r["outgoing"] or not r["text"]:
                continue
            sig = (r["sender"], r["text"])
            if sig in self.recent:
                continue
            self.recent.append(sig)
            self.recent = self.recent[-60:]
            fresh.append(Message(id=str(hash(sig)), sender=r["sender"], text=r["text"], outgoing=False))
        return fresh

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
