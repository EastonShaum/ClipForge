"""On-screen hook card: the hook text with color emojis on a white box, drawn with Pillow to a
PNG that the render overlays for the whole clip. libass can't draw color emoji, Pillow can.

Text uses the bundled Montserrat; emojis use the system color-emoji font (Noto Color Emoji on
the Pi, Segoe UI Emoji on Windows). With no emoji font the emojis are dropped, never boxes."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

EMOJI_FONTS = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]
NOTO_PX = 109  # bitmap emoji fonts only load at their native size; glyphs are scaled after
SIZES = range(66, 45, -4)  # shrink until the hook fits in MAX_LINES
MAX_LINES = 2
PAD = (34, 20)
RADIUS = 26
INK = (17, 17, 17, 255)


def is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF
            or 0x2B00 <= cp <= 0x2BFF or cp in (0x200D, 0xFE0F, 0x20E3)
            or 0xE0020 <= cp <= 0xE007F)


def runs(word: str) -> list[tuple[bool, str]]:
    """Split a word into (is_emoji, text) runs; text runs are upper-cased."""
    out: list[tuple[bool, str]] = []
    for ch in word:
        e = is_emoji(ch)
        if out and out[-1][0] == e:
            out[-1] = (e, out[-1][1] + ch)
        else:
            out.append((e, ch))
    return [(e, t if e else t.upper()) for e, t in out]


def _emoji_font(override: str = "") -> ImageFont.FreeTypeFont | None:
    for p in [override, *EMOJI_FONTS]:
        if p and Path(p).is_file():
            for size in (NOTO_PX, 64):
                try:
                    return ImageFont.truetype(p, size)
                except OSError:
                    continue
    return None


class _Card:
    def __init__(self, text_font_path: Path, emoji_font_path: str = ""):
        self.text_path = text_font_path
        self.emoji = _emoji_font(emoji_font_path)
        self._glyphs: dict[str, Image.Image] = {}

    def glyph(self, seq: str) -> Image.Image | None:
        """An emoji run rendered once at the emoji font's own size, cropped to its ink."""
        if seq not in self._glyphs:
            im = None
            if self.emoji and seq.strip("\u200d\ufe0f"):
                f = self.emoji
                w = int(f.getlength(seq)) + 8
                im = Image.new("RGBA", (max(w, 8), int(f.size * 1.3)), (0, 0, 0, 0))
                ImageDraw.Draw(im).text((4, 0), seq, font=f, embedded_color=True)
                box = im.getbbox()
                im = im.crop(box) if box else None
            self._glyphs[seq] = im
        return self._glyphs[seq]

    def layout(self, words: list[str], size: int, max_w: int):
        font = ImageFont.truetype(str(self.text_path), size)
        em = int(size * 1.05)
        space = font.getlength(" ")

        def piece(e: bool, t: str):
            if not e:
                return ("t", t, font.getlength(t))
            g = self.glyph(t)
            if g is None:
                return None
            w = round(g.width * em / g.height)
            return ("e", g.resize((w, em), Image.LANCZOS), w + size * 0.06)

        lines: list[list] = [[]]
        widths = [0.0]
        for word in words:
            parts = [p for p in (piece(e, t) for e, t in runs(word)) if p]
            if not parts:
                continue
            ww = sum(p[2] for p in parts)
            add = ww + (space if lines[-1] else 0)
            if lines[-1] and widths[-1] + add > max_w:
                lines.append([])
                widths.append(0.0)
                add = ww
            if lines[-1]:
                lines[-1].append(("t", " ", space))
            lines[-1].extend(parts)
            widths[-1] += add
        return font, lines, widths


def make_card(hook: str, out: Path, text_font_path: Path, max_w: int,
              emoji_font_path: str = "") -> tuple[int, int] | None:
    """Write the hook card PNG; returns its (width, height), or None for an empty hook."""
    words: list[str] = []
    for w in hook.split():
        if words and all(is_emoji(ch) for ch in w):
            words[-1] += " " + w  # an emoji never starts a line on its own
        else:
            words.append(w)
    if not words:
        return None
    card = _Card(text_font_path, emoji_font_path)
    inner = max_w - 2 * PAD[0]
    tries = [card.layout(words, size, inner) for size in SIZES]
    # Largest size that fits in MAX_LINES, else the largest that fits in one more line.
    font, lines, widths = next(
        (t for n in (MAX_LINES, MAX_LINES + 1) for t in tries
         if len(t[1]) <= n and max(t[2]) <= inner), tries[-1])
    lines = [ln for ln in lines if ln]
    if not lines:
        return None
    asc, desc = font.getmetrics()
    line_h = int((asc + desc) * 1.08)
    w = int(min(max_w, max(widths) + 2 * PAD[0]))
    h = line_h * len(lines) + 2 * PAD[1]
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), RADIUS, fill=(255, 255, 255, 255))
    for i, (line, lw) in enumerate(zip(lines, widths, strict=True)):
        x = (w - lw) / 2
        top = PAD[1] + i * line_h
        for kind, val, adv in line:
            if kind == "t":
                d.text((x, top), val, font=font, fill=INK)
            else:  # emoji image, centred on the text's cap height
                y = top + (asc - val.height) // 2 + int(asc * 0.08)
                im.alpha_composite(val, (int(x), max(0, y)))
            x += adv
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return w, h
