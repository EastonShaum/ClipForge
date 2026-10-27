"""Campaign brief screenshots: store what the brief stage read, and apply it to the campaign
when you say so. Applying only adds or fills in; it never removes what you entered, and it never
changes the disclosure (our #ad + on-screen tag stays, whatever the brief asks)."""

import json
import re
import sqlite3

PLATFORMS = ("tiktok", "youtube", "instagram", "x")


def _tag(t: str) -> str:
    t = t.strip()
    return t if t[:1] in "#@" else f"#{t}"


def merged(conn: sqlite3.Connection, campaign_id: int) -> dict:
    """Everything read from this campaign's brief screenshots, deduplicated, in upload order."""
    out = {"summary": [], "required_tags": [], "caption_text": [], "disclosure": [],
           "platforms": [], "end_date": [], "footage_links": [], "logo_rule": [], "rules": [],
           "length": []}
    rows = conn.execute("SELECT extracted FROM campaign_docs WHERE campaign_id=? AND extracted"
                        " IS NOT NULL ORDER BY id", (campaign_id,)).fetchall()
    for r in rows:
        b = json.loads(r["extracted"])
        for k in ("summary", "caption_text", "disclosure", "end_date", "logo_rule"):
            if b.get(k):
                out[k].append(b[k].strip())
        out["required_tags"] += [_tag(t) for t in b.get("required_tags", []) if t.strip()]
        out["platforms"] += [p.lower() for p in b.get("platforms", []) if p.lower() in PLATFORMS]
        out["footage_links"] += b.get("footage_links", [])
        out["rules"] += b.get("rules", [])
        lo, hi = b.get("min_seconds", -1), b.get("max_seconds", -1)
        if lo > 0 or hi > 0:
            out["length"].append(f"{lo:.0f}" if lo > 0 else "?")
            out["length"][-1] += f"–{hi:.0f} s" if hi > 0 else "+ s"
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def apply(conn: sqlite3.Connection, campaign_id: int) -> list[str]:
    """Fill the campaign from its briefs. Returns what changed, for the confirmation message."""
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    m = merged(conn, campaign_id)
    changes, sets = [], {}

    have = c["required_tags"].split()
    tags = have + [t for t in m["required_tags"] if t.lower() not in {h.lower() for h in have}]
    if tags != have:
        sets["required_tags"] = " ".join(tags)
        changes.append(f"tags: {sets['required_tags']}")
    if m["platforms"]:
        plats = ",".join(p for p in PLATFORMS if p in m["platforms"])
        if plats != c["allowed_platforms"]:
            sets["allowed_platforms"] = plats
            changes.append(f"platforms: {plats}")
    if m["end_date"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["end_date"][-1]) \
            and m["end_date"][-1] != c["end_date"]:
        sets["end_date"] = m["end_date"][-1]
        changes.append(f"end date: {sets['end_date']}")
    if m["caption_text"] and not c["caption_notes"]:
        sets["caption_notes"] = " ".join(m["caption_text"])
        changes.append("required caption text")
    links = [u for u in m["footage_links"] if u not in c["source_links"]]
    if links:
        sets["source_links"] = " ".join(filter(None, [c["source_links"], *links]))
        changes.append(f"{len(links)} footage link(s)")
    notes = [n for n in (m["logo_rule"] + [f"Length {x}" for x in m["length"]] + m["rules"])
             if n not in c["notes"]]
    if notes:
        sets["notes"] = " · ".join(filter(None, [c["notes"], *notes]))
        changes.append(f"{len(notes)} rule(s) in notes")
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE campaigns SET {cols} WHERE id=?", (*sets.values(), campaign_id))
        conn.execute("UPDATE campaign_docs SET applied_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                     " WHERE campaign_id=? AND extracted IS NOT NULL", (campaign_id,))
    return changes
