"""One structured Claude call for the readers and selection: streamed, so a long answer fits,
and priced even when the answer can't be used.

A failed call is still billed. PaidFailure carries what it cost, and whichever worker catches
it logs that in api_costs (handlers never touch the DB)."""

from pydantic import BaseModel, ValidationError

from clipforge.config import Settings
from clipforge.costs import llm_cost


class PaidFailure(RuntimeError):
    """The API answered (and billed) but the answer is unusable: cut off, refused or malformed."""

    def __init__(self, msg: str, meta: dict):
        super().__init__(msg)
        self.meta = meta


def structured(s: Settings, fmt: type[BaseModel], content, max_tokens: int,
               system: str | None = None) -> tuple[BaseModel, dict]:
    """Ask for JSON matching fmt. Returns (parsed, meta); raises PaidFailure with the meta when
    the answer stops early or doesn't parse. Thinking (on by default) counts in max_tokens."""
    import anthropic

    client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=4)
    extra = {"system": system} if system else {}
    with client.messages.stream(
        model=s.select_model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema",
                                  "schema": anthropic.transform_schema(fmt)}},
        **extra,
    ) as stream:
        resp = stream.get_final_message()
    tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
    usd = llm_cost(s.select_model, tin, tout)
    meta = {"provider": "anthropic", "model": s.select_model, "tokens_in": tin,
            "tokens_out": tout, "usd": usd, "usd_equiv": usd}
    if resp.stop_reason != "end_turn":
        raise PaidFailure(f"the answer stopped early ({resp.stop_reason}, {tout} tokens out)",
                          meta)
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return fmt.model_validate_json(text), meta
    except ValidationError as e:
        raise PaidFailure(f"the answer didn't match the format: {e}", meta) from e
