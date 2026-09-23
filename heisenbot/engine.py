import random
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import CLIPS

VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}


@dataclass
class Message:
    id: str
    sender: str
    text: str
    outgoing: bool = False


@dataclass
class Decision:
    reaction: str
    reason: str
    message: Message
    line: str = ""
    command: str = ""
    extra: dict = field(default_factory=dict)


def normalize(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"([^\W\d_])\1+", r"\1", s)
    s = re.sub(r"([^\W\d_])([^\W\d_])(?:\1\2)+\1?", r"\1\2\1\2", s)
    return s


def _compile(keyword):
    k = normalize(keyword)
    if re.fullmatch(r"[\w' ]+", k):
        body = re.escape(k).replace(r"\ ", r"\s+")
        return re.compile(r"(?<![\w])(?:" + body + r")+(?![\w])")
    return re.compile(re.escape(k))


class ClipBank:
    def __init__(self, root=CLIPS):
        self.root = Path(root)
        self.recent = {}

    def index(self):
        bank = {}
        if not self.root.exists():
            return bank
        for p in sorted(self.root.rglob("*")):
            if p.suffix.lower() not in VIDEO_EXT or not p.is_file():
                continue
            rel = p.relative_to(self.root)
            name = rel.parts[0] if len(rel.parts) > 1 else re.sub(r"[_\-\s]*\d+$", "", p.stem)
            bank.setdefault(name.lower(), []).append(p)
        return bank

    def pick(self, reaction):
        files = self.index().get(reaction.lower(), [])
        if not files:
            return None
        last = self.recent.get(reaction)
        pool = [f for f in files if f != last] or files
        choice = random.choice(pool)
        self.recent[reaction] = choice
        return choice


class Engine:
    def __init__(self, cfg, bank=None, rng=None, clock=time.time):
        self.cfg = cfg
        self.bank = bank or ClipBank()
        self.rng = rng or random.Random()
        self.clock = clock
        self.enabled = True
        self.last_fire = 0.0
        self.user_last = {}
        self.history = deque()
        self._cache_key = None
        self._patterns = []
        self.care_until = 0.0
        self._care_key = None
        self._care = []

    def patterns(self):
        reactions = self.cfg["reactions"]
        key = repr(sorted((k, tuple(v["keywords"])) for k, v in reactions.items()))
        if key != self._cache_key:
            pats = []
            for name, r in reactions.items():
                for kw in r["keywords"]:
                    if kw.strip():
                        pats.append((name, kw, _compile(kw)))
            pats.sort(key=lambda t: -len(t[1]))
            self._patterns, self._cache_key = pats, key
        return self._patterns

    def match(self, text):
        t = normalize(text)
        best = None
        for name, kw, pat in self.patterns():
            m = pat.search(t)
            if m and (best is None or len(kw) > len(best[1]) or (len(kw) == len(best[1]) and m.start() < best[2])):
                best = (name, kw, m.start())
        return best

    def mention_re(self):
        n = re.escape(normalize(self.cfg["bot_name"])).replace(r"\ ", r"\s+")
        return re.compile(r"(?:@" + n + r"\b|@bot\b|^" + n + r"\b)")

    def snooze_state(self):
        sz = self.cfg["snooze"]
        if sz["mode"] != "off" and self.clock() < sz["until"]:
            return sz["mode"], sz["until"]
        return "off", 0

    def care_hit(self, text):
        phrases = self.cfg["care_phrases"]
        key = tuple(phrases)
        if key != self._care_key:
            self._care = [_compile(p) for p in phrases if p.strip()]
            self._care_key = key
        t = normalize(text)
        return any(p.search(t) for p in self._care)

    def _quiet(self):
        q = self.cfg["quiet_hours"]
        if not q.get("enabled"):
            return False
        h = datetime.fromtimestamp(self.clock()).hour
        s, e = int(q["start"]), int(q["end"])
        return s <= h < e if s < e else (h >= s or h < e)

    def _rate_ok(self, sender, bypass_user=False):
        now = self.clock()
        while self.history and now - self.history[0] > 3600:
            self.history.popleft()
        if len(self.history) >= self.cfg["max_per_hour"]:
            return "hourly cap"
        if now - self.last_fire < self.cfg["cooldown_seconds"]:
            return "global cooldown"
        if not bypass_user and now - self.user_last.get(sender, 0) < self.cfg["per_user_cooldown_seconds"]:
            return "user cooldown"
        return None

    def commit(self, sender):
        now = self.clock()
        self.last_fire = now
        self.user_last[sender] = now
        self.history.append(now)

    def is_admin(self, sender):
        admins = [a.lower() for a in self.cfg["admins"]]
        return not admins or sender.lower() in admins

    def _command(self, msg):
        prefix = "!" + self.cfg["bot_name"].strip().lower()
        t = re.sub(r"\s+", " ", msg.text.strip().lower())
        if not self.cfg["commands_enabled"] or not (t == prefix or t.startswith(prefix + " ")):
            return None
        arg = t[len(prefix):].strip()
        if arg in ("", "help"):
            return Decision("", "command", msg, command="help")
        if arg in ("off", "sleep", "stop"):
            if self.is_admin(msg.sender):
                self.enabled = False
                return Decision("", "command", msg, command="off")
            return None
        if arg in ("on", "wake", "start"):
            if self.is_admin(msg.sender):
                self.enabled = True
                return Decision("", "command", msg, command="on")
            return None
        if arg == "list":
            return Decision("", "command", msg, command="list")
        name, _, say = arg.partition(" ")
        if name in self.cfg["reactions"]:
            return Decision(name, "command", msg, command="play", extra={"say": say})
        return None

    def decide(self, msg):
        if msg.outgoing or not msg.text.strip():
            return None, "own or empty"
        cmd = self._command(msg)
        if cmd and cmd.command in ("help", "off", "on", "list"):
            return cmd, "command"
        if not self.enabled:
            return None, "sleeping"
        if self.cfg["care_guard"] and self.care_hit(msg.text):
            self.care_until = self.clock() + self.cfg["care_minutes"] * 60
            return None, f"care guard: serious message, quiet for {self.cfg['care_minutes']} min"
        if self.clock() < self.care_until:
            return None, "care guard active"
        mode, _ = self.snooze_state()
        if mode == "pause":
            return None, "paused"
        if self._quiet():
            return None, "quiet hours"
        mentioned = bool(self.mention_re().search(normalize(msg.text)))
        if mode == "mentions" and not (mentioned or cmd):
            return None, "mentions only"
        decision = cmd
        if decision is None:
            hit = self.match(msg.text)
            if hit:
                decision = Decision(hit[0], ("mention+" if mentioned else "") + f"keyword '{hit[1]}'", msg)
            elif mentioned:
                decision = Decision(self.cfg["mention_reaction"], "mention", msg)
            elif self.rng.random() < self.cfg["lurk_chance"]:
                decision = Decision(self.cfg["lurk_reaction"], "lurk roll", msg)
        if decision is None:
            return None, "no match"
        why = self._rate_ok(msg.sender, bypass_user=decision.reason.startswith("mention"))
        if why:
            return None, why
        r = self.cfg["reactions"].get(decision.reaction, {"lines": [""]})
        say = decision.extra.get("say")
        line = say if say else self.rng.choice(r.get("lines") or [""])
        decision.line = fill_line(line, msg.sender, msg.text)
        return decision, decision.reason

    def help_text(self):
        n = self.cfg["bot_name"].lower()
        names = ", ".join(sorted(self.cfg["reactions"]))
        return f"I am {self.cfg['bot_name']}. Tag @{self.cfg['bot_name']} or say a trigger word. Commands: !{n} list | !{n} <reaction> [text] | !{n} off | !{n} on. Reactions: {names}"


UNKNOWN = {"", "someone", "you"}


def known(sender):
    return (sender or "").strip().lower() not in UNKNOWN


def fill_line(line, sender, text):
    if known(sender):
        return line.replace("{name}", first_name(sender)).replace("{text}", text)
    out = re.sub(r"^\s*\{name\}\s*[,.!?]*\s*", "", line)
    out = re.sub(r"[,\s]*\{name\}", "", out).replace("{text}", text).strip()
    return out[:1].upper() + out[1:] if out else out


def first_name(sender):
    return (sender or "friend").strip().split()[0] if sender and sender.strip() else "friend"
