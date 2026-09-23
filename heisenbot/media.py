import asyncio
import hashlib
import math
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import CACHE

FONT_CANDIDATES = [
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

GREEN = (38, 110, 60)
GREEN_LIGHT = (122, 196, 120)
INK = (14, 17, 15)


def known_sender(s):
    return (s or "").strip().lower() not in ("", "someone", "you")


def ffmpeg_bin():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise RuntimeError("FFmpeg not found. Run the setup script again or install FFmpeg.") from e


def font(size):
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def run(args, timeout=120):
    p = subprocess.run([ffmpeg_bin(), "-hide_banner", "-y", *args], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "ffmpeg failed")
    return p


def probe(path):
    p = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    err = p.stderr
    d = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    dur = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0
    v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    rot = re.search(r"rotation of (-?[\d.]+)|rotate\s*:\s*(-?\d+)", err)
    w, h = (int(v[1]), int(v[2])) if v else (0, 0)
    if rot and abs(float(rot[1] or rot[2] or 0)) % 180 == 90:
        w, h = h, w
    return {"duration": dur, "width": w, "height": h, "audio": bool(re.search(r"Stream #.*Audio:", err)),
            "video": bool(v)}


def target_size(w, h, max_side=720):
    if not w or not h:
        return 720, 720
    s = min(1.0, max_side / max(w, h))
    return max(2, int(w * s) // 2 * 2), max(2, int(h * s) // 2 * 2)


def _wrap(draw, text, fnt, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=fnt) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            while draw.textlength(w, font=fnt) > width and len(w) > 1:
                cut = max(1, int(len(w) * width / max(1, draw.textlength(w, font=fnt))))
                lines.append(w[:cut])
                w = w[cut:]
            cur = w
    if cur:
        lines.append(cur)
    return lines


def element_symbol(name):
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "Wh"
    a = parts[0][0].upper()
    b = (parts[1][0] if len(parts) > 1 else parts[0][1:2] or "x").lower()
    return a + b


def green_ratio(clip, at=0.5):
    try:
        p = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(clip),
                            "-frames:v", "1", "-vf", "scale=64:64", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, timeout=30)
        data = p.stdout
    except Exception:
        return 0.0
    if len(data) < 64 * 64 * 3:
        return 0.0
    border = [(x, y) for x in range(64) for y in range(64) if x < 6 or x > 57 or y < 6 or y > 57]
    g = 0
    for x, y in border:
        i = (y * 64 + x) * 3
        r, gg, b = data[i], data[i + 1], data[i + 2]
        if gg > 90 and gg > r * 1.35 and gg > b * 1.35:
            g += 1
    return g / len(border)


def backdrop(w, h, out):
    if Path(out).exists():
        return out
    img = Image.new("RGB", (w, h), (14, 17, 15))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=(int(18 + 12 * t), int(34 + 22 * (1 - t)), int(26 + 8 * t)))
    step = max(24, w // 12)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(38, 64, 46), width=1)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(38, 64, 46), width=1)
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse([w * 0.15, h * 0.1, w * 0.85, h * 0.95], fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(w // 6))
    img = Image.composite(Image.new("RGB", (w, h), (70, 120, 80)), img, glow)
    img.save(out)
    return out


def caption_overlay(w, h, sender, message, line, out):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(10, w // 40)
    if message and not sender:
        fs_msg = max(15, w // 28)
        f_msg = font(fs_msg)
        lines = _wrap(d, message if len(message) <= 140 else message[:137] + "...", f_msg, w - pad * 2)[:3]
        panel_h = len(lines) * int(fs_msg * 1.25) + pad * 2
        img.alpha_composite(Image.new("RGBA", (w, panel_h), (10, 14, 11, 190)), (0, 0))
        y = pad
        for ln in lines:
            d.text((pad, y), ln, font=f_msg, fill=(240, 240, 235))
            y += int(fs_msg * 1.25)
    elif sender or message:
        fs_name, fs_msg = max(16, w // 26), max(15, w // 30)
        f_name, f_msg = font(fs_name), font(fs_msg)
        box = int(fs_name * 2.3)
        msg = message if len(message) <= 140 else message[:137] + "..."
        text_w = w - pad * 3 - box
        lines = _wrap(d, msg, f_msg, text_w)[:3]
        panel_h = max(box, fs_name + 6 + len(lines) * int(fs_msg * 1.25)) + pad * 2
        panel = Image.new("RGBA", (w, panel_h), (10, 14, 11, 190))
        img.alpha_composite(panel, (0, 0))
        d.rectangle([pad, pad, pad + box, pad + box], fill=GREEN + (255,), outline=GREEN_LIGHT + (255,), width=2)
        sym = element_symbol(sender or "?")
        f_sym = font(int(box * 0.5))
        tw = d.textlength(sym, font=f_sym)
        d.text((pad + (box - tw) / 2, pad + box * 0.22), sym, font=f_sym, fill=(255, 255, 255))
        x = pad * 2 + box
        d.text((x, pad), (sender or "Someone")[:40], font=f_name, fill=GREEN_LIGHT)
        y = pad + fs_name + 6
        for ln in lines:
            d.text((x, y), ln, font=f_msg, fill=(240, 240, 235))
            y += int(fs_msg * 1.25)
    if line:
        fs = max(20, w // 16)
        f = font(fs)
        rows = _wrap(d, line.upper(), f, w - pad * 2)[:3]
        y = h - pad * 2 - len(rows) * int(fs * 1.15)
        for r in rows:
            tw = d.textlength(r, font=f)
            d.text(((w - tw) / 2, y), r, font=f, fill=(255, 255, 255), stroke_width=max(2, fs // 12),
                   stroke_fill=(0, 0, 0))
            y += int(fs * 1.15)
    img.save(out)
    return out


async def _tts(text, voice, rate, pitch, out):
    import edge_tts

    await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out))


def tts(text, voice, rate="+0%", pitch="+0Hz", out=None):
    text = re.sub(r"\s+", " ", text or "").strip()[:280]
    if not text:
        return None
    key = hashlib.sha1(f"{text}|{voice}|{rate}|{pitch}".encode()).hexdigest()[:16]
    out = Path(out or CACHE / f"tts_{key}.mp3")
    if out.exists() and out.stat().st_size > 0:
        return out
    coro = _tts(text, voice, rate, pitch, out)
    try:
        asyncio.get_running_loop()
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            ex.submit(asyncio.run, coro).result(timeout=60)
    except RuntimeError:
        asyncio.run(coro)
    return out if out.exists() and out.stat().st_size > 0 else None


def speech_text(cfg, decision):
    src = cfg["tts_source"]
    line, msg = decision.line.strip(), decision.message.text.strip()
    if src == "message":
        return f"{decision.message.sender} says: {msg}"
    if src == "both":
        return f"{decision.message.sender} says: {msg}. {line}".strip(". ")
    return line


def render(clip, out_dir, sender="", message="", line="", speech="", cfg=None):
    cfg = cfg or {}
    clip = Path(clip)
    info = probe(clip)
    if not info["video"]:
        raise RuntimeError(f"{clip.name} has no video stream")
    w, h = target_size(info["width"], info["height"])
    use_caption = cfg.get("caption", True)
    voice = None
    if cfg.get("tts", True) and speech.strip():
        try:
            voice = tts(speech, cfg.get("tts_voice", "en-US-ChristopherNeural"), cfg.get("tts_rate", "+0%"),
                        cfg.get("tts_pitch", "+0Hz"))
        except Exception as e:
            print(f"[tts] skipped: {e}", file=sys.stderr)
    vdur = probe(voice)["duration"] if voice else 0.0
    dur = min(float(cfg.get("max_seconds", 12)), max(info["duration"] or 3.0, vdur + 0.4))
    key = hashlib.sha1(f"{clip}|{clip.stat().st_mtime}|{sender}|{message}|{line}|{voice}|{use_caption}|{dur}|{cfg.get('clip_volume')}|{cfg.get('green_screen')}|v2".encode()).hexdigest()[:16]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{clip.stem}_{key}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    args = []
    if dur > info["duration"] + 0.05:
        args += ["-stream_loop", "-1"]
    args += ["-i", str(clip)]
    idx = 1
    mode = cfg.get("green_screen", "auto")
    keyed = mode == "on" or (mode == "auto" and green_ratio(clip, min(0.5, (info["duration"] or 1) / 2)) > 0.55)
    if keyed:
        bg = backdrop(w, h, CACHE / f"backdrop_{w}x{h}.png")
        args += ["-loop", "1", "-i", str(bg)]
        bi = idx
        idx += 1
        vf = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,chromakey=0x00d000:0.18:0.08,despill=type=green,"
              f"format=yuva420p[fg];[{bi}:v]scale={w}:{h},setsar=1,format=yuv420p[bgv];"
              f"[bgv][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,fps=30")
    else:
        vf = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0e110f,setsar=1,fps=30"
    if not known_sender(sender):
        sender = ""
    if use_caption and (sender or message or line):
        png = caption_overlay(w, h, sender, message, line, out_dir / f"cap_{key}.png")
        args += ["-i", str(png)]
        vf += f"[base];[base][{idx}:v]overlay=0:0,format=yuv420p[v]"
        idx += 1
    else:
        vf += ",format=yuv420p[v]"
    vol = float(cfg.get("clip_volume", 0.35))
    if voice:
        args += ["-i", str(voice)]
        vi = idx
        idx += 1
        if info["audio"]:
            af = f"[0:a]volume={vol}[ca];[{vi}:a]adelay=150|150,volume=1.6[va];[ca][va]amix=inputs=2:duration=longest:normalize=0[a]"
        else:
            af = f"[{vi}:a]adelay=150|150,volume=1.6,apad[a]"
    elif info["audio"]:
        af = "[0:a]anull[a]"
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        af = f"[{idx}:a]anull[a]"
        idx += 1
    args += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", "-profile:v", "main",
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart", str(out)]
    run(args)
    return out


STARTER = {
    "laugh": "bounce",
    "facepalm": "sink",
    "dance": "sway",
    "rage": "shake",
    "stare": "zoom",
    "confusion": "tilt",
    "dead": "fade",
    "saymyname": "push",
}


def _frame(base, t, style, size):
    s = size
    zoom, dx, dy, ang, dark, tint = 1.0, 0.0, 0.0, 0.0, 0.0, None
    if style == "bounce":
        zoom, dy = 1.08, -abs(math.sin(t * math.pi * 6)) * s * 0.05
    elif style == "sink":
        zoom, dy, dark = 1.1, t * s * 0.06, t * 0.35
    elif style == "sway":
        zoom, ang, dx = 1.15, math.sin(t * math.pi * 4) * 8, math.sin(t * math.pi * 4) * s * 0.04
    elif style == "shake":
        zoom, dx, dy, tint = 1.18 + t * 0.12, math.sin(t * 90) * s * 0.02, math.cos(t * 77) * s * 0.02, (170, 20, 20)
    elif style == "zoom":
        zoom, dark = 1.0 + t * 0.45, t * 0.2
    elif style == "tilt":
        zoom, ang = 1.12, math.sin(min(1, t * 2) * math.pi / 2) * 14
    elif style == "fade":
        zoom, dark = 1.05 + t * 0.1, 0.0
    elif style == "push":
        zoom, tint = 1.0 + t * t * 0.6, (30, 90, 45)
    fw = int(s * zoom)
    img = base.resize((fw, fw), Image.LANCZOS)
    if ang:
        img = img.rotate(ang, resample=Image.BICUBIC)
    x, y = (fw - s) // 2 - int(dx), (fw - s) // 2 - int(dy)
    x, y = max(0, min(fw - s, x)), max(0, min(fw - s, y))
    img = img.crop((x, y, x + s, y + s)).convert("RGB")
    if style == "fade":
        g = ImageOps.grayscale(img).convert("RGB")
        img = Image.blend(img, g, min(1, t * 1.5))
    if tint:
        img = Image.blend(img, Image.new("RGB", img.size, tint), 0.12 + 0.15 * t)
    if dark:
        img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), dark)
    return img


def make_starter_clips(image_path, clips_dir, seconds=3.0, size=640, fps=24, overwrite=False):
    base = ImageOps.fit(Image.open(image_path).convert("RGB"), (size, size), Image.LANCZOS)
    base = base.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60))
    made = []
    for reaction, style in STARTER.items():
        folder = Path(clips_dir) / reaction
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"starter_{style}.mp4"
        if out.exists() and not overwrite:
            made.append(out)
            continue
        n = int(seconds * fps)
        p = subprocess.Popen([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "-s", f"{size}x{size}", "-r", str(fps), "-i", "-",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                              "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
        for i in range(n):
            p.stdin.write(_frame(base, i / max(1, n - 1), style, size).tobytes())
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError(f"Could not build starter clip {out.name}")
        made.append(out)
    return made
