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
                  caption_notes: str, disclosure: str, must_mention: str = "",
                  mention_all: bool = False) -> str:
    """Disclosure goes first: FTC guidance says it must be hard to miss, not buried in hashtags.
    A brief that says the caption must name someone gets the missing name(s) up front (D40;
    all of them when the brief requires every name, D47)."""
    lead = (title or hook).strip()
    if miss := missing_names(f"{lead} {caption_notes}", must_mention, mention_all):
        lead = f"{' x '.join(miss)}: {lead}"
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


def _groups(names: str) -> list[list[str]]:
    """"Core Boys, StableRonaldo (Ron)" -> [["Core Boys"], ["StableRonaldo", "Ron"]]."""
    out = []
    for n in re.split(r"[,\n]", names):
        name, _, nick = n.partition("(")
        if g := [x.strip() for x in (name, nick.rstrip(") ")) if x.strip()]:
            out.append(g)
    return out


def missing_names(text: str, names: str, all_: bool = False) -> list[str]:
    """The names the text still has to add: none if it names one of them (or, with all_, the
    first form of each one it doesn't name). Hashtags and @handles don't count."""
    groups = _groups(names)
    words = re.sub(r"[#@][\w.]+", " ", text)

    def named(g):
        return any(re.search(r"\b" + r"\s*".join(map(re.escape, n.split())) + r"\b", words,
                             re.I) for n in g)
    if all_:
        return [g[0] for g in groups if not named(g)]
    return [] if not groups or any(named(g) for g in groups) else [groups[0][0]]


def mentions(text: str, names: str) -> bool:
    """Does the text name one of them in words? Hashtags don't count (the brief asks for the
    name in the caption or on-screen text). "CORE boys" and "CoreBoys" both match "Core Boys"."""
    if not name_list(names):
        return True
    words = re.sub(r"[#@][\w.]+", " ", text)
    return any(re.search(r"\b" + r"\s*".join(map(re.escape, n.split())) + r"\b", words, re.I)
               for n in name_list(names))
