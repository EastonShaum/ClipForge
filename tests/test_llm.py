"""Structured Claude calls (stages.llm): a cut-off or malformed answer is still billed and logged."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from clipforge.stages import llm
from clipforge.worker import run_local
from tests.test_scout import _png, _upload


class Out(BaseModel):
    names: list[str]


def _fake_api(monkeypatch, text: str, stop: str):
    msg = SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                          usage=SimpleNamespace(input_tokens=10_000, output_tokens=8_000))

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return msg

    fake = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream()))
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)


def test_structured_parses_and_prices(settings, monkeypatch):
    _fake_api(monkeypatch, '{"names": ["Ron"]}', "end_turn")
    out, meta = llm.structured(settings, Out, "hi", 1000)
    assert out.names == ["Ron"] and meta["usd"] == pytest.approx(0.005)


@pytest.mark.parametrize("text,stop", [('{"names": ["Ro', "max_tokens"), ("{}", "end_turn")])
def test_an_unusable_answer_still_carries_its_cost(settings, monkeypatch, text, stop):
    _fake_api(monkeypatch, text, stop)
    with pytest.raises(llm.PaidFailure) as e:
        llm.structured(settings, Out, "hi", 1000)
    assert e.value.meta["tokens_out"] == 8_000 and e.value.meta["usd"] > 0


def test_a_failed_read_is_logged_in_api_costs(client, conn, tmp_path, monkeypatch):
    meta = {"provider": "anthropic", "model": "claude-haiku-5-5", "tokens_in": 12_849,
            "tokens_out": 8_000, "usd": 0.0053}

    def cut_off(s, path):
        raise llm.PaidFailure("the answer stopped early (max_tokens, 8000 tokens out)", meta)

    monkeypatch.setattr("clipforge.pipeline.scout_stage.read_screenshot", cut_off)
    _upload(client, _png(tmp_path))
    run_local(["scout"], once=True, idle_s=0)
    row = conn.execute("SELECT stage, units, usd FROM api_costs").fetchone()
    assert tuple(row) == ("scout", "12849 in, 8000 out (failed)", 0.0053)
    assert "stopped early" in conn.execute("SELECT last_error FROM jobs").fetchone()[0]
