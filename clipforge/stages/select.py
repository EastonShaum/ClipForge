"""Select stage: an LLM reads the timestamped transcript and returns scored candidate clips."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from clipforge.config import Settings
from clipforge.costs import CLI_ALIAS_TO_MODEL, llm_cost
from clipforge.stages.llm import structured

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# From research/hooks-playbook.md. Keep stable: analytics group by these values.
HOOK_TYPES = ["bold_claim", "specific_number", "question", "story_open", "conflict", "reaction",
              "reveal", "curiosity_gap", "relatable_pain", "fail_or_absurd", "skill_flex", "other"]

DEFAULT_RUBRIC = """\
- hook_strength (weight 30): do the first 2 seconds create curiosity or tension on their own?
- payoff (weight 20): is there a clear punchline, answer, reveal, or emotional peak?
- standalone (weight 15): does it make sense with zero context?
- emotion_energy (weight 15): laughter, anger, surprise, hype, vulnerability
- shareability (weight 10): would someone send it to a friend or argue in the comments?
- pacing (weight 10): little dead air; tight from first to last word"""


class Candidate(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    hook_text: str
    hook_type: str = "other"
    title: str = ""
    rationale: str = ""
    score: float = Field(ge=0, le=100)


class Selection(BaseModel):
    clips: list[Candidate]


def selection_schema() -> dict:
    return Selection.model_json_schema()


def compact_transcript(words: list[dict], max_words: int = 14, gap_ms: int = 700) -> str:
    """Group words into short lines, breaking at pauses and sentence ends."""
    lines, cur, cur_start = [], [], None
    prev_end = None
    for w in words:
        if cur and (len(cur) >= max_words or (prev_end is not None and w["s"] - prev_end > gap_ms)):
            lines.append(f"[{cur_start / 1000:.1f}] {' '.join(cur)}")
            cur, cur_start = [], None
        if cur_start is None:
            cur_start = w["s"]
        cur.append(w["w"])
        prev_end = w["e"]
        if w["w"].endswith((".", "?", "!")) and len(cur) >= 4:
            lines.append(f"[{cur_start / 1000:.1f}] {' '.join(cur)}")
            cur, cur_start = [], None
    if cur:
        lines.append(f"[{cur_start / 1000:.1f}] {' '.join(cur)}")
    return "\n".join(lines)


def load_prompt(name: str) -> tuple[str, str]:
    body = (PROMPTS / f"{name}.md").read_text()
    return body, hashlib.sha256(body.encode()).hexdigest()[:12]


def brief_block(bounty: dict | None) -> str:
    """What the bounty's brief pays for, for {brief} (D40): a moment outside it gets rejected,
    and a hook that doesn't name who the brief asks for breaks its rules."""
    b = bounty or {}
    lines = [f"Footage the brief allows: {b['footage_scope']}" if b.get("footage_scope") else "",
             f"The brief's rules: {b['notes']}" if b.get("notes") else "",
             f"Brief text: {b['brief_text']}" if b.get("brief_text") else "",
             f"Every hook_text and title must name one of: {b['must_mention']}"
             if b.get("must_mention") else ""]
    return "\n".join(x for x in lines if x) or "No brief entered: any strong moment counts."


def build_prompt(s: Settings, template: str, *, words: list[dict], title: str, category: str,
                 feedback: str, rubric: str | None = None, live: dict | None = None,
                 bounty: dict | None = None) -> str:
    """live: stream_info() for a live chunk; it switches to the live lengths and fills
    {streamer} and {people} (the live prompt's extra variables). bounty: the source's bounty,
    for {brief} and, on ordinary footage, {people} (D40)."""
    guests = ((live or {}).get("guests") or (bounty or {}).get("people") or "").strip()
    return template.format(
        category=category or "long-form",
        title=title.replace('"', "'"),
        n=s.live_candidates_requested if live else s.candidates_requested,
        min_s=int(s.live_min_clip_s if live else s.min_clip_s),
        max_s=int(s.live_max_clip_s if live else s.max_clip_s),
        streamer=(live or {}).get("streamer", ""),
        people=(f"{'Guests at this stream' if live else 'People in this footage'}: {guests}"
                if guests else f"{'Guests at this stream' if live else 'People'}: none listed"),
        brief=brief_block(bounty),
        rubric=rubric or DEFAULT_RUBRIC,
        feedback=feedback or "(no feedback yet)",
        hook_types=", ".join(HOOK_TYPES),
        transcript=compact_transcript(words),
    )


def snap(c: Candidate, words: list[dict], s: Settings, min_s: float | None = None,
         max_s: float | None = None) -> tuple[int, int] | None:
    """Snap LLM times to real word boundaries and enforce length limits."""
    min_s = s.min_clip_s if min_s is None else min_s
    max_s = s.max_clip_s if max_s is None else max_s
    if not words:
        return None
    start_ms, end_ms = int(c.start * 1000), int(c.end * 1000)
    first = next((w for w in words if w["s"] >= start_ms - 300), None)
    last = next((w for w in reversed(words) if w["e"] <= end_ms + 600), None)
    if not first or not last or last["e"] <= first["s"]:
        return None
    s_ms = max(0, first["s"] - 150)
    e_ms = last["e"] + 350
    dur = (e_ms - s_ms) / 1000
    if dur < min_s * 0.8 or dur > max_s * 1.15:
        return None
    return s_ms, e_ms


def parse_json_block(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        text = text[start : end + 1]
    return json.loads(text)


def call_claude_cli(s: Settings, prompt: str) -> tuple[Selection, dict]:
    """Run the local `claude` CLI headless on the user's subscription. No tools, no session."""
    exe = shutil.which(s.claude_cli_path) or s.claude_cli_path
    with tempfile.TemporaryDirectory() as cwd:  # empty cwd: no CLAUDE.md is picked up
        proc = subprocess.run(
            [exe, "-p", "--model", s.claude_cli_model, "--output-format", "json",
             "--tools", "", "--no-session-persistence", "--strict-mcp-config",
             "--json-schema", json.dumps(selection_schema()),
             "--system-prompt", "You select short-form video clips. Reply with JSON only."],
            input=prompt, capture_output=True, text=True, cwd=cwd, timeout=900,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed ({proc.returncode}): {proc.stderr[-800:]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI error: {str(envelope.get('result'))[:800]}")
    payload = envelope.get("structured_output") or parse_json_block(envelope.get("result", ""))
    usage = envelope.get("usage") or {}
    tin = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0)) + int(
        usage.get("cache_creation_input_tokens", 0))
    tout = int(usage.get("output_tokens", 0))
    model = CLI_ALIAS_TO_MODEL.get(s.claude_cli_model, s.claude_cli_model)
    meta = {"provider": "claude_cli", "model": model, "tokens_in": tin, "tokens_out": tout,
            "usd": 0.0, "usd_equiv": llm_cost(model, tin, tout)}
    return Selection.model_validate(payload), meta


def call_anthropic(s: Settings, prompt: str) -> tuple[Selection, dict]:
    from clipforge.stages.transcribe import MissingKey

    if not s.anthropic_api_key:
        raise MissingKey("CF_ANTHROPIC_API_KEY is not set")
    return structured(s, Selection, prompt, 32000, system="You select short-form video clips.")


def call_fake(s: Settings, words: list[dict]) -> tuple[Selection, dict]:
    """Evenly spaced candidates; keeps the pipeline testable without any LLM."""
    if not words:
        return Selection(clips=[]), {"provider": "fake", "model": "fake", "usd": 0.0,
                                     "usd_equiv": 0.0, "tokens_in": 0, "tokens_out": 0}
    total = words[-1]["e"] / 1000
    length = min(s.max_clip_s, max(s.min_clip_s, total / 3))
    clips, t, i = [], words[0]["s"] / 1000, 0
    while t + length <= total + 0.01 and i < s.candidates_requested:
        clips.append(Candidate(start=t, end=t + length, hook_text=f"Moment {i + 1}",
                               hook_type="other", title=f"Clip {i + 1}",
                               rationale="fake selector", score=90 - i * 5))
        t += length
        i += 1
    return Selection(clips=clips), {"provider": "fake", "model": "fake", "usd": 0.0,
                                    "usd_equiv": 0.0, "tokens_in": 0, "tokens_out": 0}


def estimate_usd(s: Settings, prompt: str) -> float:
    if s.select_provider != "anthropic":
        return 0.0
    return llm_cost(s.select_model, len(prompt) // 3, 5000)


def select(s: Settings, prompt: str, words: list[dict], min_s: float | None = None,
           max_s: float | None = None) -> tuple[list[dict], dict]:
    if s.select_provider == "fake":
        sel, meta = call_fake(s, words)
    elif s.select_provider == "claude_cli":
        sel, meta = _with_one_retry(lambda: call_claude_cli(s, prompt))
    elif s.select_provider == "anthropic":
        sel, meta = call_anthropic(s, prompt)
    else:
        raise ValueError(f"unknown select provider {s.select_provider}")

    out, seen = [], []
    for c in sorted(sel.clips, key=lambda c: c.score, reverse=True):
        snapped = snap(c, words, s, min_s, max_s)
        if not snapped:
            continue
        a, b = snapped
        if any(min(b, e) - max(a, st) > 5000 for st, e in seen):  # overlap > 5 s
            continue
        seen.append((a, b))
        out.append({**c.model_dump(), "start_ms": a, "end_ms": b,
                    "hook_type": c.hook_type if c.hook_type in HOOK_TYPES else "other"})
    return out, meta


def _with_one_retry(fn):
    try:
        return fn()
    except (ValidationError, json.JSONDecodeError, ValueError):
        return fn()
