"""Render stage: cut, reframe to 9:16, overlay the hook card + disclosure, normalize loudness.

No word-by-word captions: TikTok and Shorts add their own auto-captions, and burned-in ones
would sit on top of them. The hook card (hookcard.py, with color emojis) stays for the whole
clip, just below the picture so it never covers it; logos go in the band above the picture.

Placement follows platform safe zones on a 1080x1920 canvas: TikTok/Shorts/Reels UI covers
roughly the top 10%, the bottom 25% and a ~120 px strip on the right.
"""

import re
import subprocess
import time
from pathlib import Path

from clipforge.config import Settings
from clipforge.stages import layout as live_layout
from clipforge.stages.hookcard import make_card
from clipforge.tools import ffmpeg, ffprobe

W, H = 1080, 1920
HOOK_MAX_W = 820  # px; clear of the frame edge and TikTok's right-hand buttons
HOOK_X = 510  # center of the 100..920 band
PIC_SHIFT = 120  # blurfill: the picture sits this far above the canvas center
GAP = 24  # px between the picture and the hook card / logo strip
SAFE_TOP = 200  # TikTok's top UI ends around here
HOOK_TOP_MAX = 1200  # a two-line card from here still ends above TikTok's bottom UI (~1440)
# Required brand logos: one row on a dark strip in the blurred band above the picture, centered
# on the hook's column. Not over the picture: footage often carries its own marks there (Man On's
# has a Playmaker wordmark top-right) and a logo on top would hide them. The strip keeps white
# logos readable.
LOGO_H = 120
LOGO_GAP = 40
LOGO_PAD = (24, 12)  # strip padding x, y
LOGO_STRIP_H = LOGO_H + 2 * LOGO_PAD[1]


def placement(layout: str, src_w: int, src_h: int) -> tuple[int, int]:
    """(logo strip bottom, hook card top) for a source of this size: the card just under the
    picture, the logos just above it. Crop fills the canvas, so there is no free band."""
    if layout == "crop" or not (src_w and src_h):
        return SAFE_TOP + LOGO_STRIP_H, HOOK_TOP_MAX - 80
    pic_h = min(H, round(W * src_h / src_w / 2) * 2)
    top = (H - pic_h) // 2 - PIC_SHIFT
    return (max(SAFE_TOP + LOGO_STRIP_H, top - GAP),
            min(HOOK_TOP_MAX, top + pic_h + GAP))


def probe_size(path: str) -> tuple[int, int]:
    """Video width/height, or (0, 0) if ffprobe can't tell (placement then falls back)."""
    try:
        r = subprocess.run([ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60)
        w, h = r.stdout.strip().splitlines()[0].split(",")[:2]
        return int(w), int(h)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0, 0


def _ts(ms: int) -> str:
    cs = max(0, ms) // 10
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(dur_ms: int, font: str, disclosure: str) -> str:
    """ASS file with the always-visible disclosure label (FTC wants it in the video, not only
    the post caption)."""
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Disclosure: small, semi-opaque box, top-left inside the safe zone.
        f"Style: Disc,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,"
        "3,8,0,7,60,0,200,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 2,{_ts(0)},{_ts(dur_ms)},Disc,,0,0,0,,{_esc(disclosure)}",
    ]) + "\n"


def filter_graph(layout: str, ass_path: Path | None, fonts_dir: Path, logos: int = 0,
                 hook: bool = False, src: tuple[int, int] = (1920, 1080)) -> str:
    """Video filter graph. Input 0 is the source; inputs 1..logos are logo PNGs, then the hook
    card PNG if there is one. Single-image inputs repeat their frame for the whole clip."""
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
    logo_bottom, hook_top = placement(layout, *src)
    graph, cur = [base], "[base]"
    if logos:
        px, py = LOGO_PAD
        graph += [f"[{i}:v]scale=-1:{LOGO_H},format=rgba,"
                  f"pad=iw+{LOGO_GAP}:ih:{LOGO_GAP // 2}:0:color=black@0[l{i}]"
                  for i in range(1, logos + 1)]
        graph.append("".join(f"[l{i}]" for i in range(1, logos + 1))
                     + f"hstack=inputs={logos}[row0]" if logos > 1 else "[l1]null[row0]")
        graph.append(f"[row0]pad=iw+{2 * px - LOGO_GAP}:ih+{2 * py}:{px - LOGO_GAP // 2}:{py}:"
                     f"color=black@0.55,scale='min(iw,{HOOK_MAX_W})':-1[row]")
        graph.append(f"{cur}[row]overlay={HOOK_X}-w/2:{logo_bottom}-h:format=auto[logo]")
        cur = "[logo]"
    if hook:
        graph.append(f"{cur}[{logos + 1}:v]overlay={HOOK_X}-w/2:{hook_top}:format=auto[hook]")
        cur = "[hook]"
    if ass_path:  # the on-screen disclosure label
        graph.append(f"{cur}ass={_filter_path(ass_path)}:fontsdir={_filter_path(fonts_dir)}[v]")
    else:
        graph.append(f"{cur}null[v]")
    return ";".join(graph)


def _filter_path(p: Path) -> str:
    """Escape a path for a filter option inside -filter_complex. ffmpeg unescapes twice: once
    for the option list (':' separates options, so 'C:/' must be 'C\\:/') and once for the graph
    (',' ';' '[' ']' are syntax). Windows drive letters break with only one level."""
    v = re.sub(r"([\\:'])", r"\\\1", str(p).replace("\\", "/"))
    return re.sub(r"([\\'\[\],;])", r"\\\1", v)


def render(s: Settings, *, media_path: str, start_ms: int, end_ms: int, hook: str,
           out_path: Path, work: Path, layout: str | None = None, disclosure: str = "",
           logo_paths: list[str] | None = None) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass = None
    if disclosure:
        ass = work / f"disc_{start_ms}_{end_ms}.ass"
        ass.write_text(build_ass(end_ms - start_ms, s.font_name, disclosure), encoding="utf-8")
    card = work / f"hook_{start_ms}_{end_ms}.png"
    has_card = make_card(hook, card, s.fonts_dir / "Montserrat-ExtraBold.ttf", HOOK_MAX_W,
                         s.emoji_font) is not None
    tmp = out_path.with_suffix(".part.mp4")
    layout = live_layout.choose(layout or s.render_layout, media_path, start_ms, end_ms)
    cmd = [
        ffmpeg(), "-y", "-v", "error",
        "-ss", f"{start_ms / 1000:.3f}", "-t", f"{(end_ms - start_ms) / 1000:.3f}",
        "-i", media_path,
        *[a for p in logo_paths or [] for a in ("-i", p)],
        *(["-i", str(card)] if has_card else []),
        "-filter_complex", filter_graph(layout, ass, s.fonts_dir,
                                        logos=len(logo_paths or []), hook=has_card,
                                        src=probe_size(media_path)),
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
            "render_s": round(time.monotonic() - t0, 2), "layout": layout}
