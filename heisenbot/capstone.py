import asyncio
import json
import re
import threading
import time
import unicodedata
from pathlib import Path

from .config import DATA

STORE = DATA / "capstone.json"
DEFAULT_SITE = "https://gawk-capstone-sti-lipa.pages.dev"

_MOD = r"(?:my|our|proposed|suggested|draft|new|final|capstone|project|thesis|research|system|possible|another)"
_KIND = r"(?:proposal|idea|draft|suggestion|entry|option)"
_HEAD = (r"(?:(?:" + _MOD + r"\s+){0,3}(?:title|pamagat)s?(?:\s+" + _KIND + r"s?)?"
         r"|capstone\s+(?:title|" + _KIND + r")s?)")
LABELED = re.compile(r"^[\W_]{0,3}" + _HEAD + r"\s*(?:ko|namin|natin|nya|niya|nila)?\s*(?:#?\d{1,2}\s*)?[:=\-\u2013\u2014]\s*(.+)$", re.I)
LIST_HEADER = re.compile(r"^[\W_]{0,3}(?:(?:mga|here are|heres|here's|ito ang|ito mga|eto)\s+)?(?:(?:our|my|the)\s+)?" + _HEAD + r"\s*(?:ko|namin|natin)?\s*[:\-\u2013\u2014]?\s*$", re.I)
COMMAND = re.compile(r"^[!#/](?:title|capstone|cap|pamagat)\s+(.+)$", re.I)
BULLET = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*\u2022\u25cf\u25aa\u2023\u2043]|[a-e][.)])\s+(.+)$")
FIELD = re.compile(r"^\s*(domain|category|field|area|description|desc|summary|problem|abstract|objective|context|note|notes)\s*[:=\-\u2013\u2014]\s*(.*)$", re.I)
QUOTES = "\"'`\u201c\u201d\u2018\u2019*_~\u00ab\u00bb"
JUNK = {"tbd", "tba", "none", "wala", "wala pa", "n/a", "na", "idk", "later", "soon", "pending", "?", "ewan", "di pa alam", "secret"}


def normalize_title(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "").strip()).lower()


def clean_title(raw):
    t = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw or "")).strip()
    t = t.strip(QUOTES + " ")
    t = re.sub(r"\s*[.,;!]+$", "", t).strip(QUOTES + " ")
    domain = ""
    m = re.search(r"\s*[(\[]([^()\[\]]{2,60})[)\]]\s*$", t)
    if m and len(t[:m.start()].strip()) >= 3:
        domain, t = m.group(1).strip(), t[:m.start()].strip(QUOTES + " ")
    return t[:240], domain[:100]


def valid_title(t):
    n = normalize_title(t)
    if len(n) < 4 or n in JUNK or n.endswith("?"):
        return False
    if not re.search(r"[^\W\d_]", n):
        return False
    if re.match(r"^(https?://|www\.)", n):
        return False
    return len(n.split()) >= 2 or len(n) >= 6


INLINE_ITEM = re.compile(r"(?:^|\s)(?:\d{1,2}[.)]|\u2022)\s+")


def _expand(text):
    out = []
    for ln in (text or "").replace("\r", "").split("\n"):
        head = re.match(r"^(.*?[:\-\u2013\u2014])\s*((?:\d{1,2}[.)]|\u2022)\s+.+)$", ln.strip())
        if head and len(INLINE_ITEM.findall(head.group(2))) >= 2 and (LIST_HEADER.match(head.group(1)) or LABELED.match(head.group(1) + " x")):
            out.append(head.group(1))
            out += [("1. " + p.strip()) for p in INLINE_ITEM.split(" " + head.group(2)) if p.strip()]
        else:
            out.append(ln.rstrip())
    return out


def extract_titles(text):
    lines = _expand(text)
    out, cur, in_list = [], None, False

    def push(raw):
        title, domain = clean_title(raw)
        if not valid_title(title):
            return None
        if any(normalize_title(x["title"]) == normalize_title(title) for x in out):
            return None
        item = {"title": title, "domain": domain, "summary": ""}
        out.append(item)
        return item

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        f = FIELD.match(s)
        if f and cur is not None:
            key, val = f.group(1).lower(), f.group(2).strip()
            if key in ("domain", "category", "field", "area"):
                cur["domain"] = val.strip(QUOTES + " ")[:100]
            else:
                cur["summary"] = (cur["summary"] + "\n" + val).strip()[:4000]
            continue
        m = COMMAND.match(s) or LABELED.match(s)
        if m:
            in_list = False
            body = m.group(1).strip()
            if not body:
                in_list = True
                continue
            cur = push(body)
            continue
        if LIST_HEADER.match(s):
            in_list, cur = True, None
            continue
        b = BULLET.match(s)
        if in_list and b:
            cur = push(b.group(1))
            continue
        if cur is not None and not in_list and len(out) == 1:
            cur["summary"] = (cur["summary"] + "\n" + s).strip()[:4000]
            continue
        if in_list and not b:
            in_list = False
    for item in out:
        item["domain"] = item["domain"] or "General IT"
    return out


class CapstoneDesk:
    def __init__(self, cfg, log, path=STORE):
        self.cfg = cfg
        self.log = log
        self.path = Path(path)
        self._lock = threading.Lock()
        self.items = self._read()
        self.last_sync = 0.0
        self.last_error = ""
        self.syncing = False

    def _read(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return [i for i in data.get("items", []) if isinstance(i, dict) and i.get("title")]
            except (ValueError, OSError):
                self.path.rename(self.path.with_suffix(".broken.json"))
        return []

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": self.items}, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    @property
    def settings(self):
        return self.cfg["capstone"]

    def token(self):
        import os

        return self.settings.get("token") or os.environ.get("HEISENBOT_CAPSTONE_TOKEN", "")

    def site(self):
        return (self.settings.get("site_url") or DEFAULT_SITE).rstrip("/")

    def find(self, title):
        n = normalize_title(title)
        return next((i for i in self.items if normalize_title(i["title"]) == n), None)

    def add(self, found, author="", origin="chat"):
        added = []
        for f in found:
            if self.find(f["title"]):
                continue
            item = {"title": f["title"], "domain": f.get("domain") or "General IT", "summary": f.get("summary", ""),
                    "author": "" if (author or "").strip().lower() in ("", "someone", "you") else author.strip()[:80],
                    "origin": origin, "status": "pending", "t": time.time(), "error": ""}
            self.items.append(item)
            added.append(item)
        if added:
            self.items = self.items[-1000:]
            self.save()
        return added

    def collect(self, msg, origin="chat"):
        if not self.settings.get("enabled", True) or msg.outgoing:
            return []
        found = extract_titles(msg.text)
        added = self.add(found, msg.sender, origin)
        for a in added:
            self.log("ok", f"Capstone title from {a['author'] or 'a member'}: {a['title']}", capstone=a["title"])
        return added

    def remove(self, title):
        item = self.find(title)
        if not item:
            raise ValueError("Title not found")
        self.items.remove(item)
        self.save()

    def retry(self, title=None):
        n = 0
        for i in self.items:
            if i["status"] in ("failed", "rejected") and (title is None or normalize_title(i["title"]) == normalize_title(title)):
                i["status"], i["error"] = "pending", ""
                n += 1
        if n:
            self.save()
        return n

    def counts(self):
        c = {"pending": 0, "posted": 0, "skipped": 0, "failed": 0, "rejected": 0}
        for i in self.items:
            c[i["status"]] = c.get(i["status"], 0) + 1
        return c

    def summary(self):
        return {"items": list(reversed(self.items[-300:])), "counts": self.counts(), "site": self.site(),
                "token_set": bool(self.token()), "last_sync": self.last_sync, "last_error": self.last_error,
                "syncing": self.syncing, "enabled": self.settings.get("enabled", True),
                "auto_post": self.settings.get("auto_post", True)}

    async def _request(self, method, path, body=None):
        import aiohttp

        headers = {"Authorization": f"Bearer {self.token()}", "User-Agent": "Heisenbot/1 capstone-sync"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.request(method, self.site() + path, headers=headers,
                                 data=json.dumps(body) if body is not None else None) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = {}
                return r.status, data if isinstance(data, dict) else {}

    async def check(self):
        if not self.token():
            raise ValueError("Paste the intake token from the capstone site first")
        status, data = await self._request("GET", "/api/intake")
        if status != 200:
            raise RuntimeError(data.get("error") or f"Site answered HTTP {status}")
        return {"site": self.site(), "drafts": data.get("drafts", 0)}

    async def sync(self):
        pending = [i for i in self.items if i["status"] == "pending"]
        if not pending:
            return {"added": 0, "skipped": 0, "failed": 0}
        if not self.token():
            self.last_error = "No intake token set"
            return {"added": 0, "skipped": 0, "failed": 0, "error": self.last_error}
        if self.syncing:
            return {"added": 0, "skipped": 0, "failed": 0, "busy": True}
        self.syncing = True
        tally = {"added": 0, "skipped": 0, "failed": 0}
        try:
            for k in range(0, len(pending), 50):
                chunk = pending[k:k + 50]
                body = {"drafts": [{"title": i["title"], "domain": i["domain"][:100], "summary": i["summary"][:4000],
                                    "author": i["author"][:80], "source": "heisenbot"} for i in chunk]}
                try:
                    status, data = await self._request("POST", "/api/intake", body)
                except Exception as e:
                    self.last_error = f"Could not reach {self.site()}: {e.__class__.__name__}"
                    tally["failed"] += len(chunk)
                    break
                if status in (200, 201):
                    added = {normalize_title(t) for t in data.get("added", [])}
                    for i in chunk:
                        i["status"] = "posted" if normalize_title(i["title"]) in added else "skipped"
                        i["error"] = "" if i["status"] == "posted" else next(
                            (s.get("reason", "") for s in data.get("skipped", []) if normalize_title(s.get("title", "")) == normalize_title(i["title"])), "already on the site")
                        tally["added" if i["status"] == "posted" else "skipped"] += 1
                    self.last_error = ""
                elif status == 400 and len(chunk) > 1:
                    for i in chunk:
                        st, dt = await self._request("POST", "/api/intake", {"drafts": [body["drafts"][chunk.index(i)]]})
                        if st in (200, 201):
                            i["status"] = "posted" if dt.get("added") else "skipped"
                            i["error"] = "" if dt.get("added") else "already on the site"
                            tally["added" if dt.get("added") else "skipped"] += 1
                        else:
                            i["status"], i["error"] = "rejected", dt.get("error", f"HTTP {st}")
                            tally["failed"] += 1
                elif status == 400:
                    chunk[0]["status"], chunk[0]["error"] = "rejected", data.get("error", "HTTP 400")
                    tally["failed"] += 1
                else:
                    self.last_error = data.get("error") or f"Site answered HTTP {status}"
                    tally["failed"] += len(chunk)
                    break
            self.last_sync = time.time()
            self.save()
            if tally["added"]:
                self.log("ok", f"Posted {tally['added']} capstone title(s) to {self.site()}")
            if self.last_error:
                self.log("warn", f"Capstone sync: {self.last_error}")
            return {**tally, **({"error": self.last_error} if self.last_error else {})}
        finally:
            self.syncing = False

    async def loop(self):
        while True:
            try:
                if self.settings.get("enabled", True) and self.settings.get("auto_post", True) and self.token() \
                        and any(i["status"] == "pending" for i in self.items):
                    await self.sync()
            except Exception as e:
                self.last_error = str(e)[:200]
            await asyncio.sleep(60 if self.last_error else 15)
