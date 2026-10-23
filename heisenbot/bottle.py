import json
import re
import threading
import time
import unicodedata
from pathlib import Path

from .config import CACHE, DATA

STORE = DATA / "bottle.json"
FILM_URL = "https://github.com/Tiredicey/ITCAPSTONE/releases/download/film-v1/bote_sa_bote_messenger.mp4"
DEFAULT_MILESTONES = ["Chapter 1: Introduction", "Chapter 2: Review of Related Literature", "Chapter 3: Methodology",
                      "System prototype", "Testing and evaluation", "Final defense"]

CMD = re.compile(r"^\s*[!/](bottle|bote|progress|done|tapos|doing|ginagawa|todo|undo|task|add|milestone|sail|teaser|film)\b\s*(.*)$", re.I | re.S)
VERBS = {"bottle": "show", "bote": "show", "progress": "show", "done": "done", "tapos": "done", "doing": "doing",
         "ginagawa": "doing", "todo": "todo", "undo": "todo", "task": "add", "add": "add", "milestone": "milestone",
         "sail": "milestone", "teaser": "teaser", "film": "teaser"}
STOP = {"the", "a", "an", "ng", "sa", "na", "ang", "and", "of", "for", "to"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def tokens(s):
    return [w for w in norm(s).split() if w not in STOP]


def parse(text):
    m = CMD.match(text or "")
    if not m:
        return None
    verb, arg = VERBS[m.group(1).lower()], m.group(2).strip()
    if verb == "show" and arg.lower() in ("help", "?"):
        return {"verb": "help", "arg": ""}
    return {"verb": verb, "arg": arg[:200]}


def best_match(query, items, key):
    q = norm(query)
    if not q:
        return None, []
    if q.isdigit():
        i = int(q) - 1
        return (items[i], []) if 0 <= i < len(items) else (None, [])
    exact = [x for x in items if norm(key(x)) == q]
    if len(exact) == 1:
        return exact[0], []
    qt = set(tokens(query))
    scored = []
    for x in items:
        s = (2.0 if q in norm(key(x)) else 0.0) + len(qt & set(tokens(key(x)))) / max(1, len(qt))
        if s > 0:
            scored.append((s, x))
    scored.sort(key=lambda p: -p[0])
    if not scored or scored[0][0] < 0.5 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None, [x for _, x in scored[:3]]
    return scored[0][1], []


def split_task(arg):
    m = re.match(r"^(.{1,60}?)\s*[:>|]\s*(.+)$", arg)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", arg.strip())


class BottleDesk:
    def __init__(self, cfg, log, path=None):
        self.cfg = cfg
        self.log = log
        self.path = Path(path) if path else STORE
        self._lock = threading.Lock()
        self.state = self._read()
        self.last_error = ""

    def _read(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.path.rename(self.path.with_suffix(".broken.json"))
        return {}

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    @property
    def settings(self):
        return self.cfg["bottle"]

    def site(self):
        return (self.settings.get("site_url") or "").rstrip("/")

    def view_url(self):
        return f"{self.site()}/p/{self.state['slug']}" if self.state.get("slug") and self.site() else ""

    def summary(self):
        return {"enabled": self.settings.get("enabled", True), "site": self.site(), "linked": bool(self.state.get("slug")),
                "view_url": self.view_url(), "title": self.state.get("title", ""), "last_error": self.last_error,
                "announce": self.settings.get("announce", True), "announced": bool(self.state.get("announced"))}

    async def _req(self, method, path, body=None, auth=True):
        import aiohttp

        if not self.site():
            raise RuntimeError("The bottle site address is not set yet")
        headers = {"User-Agent": "Heisenbot/1 bottle"}
        if auth and self.state.get("key"):
            headers["x-edit-key"] = self.state["key"]
        if body is not None:
            headers["Content-Type"] = "application/json"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.request(method, self.site() + path, headers=headers, data=json.dumps(body) if body is not None else None) as r:
                data = await r.json(content_type=None) if "json" in (r.headers.get("content-type") or "") else await r.text()
                if r.status >= 400:
                    err = data.get("error") if isinstance(data, dict) else str(data)[:120]
                    raise RuntimeError(f"bottle site answered {r.status}: {err}")
                return data

    def _guess_title(self, capstone_items):
        known = [i for i in capstone_items if i.get("status") in ("posted", "skipped", "pending")]
        return known[-1]["title"] if known else "Our Capstone"

    async def ensure(self, capstone_items=()):
        if self.state.get("slug"):
            return self.state
        title = self.settings.get("title") or self._guess_title(capstone_items)
        data = await self._req("POST", "/api/projects", {"title": title, "team": self.settings.get("team", ""),
                                                          "members": [], "milestones": DEFAULT_MILESTONES}, auth=False)
        self.state = {"slug": data["slug"], "key": data["editKey"], "title": title, "created": time.time()}
        self.save()
        self.log("ok", f"Bottle launched: {self.view_url()}")
        return self.state

    async def fetch(self):
        return await self._req("GET", f"/api/projects/{self.state['slug']}", auth=False)

    async def text(self):
        return await self._req("GET", f"/api/projects/{self.state['slug']}/summary", auth=False)

    async def member_id(self, data, sender):
        if not sender or sender.strip().lower() in ("someone", "you"):
            return None
        hit, _ = best_match(sender, data["members"], lambda m: m["name"])
        if hit and norm(hit["name"]) == norm(sender):
            return hit["id"]
        if len(data["members"]) >= 12:
            return None
        r = await self._req("POST", f"/api/projects/{self.state['slug']}/members", {"name": sender[:40]})
        return r["id"]

    async def run(self, cmd, sender, capstone_items=()):
        verb, arg = cmd["verb"], cmd["arg"]
        if verb == "help":
            return {"text": self.help_text()}
        if verb == "teaser":
            return {"file": await self.film()}
        await self.ensure(capstone_items)
        slug = self.state["slug"]
        if verb == "show":
            return {"text": await self.text()}
        data = await self.fetch()
        if verb == "milestone":
            if not arg:
                return {"text": "Write the milestone name, e.g. !milestone User acceptance test"}
            await self._req("POST", f"/api/projects/{slug}/milestones", {"title": arg[:60]})
            return {"text": f"New sail: {arg[:60]}. {self.view_url()}"}
        if verb == "add":
            ms_q, title = split_task(arg)
            if not title:
                return {"text": "Write it like: !task Chapter 1: Draft the background of the study"}
            ms = data["milestones"]
            if not ms:
                return {"text": "Add a milestone first: !milestone Chapter 1"}
            target, _ = best_match(ms_q, ms, lambda m: m["title"]) if ms_q else (ms[0], [])
            if not target:
                return {"text": "Which milestone? " + " | ".join(f"{i + 1}. {m['title']}" for i, m in enumerate(ms))}
            mid = await self.member_id(data, sender)
            await self._req("POST", f"/api/projects/{slug}/tasks", {"milestone_id": target["id"], "title": title[:100], "member_id": mid})
            return {"text": f"Added to {target['title']}: {title[:100]}"}
        if not arg:
            return {"text": f"Which task? e.g. !{verb} Chapter 1 draft"}
        tasks = data["tasks"]
        task, options = best_match(arg, tasks, lambda t: t["title"])
        if not task:
            if options:
                return {"text": "Which one? " + " | ".join(t["title"] for t in options)}
            ms, _ = best_match(arg, data["milestones"], lambda m: m["title"])
            if ms and verb == "done" and not any(t["milestone_id"] == ms["id"] and t["status"] != "done" for t in tasks):
                created = await self._req("POST", f"/api/projects/{slug}/tasks", {"milestone_id": ms["id"], "title": ms["title"], "member_id": await self.member_id(data, sender)})
                await self._req("PATCH", f"/api/projects/{slug}/tasks/{created['id']}", {"status": "done"})
                return await self._after(f"Done: {ms['title']}")
            return {"text": f"No task matches \"{arg[:60]}\". Add it first: !task {arg[:60]}"}
        patch = {"status": verb}
        if task["member_id"] is None:
            mid = await self.member_id(data, sender)
            if mid:
                patch["member_id"] = mid
        await self._req("PATCH", f"/api/projects/{slug}/tasks/{task['id']}", patch)
        label = {"done": "Done", "doing": "On it", "todo": "Reopened"}[verb]
        return await self._after(f"{label}: {task['title']}")

    async def _after(self, line):
        p = (await self.fetch())["progress"]
        out = f"{line}. Bottle at {p['percent']}% ({p['tasksDone']}/{p['tasksTotal']})."
        full = {m["title"] for m in p["milestones"] if m["total"] and m["done"] == m["total"]}
        seen = set(self.state.get("full", []))
        new = sorted(full - seen)
        if new:
            out += f" Sail full: {new[0]}!"
        if p["tasksTotal"] and p["percent"] == 100 and not self.state.get("finished"):
            out += " Every sail is full. Congrats, crew!"
            self.state["finished"] = time.time()
        if full != seen or new:
            self.state["full"] = sorted(full)
        self.save()
        return {"text": out + " " + self.view_url()}

    async def film(self):
        dest = CACHE / "bote_sa_bote_messenger.mp4"
        if dest.exists() and dest.stat().st_size > 1_000_000:
            return dest
        import aiohttp

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            async with s.get(self.settings.get("film_url") or FILM_URL) as r:
                if r.status != 200:
                    raise RuntimeError(f"Could not download the teaser (HTTP {r.status})")
                with tmp.open("wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
        tmp.replace(dest)
        return dest

    def help_text(self):
        return ("Bottle commands: !bottle (progress) | !done <task> | !doing <task> | !todo <task> | "
                "!task <milestone>: <task> | !milestone <name> | !teaser (the film). Tapos/ginagawa work too.")
