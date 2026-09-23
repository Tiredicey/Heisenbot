import copy
import os
import re
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HEISENBOT_DATA", ROOT / "data"))
CLIPS = ROOT / "clips"
CACHE = DATA / "cache"
LOGS = DATA / "logs"
PROFILE = DATA / "browser_profile"
CONFIG_PATH = DATA / "config.json"

DEFAULTS = {
    "thread_url": "",
    "bot_name": "Walter",
    "headless": False,
    "live": False,
    "ntfy_topic": "",
    "dry_run": True,
    "lurk_chance": 0.03,
    "lurk_reaction": "stare",
    "mention_reaction": "saymyname",
    "cooldown_seconds": 20,
    "per_user_cooldown_seconds": 45,
    "max_per_hour": 25,
    "quiet_hours": {"enabled": False, "start": 1, "end": 7},
    "snooze": {"until": 0, "mode": "off", "label": ""},
    "care_guard": True,
    "care_minutes": 45,
    "care_phrases": [
        "passed away", "pumanaw", "namatay si", "namatay na", "condolence", "nakikiramay", "burol", "funeral", "lamay",
        "hospital", "ospital", "sinugod", "emergency", "accident", "aksidente talaga",
        "kill myself", "want to die", "gusto ko na mamatay", "ayoko na mabuhay", "suicide", "self harm", "saktan sarili",
        "depressed", "depression", "panic attack", "anxiety attack", "di ako makahinga", "umiiyak", "iyak ako", "crying",
        "hiwalay na kami", "break na kami", "we broke up", "iniwan ako", "please help", "tulungan nyo ako", "seryoso ako",
    ],
    "green_screen": "auto",
    "phone_control": True,
    "caption": True,
    "tts": True,
    "tts_source": "line",
    "tts_voice": "en-US-ChristopherNeural",
    "tts_rate": "-8%",
    "tts_pitch": "-10Hz",
    "clip_volume": 0.35,
    "max_seconds": 12,
    "poll_ms": 1500,
    "commands_enabled": True,
    "admins": [],
    "reactions": {
        "laugh": {
            "keywords": ["lmao", "lol", "haha", "hahaha", "hehe", "rofl", "😂", "🤣", "kek", "xd", "ahaha", "wkwk"],
            "lines": ["{name}. That was actually funny.", "I am the one who laughs."],
        },
        "facepalm": {
            "keywords": ["fail", "bobo", "tanga", "stupid", "bruh", "smh", "🤦", "wala magawa", "ano ba yan"],
            "lines": ["{name}, you clearly don't know who you're talking to.", "This is not chemistry. This is art. And you ruined it."],
        },
        "dance": {
            "keywords": ["lets go", "let's go", "yay", "panalo", "win", "sana all", "party", "🎉", "🥳", "tara"],
            "lines": ["We're done when I say we're done.", "I did it for me. I liked it."],
        },
        "rage": {
            "keywords": ["wtf", "gago", "putek", "angry", "tangina", "😡", "🤬", "bwisit"],
            "lines": ["{name}. Stay out of my territory.", "Tread lightly."],
        },
        "stare": {
            "keywords": ["sus", "hmm", "🤨", "👀", "weird", "creepy"],
            "lines": ["", ""],
        },
        "confusion": {
            "keywords": ["huh", "what", "ano daw", "ha?", "???", "😕", "🤔", "di ko gets", "idk"],
            "lines": ["{name}, what are you talking about?", "I don't follow."],
        },
        "dead": {
            "keywords": ["dead", "ded", "patay", "💀", "rip", "im dead", "i'm dead", "deadass"],
            "lines": ["Say my name, {name}.", "Now say it."],
        },
        "saymyname": {
            "keywords": ["heisenberg", "walter white", "mr white", "breaking bad", "say my name"],
            "lines": ["You're goddamn right.", "Say my name."],
        },
    },
    "selectors": {
        "container": "[role='main']",
        "row": "[role='row']",
        "text": "[dir='auto']",
        "composer": "[role='textbox'][contenteditable='true']",
        "file_input": "input[type='file']",
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k == "reactions" and isinstance(v, dict):
            out[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        for d in (DATA, CLIPS, CACHE, LOGS):
            d.mkdir(parents=True, exist_ok=True)
        self.data = _merge(DEFAULTS, self._read())
        if not self.data["ntfy_topic"] and os.environ.get("HEISENBOT_NTFY"):
            self.data["ntfy_topic"] = os.environ["HEISENBOT_NTFY"]
        self.save()

    def _read(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.path.rename(self.path.with_suffix(".broken.json"))
        return {}

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def update(self, patch):
        self.data = _merge(self.data, validate(patch))
        self.save()
        return self.data

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)


def validate(patch):
    clean = {}
    num = {
        "lurk_chance": (0.0, 1.0),
        "cooldown_seconds": (0, 3600),
        "per_user_cooldown_seconds": (0, 3600),
        "max_per_hour": (1, 500),
        "clip_volume": (0.0, 2.0),
        "max_seconds": (3, 30),
        "poll_ms": (500, 10000),
    }
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue
        if k in num:
            lo, hi = num[k]
            v = min(hi, max(lo, float(v)))
            if k not in ("lurk_chance", "clip_volume"):
                v = int(v)
        elif isinstance(DEFAULTS[k], bool):
            v = bool(v)
        elif k == "reactions":
            v = {
                str(name).strip().lower(): {
                    "keywords": [str(x).strip() for x in r.get("keywords", []) if str(x).strip()],
                    "lines": [str(x) for x in r.get("lines", [])] or [""],
                }
                for name, r in v.items()
                if str(name).strip()
            }
        elif k == "care_phrases":
            v = [str(x).strip() for x in v if str(x).strip()]
        elif k == "care_minutes":
            v = int(min(1440, max(1, float(v))))
        elif k == "green_screen" and v not in ("auto", "on", "off"):
            continue
        elif k == "snooze":
            mode = v.get("mode", "off")
            if mode not in ("off", "pause", "mentions"):
                continue
            v = {"until": float(v.get("until", 0)), "mode": mode, "label": str(v.get("label", ""))[:20]}
        elif k == "admins":
            v = [str(x).strip() for x in v if str(x).strip()]
        elif k == "tts_source" and v not in ("line", "message", "both"):
            continue
        elif k == "ntfy_topic":
            v = re.sub(r"[^A-Za-z0-9_\-]", "", str(v))[:64]
        elif k == "thread_url":
            v = str(v).strip()
            if v and not re.match(r"^https://(www\.)?(facebook|messenger)\.com/", v):
                raise ValueError("Paste a facebook.com/messages link")
        clean[k] = v
    return clean
