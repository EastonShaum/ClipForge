"""Post caption builder: hook/title + campaign-required tags + niche hashtags + disclosure."""

import re

NICHE_TAGS = {
    "gaming": ["#gaming", "#gamingclips", "#twitch"],
    "podcast": ["#podcast", "#podcastclips"],
    "interview": ["#interview", "#podcastclips"],
}
PLATFORM_REMINDER = {
    "tiktok": "Turn ON: Content disclosure → Paid partnership",
    "youtube": "Tick: Paid promotion (Details → Show more)",
    "instagram": "Turn ON: Paid partnership label",
    "x": "Keep #ad at the start of the post",
}


def build_caption(*, title: str, hook: str, category: str, required_tags: str,
                  caption_notes: str, disclosure: str, must_mention: str = "") -> str:
    """Disclosure goes first: FTC guidance says it must be hard to miss, not buried in hashtags.
    A brief that says the caption must name someone gets the first name up front (D40)."""
    lead = (title or hook).strip()
    if not mentions(lead, must_mention):
        lead = f"{name_list(must_mention)[0]}: {lead}"
    req = " ".join(t for t in required_tags.replace(",", " ").split() if t)
    niche = " ".join(NICHE_TAGS.get(category.lower().strip(), ["#clips"]))
    parts = [f"{disclosure.strip()} {lead}".strip() if disclosure.strip() else lead]
    if caption_notes.strip():
        parts.append(caption_notes.strip())
    tags = " ".join(dict.fromkeys(f"{req} {niche}".split()))  # dedupe, keep order
    if tags:
        parts.append(tags)
    return "\n\n".join(parts)


def name_list(names: str) -> list[str]:
    """"Core Boys, StableRonaldo (Ron)" -> ["Core Boys", "StableRonaldo", "Ron"]."""
    out = []
    for n in re.split(r"[,\n]", names):
        name, _, nick = n.partition("(")
        out += [x.strip() for x in (name, nick.rstrip(") ")) if x.strip()]
    return list(dict.fromkeys(out))


def mentions(text: str, names: str) -> bool:
    """Does the text name one of them in words? Hashtags don't count (the brief asks for the
    name in the caption or on-screen text). "CORE boys" and "CoreBoys" both match "Core Boys"."""
    if not name_list(names):
        return True
    words = re.sub(r"[#@][\w.]+", " ", text)
    return any(re.search(r"\b" + r"\s*".join(map(re.escape, n.split())) + r"\b", words, re.I)
               for n in name_list(names))
