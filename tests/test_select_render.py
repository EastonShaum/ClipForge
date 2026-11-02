import json
import subprocess

import pytest

from clipforge.stages import render, select


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


def test_prompt_carries_the_brief_and_names(settings):
    """D40: ordinary footage gets the bounty's brief and the names in it, like a live stream."""
    bounty = {"people": "IShowSpeed (Speed), StableRonaldo (Ron)", "must_mention": "Core Boys",
              "footage_scope": "Shibuya segment", "notes": "Core Boys reacting to Fortnite"}
    for name in ("select-v1", "select-v2"):
        body, _ = select.load_prompt(name)
        p = select.build_prompt(settings, body, words=words(20), title="t", category="irl",
                                feedback="", bounty=bounty)
        assert "People in this footage: IShowSpeed (Speed)" in p
        assert "must name one of: Core Boys" in p and "Shibuya segment" in p
        bare = select.build_prompt(settings, body, words=words(20), title="t", category="irl",
                                   feedback="")
        assert "No brief entered" in bare and "People: none listed" in bare
    body, _ = select.load_prompt("live-v1")
    p = select.build_prompt(settings, body, words=words(20), title="t", category="irl",
                            feedback="", live={"streamer": "N3on", "guests": "AB"}, bounty=bounty)
    assert "Guests at this stream: AB" in p and "Core Boys reacting to Fortnite" in p


def test_ass_holds_only_the_disclosure():
    ass = render.build_ass(14000, "Montserrat ExtraBold", "Paid partnership #ad")
    assert "Disc,,0,0,0,,Paid partnership #ad" in ass
    assert ass.count("Dialogue:") == 1  # no word captions: TikTok/Shorts auto-caption


def test_hook_card_keeps_emojis_with_their_words(tmp_path):
    from clipforge.stages import hookcard
    assert hookcard.runs("maze🎃👻") == [(False, "MAZE"), (True, "🎃👻")]
    assert hookcard.is_emoji("😂") and not hookcard.is_emoji("A")
    font = render.Path(__file__).parent.parent / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"
    size = hookcard.make_card("N3on gets jumpscared at a haunted maze 😱🎃", tmp_path / "c.png",
                              font, render.HOOK_MAX_W)
    assert size and size[0] <= render.HOOK_MAX_W and (tmp_path / "c.png").is_file()
    assert hookcard.make_card("   ", tmp_path / "e.png", font, 820) is None

