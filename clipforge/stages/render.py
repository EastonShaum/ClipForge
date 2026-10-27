"""Render stage: cut, reframe to 9:16, burn word-by-word captions + hook, normalize loudness.

Caption placement follows platform safe zones on a 1080x1920 canvas: TikTok/Shorts/Reels UI
covers roughly the top 10%, the bottom 25% and a ~120 px strip on the right. Captions sit just
below center; the hook sits in the upper third.
"""

import re
import subprocess
import time
from pathlib import Path

from clipforge.config import Settings
from clipforge.tools import ffmpeg

W, H = 1080, 1920
CAPTION_MARGIN_V = 640  # bottom-anchored: baseline ~1280 px, above TikTok's caption block
HOOK_MARGIN_V = 300  # top-anchored
HOOK_SECONDS = 3.0
# Brand logo: a "TV bug" in the top-right corner of the 16:9 picture (blurfill puts its top edge
# at ~y=536). Below the hook box and the disclosure, left of TikTok's right-hand button column,
# which starts lower down.
LOGO_W = 210
LOGO_X_MARGIN = 48
LOGO_Y = 556


def _ts(ms: int) -> str:
    cs = max(0, ms) // 10
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words: list[dict], n: int, gap_ms: int = 450) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (len(cur) >= n or w["s"] - cur[-1]["e"] > gap_ms):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w["w"].endswith((".", "?", "!", ",")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[dict], clip_start_ms: int, clip_end_ms: int, hook: str,
              font: str, words_per_chunk: int = 3, disclosure: str = "") -> str:
    """ASS subtitle file: hook banner + karaoke-style captions with the active word highlighted,
    plus an always-visible disclosure label (FTC wants it in the video, not only the caption)."""
    rel = [{"w": w["w"], "s": w["s"] - clip_start_ms, "e": w["e"] - clip_start_ms}
           for w in words if w["s"] >= clip_start_ms and w["e"] <= clip_end_ms + 200]
    dur = clip_end_ms - clip_start_ms
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Captions: white, thick black outline, bottom-center anchored.
        f"Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,"
        f"1,7,3,2,90,150,{CAPTION_MARGIN_V},1",
        # Hook: black text on a white box, top-center anchored.
        f"Style: Hook,{font},70,&H00111111,&H00111111,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,"
        f"3,18,0,8,110,150,{HOOK_MARGIN_V},1",
        # Disclosure: small, semi-opaque box, top-left inside the safe zone.
        f"Style: Disc,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,"
        "3,8,0,7,60,0,200,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if disclosure:
        out.append(f"Dialogue: 2,{_ts(0)},{_ts(dur)},Disc,,0,0,0,,{_esc(disclosure)}")
    if hook:
        out.append(f"Dialogue: 1,{_ts(0)},{_ts(min(int(HOOK_SECONDS * 1000), dur))},Hook,,0,0,0,,"
                   f"{_esc(hook.upper())}")
    for chunk in chunk_words(rel, words_per_chunk):
        for i, w in enumerate(chunk):
            start = w["s"]
            end = chunk[i + 1]["s"] if i + 1 < len(chunk) else w["e"] + 120
            parts = []
            for j, other in enumerate(chunk):
                t = _esc(other["w"].upper())
                # Active word: yellow (ASS colours are &HBBGGRR) and slightly larger.
                parts.append(r"{\c&H00E6FF&\fscx108\fscy108}" + t + r"{\r}" if j == i else t)
            out.append(f"Dialogue: 0,{_ts(start)},{_ts(max(end, start + 60))},Cap,,0,0,0,,"
                       + " ".join(parts))
    return "\n".join(out) + "\n"


def filter_graph(layout: str, ass_path: Path, fonts_dir: Path, logo: bool = False) -> str:
    """Video filter graph. Input 0 is the source; input 1, when logo=True, is the logo PNG.
    The logo goes under the subtitles so captions stay readable if they ever meet."""
    subs = f"ass={_filter_path(ass_path)}:fontsdir={_filter_path(fonts_dir)}"
    if layout == "crop":
        base = f"[0:v]scale=-2:{H},crop={W}:{H},setsar=1[base]"
    else:
        # blurfill: whole frame centered over a blurred, zoomed copy of itself. Safer than a
        # blind center crop for two-person podcasts and gameplay where the action isn't centered.
        base = (
            f"[0:v]split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            f"scale={W}:{H},eq=brightness=-0.12[bg];"
            f"[b]scale={W}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2-120,setsar=1[base]"
        )
    if not logo:
        return f"{base};[base]{subs}[v]"
    return (f"{base};[1:v]scale={LOGO_W}:-1,format=rgba[lg];"
            f"[base][lg]overlay=W-w-{LOGO_X_MARGIN}:{LOGO_Y}:format=auto,{subs}[v]")


def _filter_path(p: Path) -> str:
    """Escape a path for a filter option inside -filter_complex. ffmpeg unescapes twice: once
    for the option list (':' separates options, so 'C:/' must be 'C\\:/') and once for the graph
    (',' ';' '[' ']' are syntax). Windows drive letters break with only one level."""
    v = re.sub(r"([\\:'])", r"\\\1", str(p).replace("\\", "/"))
    return re.sub(r"([\\'\[\],;])", r"\\\1", v)


def render(s: Settings, *, media_path: str, words: list[dict], start_ms: int, end_ms: int,
           hook: str, out_path: Path, work: Path, layout: str | None = None,
           disclosure: str = "", logo_path: str | None = None) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass = work / f"cap_{start_ms}_{end_ms}.ass"
    ass.write_text(build_ass(words, start_ms, end_ms, hook, s.font_name,
                             s.caption_words_per_chunk, disclosure), encoding="utf-8")
    tmp = out_path.with_suffix(".part.mp4")
    cmd = [
        ffmpeg(), "-y", "-v", "error",
        "-ss", f"{start_ms / 1000:.3f}", "-t", f"{(end_ms - start_ms) / 1000:.3f}",
        "-i", media_path,
        *(["-i", logo_path] if logo_path else []),
        "-filter_complex", filter_graph(layout or s.render_layout, ass, s.fonts_dir,
                                        logo=bool(logo_path)),
        "-map", "[v]", "-map", "0:a:0?",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
        "-c:v", "libx264", "-preset", s.render_preset, "-crf", str(s.render_crf),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(tmp),
    ]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode:
        # The command line alone is useless for diagnosis; keep ffmpeg's own message.
        raise RuntimeError(f"ffmpeg exit {proc.returncode}: {proc.stderr.strip()[-800:]}")
    tmp.replace(out_path)  # atomic: a half-written file never looks finished
    return {"path": str(out_path), "size_bytes": out_path.stat().st_size,
            "render_s": round(time.monotonic() - t0, 2)}
