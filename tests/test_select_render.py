import json
import subprocess

import pytest

from clipforge.stages import render, select
from clipforge.stages.transcribe import fake_transcript


def words(n=200, step=330):
    return [{"w": f"w{i}" + ("." if i % 12 == 11 else ""), "s": i * step, "e": i * step + 280}
            for i in range(n)]


def test_compact_transcript_breaks_on_sentences():
    text = select.compact_transcript(words(30))
    lines = text.splitlines()
    assert lines[0].startswith("[0.0] w0 ")
    assert lines[0].endswith("w11.")
    assert lines[1].startswith("[3.96] w12") or lines[1].startswith("[4.0] w12")


def test_snap_to_word_boundaries(settings):
    c = select.Candidate(start=3.1, end=14.2, hook_text="h", score=80)
    a, b = select.snap(c, words(), settings)
    assert a == 2970 - 150  # first word (330 ms grid) starting at/after 2.8 s
    assert (b - a) / 1000 >= settings.min_clip_s * 0.8


def test_snap_rejects_too_long(settings):
    c = select.Candidate(start=0, end=60, hook_text="h", score=80)
    assert select.snap(c, words(), settings) is None


def test_parse_json_block_variants():
    assert select.parse_json_block('```json\n{"clips": []}\n```') == {"clips": []}
    assert select.parse_json_block('Sure! {"clips": [1]} done') == {"clips": [1]}


def test_select_drops_overlaps_and_sorts(settings):
    w = words(400)
    sel = select.Selection(clips=[
        select.Candidate(start=10, end=22, hook_text="a", score=70),
        select.Candidate(start=11, end=23, hook_text="dup", score=60),
        select.Candidate(start=40, end=52, hook_text="b", score=90),
    ])

    def fake_cli(s, prompt):
        return sel, {"provider": "claude_cli", "model": "claude-haiku-5-5", "usd": 0,
                     "usd_equiv": 0}

    settings_cli = settings.model_copy(update={"select_provider": "claude_cli"})
    orig = select.call_claude_cli
    select.call_claude_cli = fake_cli
    try:
        out, _ = select.select(settings_cli, "p", w)
    finally:
        select.call_claude_cli = orig
    assert [c["hook_text"] for c in out] == ["b", "a"]


def test_claude_cli_envelope_parsing(settings, monkeypatch):
    payload = {"clips": [{"start": 3.0, "end": 14.0, "hook_text": "x", "hook_type": "funny",
                          "title": "t", "rationale": "r", "score": 77}]}
    envelope = {"type": "result", "is_error": False, "result": json.dumps(payload),
                "usage": {"input_tokens": 20000, "output_tokens": 3000}}

    def fake_run(cmd, **kw):
        assert "--tools" in cmd and "--no-session-persistence" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr(select.subprocess, "run", fake_run)
    sel, meta = select.call_claude_cli(settings, "prompt")
    assert sel.clips[0].hook_type == "funny"
    assert meta["usd"] == 0 and meta["usd_equiv"] == pytest.approx(0.0035)


def test_build_prompt_fills_everything(settings):
    body, _ = select.load_prompt("select-v1")
    p = select.build_prompt(settings, body, words=words(20), title='A "quoted" title',
                            category="gaming", feedback="- LIKED: x")
    assert "{" not in p.split("## Output")[0]  # no unfilled placeholders before the output spec
    assert "gaming" in p and "LIKED" in p and "[0.0]" in p


def test_ass_has_hook_disclosure_and_highlight():
    w = fake_transcript(20)["words"]
    ass = render.build_ass(w, 1000, 15000, "Watch this", "Montserrat ExtraBold", 3,
                           "Paid partnership #ad")
    assert "Hook,,0,0,0,,WATCH THIS" in ass
    assert "Disc,,0,0,0,,Paid partnership #ad" in ass
    assert r"{\c&H00E6FF&" in ass
    assert ass.count("Dialogue: 0,") > 5


def test_chunk_words_breaks_on_pause():
    w = [{"w": "a", "s": 0, "e": 100}, {"w": "b", "s": 120, "e": 200},
         {"w": "c", "s": 2000, "e": 2100}]
    assert [len(c) for c in render.chunk_words(w, 3)] == [2, 1]


def _times(line):
    a, b = line.split(",")[1:3]
    ms = [int(h) * 3600000 + int(m) * 60000 + round(float(s) * 1000)
          for h, m, s in (t.split(":") for t in (a, b))]
    return ms


def test_captions_stay_on_one_line_and_never_overlap():
    text = ["WHAT", "WE", "WANTED", "CONGRATULATIONS", "EXTRAORDINARILY", "MMM", "WWW", "ok"]
    w = [{"w": t, "s": i * 300, "e": i * 300 + 250} for i, t in enumerate(text)]
    for chunk in render.chunk_words(w, 3):
        assert len(chunk) == 1 or render._line_width(chunk) <= render.CAPTION_MAX_W
    ass = render.build_ass(w, 0, 3000, "", "Montserrat ExtraBold", 3)
    caps = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0,")]
    assert all(r"\pos(" in ln and r"\q2" in ln for ln in caps)
    spans = [_times(ln) for ln in caps]
    assert all(end <= nxt for (_, end), (nxt, _) in zip(spans, spans[1:], strict=False))
