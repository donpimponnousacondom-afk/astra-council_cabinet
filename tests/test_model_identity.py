import json

import httpx
import pytest

from conftest import configured, ingest
from hortator.footer import render_footer
from hortator.prompt_templates import catalog
from hortator.slash_commands import SlashContexts
from support.provider import install_client
from support.runtime import completion

CHANNEL = "222222222222222222"


@pytest.mark.parametrize("kind", ["ordinary", "slash", "panel"])
@pytest.mark.parametrize("transcript_format", ["structured", "conversation"])
async def test_current_request_model_matches_tail_and_footer_without_changing_prefix(
    kernel, monkeypatch, kind, transcript_format
):
    bot = configured(
        kernel,
        transcript_format=transcript_format,
        dynamic_prompt="Currently using {{MODEL}}; keep {unknown} literal.",
        footer_enabled=True,
        footer_template="{{MODEL}}",
        persona="Stable bot identity",
    )
    ingest(kernel, content="Older messages may mention another model.")
    monkeypatch.setattr("hortator.context.time.time", lambda: 1_800_000_000.0)
    contexts = (
        kernel.engine.contexts
        if kind == "ordinary"
        else SlashContexts(kernel.store, kernel.pool, {"kind": kind}, None)
    )
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        body = completion("Complete answer").json()
        body["model"] = "upstream-undisclosed-route"
        return httpx.Response(200, json=body)

    await install_client(kernel, respond)
    profile = kernel.store.get("profiles", "balanced")
    exchanges = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-one", "type": "function", "function": {"name": "example", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-one", "content": "Earlier tool result"},
    ]
    prefix = None
    for round_index, model in enumerate(("vendor/first-model", "vendor/second-{now}")):
        # The turn's captured profile is authoritative, not a fresh lookup of
        # the bot's assigned profile or the provider's response model label.
        current = {**profile, "model": model}
        messages, meta = await contexts.assemble(
            bot,
            current,
            CHANNEL,
            kernel.store.transcript(CHANNEL),
            "Earlier summary",
            round_index=round_index,
            extras=exchanges,
        )
        facts = next(p for p in meta["prompt_layers"] if p["id"] == "runtime_facts")
        values, _ = json.JSONDecoder().raw_decode(facts["content"].removeprefix("Runtime facts (trusted): "))
        assert values["model"] == model
        assert messages[-2] == exchanges[-1]
        assert model in messages[-1]["content"]
        assert f"Currently using {model}; keep {{unknown}} literal." in messages[-1]["content"]
        assert model not in str(messages[:-1])
        if prefix is not None:
            assert messages[:-1] == prefix
        prefix = messages[:-1]
        result = await kernel.pool.complete(
            bot=bot,
            profile=current,
            messages=messages,
            tools=[],
            turn_id=f"model-{kind}-{round_index}",
            context=meta,
        )
        recorded = kernel.store.one("SELECT model FROM requests WHERE id=?", (result.request_id,))
        assert captured[-1]["model"] == recorded["model"] == values["model"]
        assert render_footer(bot, profile, request=recorded) == "-# " + model
    assert kernel.store.get("profiles", "balanced")["model"] == "test-model"
    assert kernel.store.get("bots", "ada")["dynamic_prompt"] == bot["dynamic_prompt"]


async def test_model_tail_respects_layer_switches_and_custom_templates(kernel, owner):
    bot = configured(kernel, dynamic_prompt="Using {{MODEL}}", disabled_prompt_layers=["runtime_facts"])
    profile = kernel.store.get("profiles", "balanced")
    layers = kernel.engine.contexts.dynamic_layers(bot, profile, CHANNEL, 0, 0)
    assert [p["id"] for p in layers] == ["dynamic_prompt"]
    assert layers[0]["content"] == "Using test-model"
    bot = {**bot, "disabled_prompt_layers": ["runtime_facts", "dynamic_prompt"]}
    assert kernel.engine.contexts.dynamic_layers(bot, profile, CHANNEL, 0, 0) == []
    await kernel.service.save(
        owner, "prompts", "runtime-runtime-facts", {"content": "Configured request model: {{MODEL}}"}
    )
    bot = {**bot, "disabled_prompt_layers": ["dynamic_prompt"]}
    layers = kernel.engine.contexts.dynamic_layers(bot, profile, CHANNEL, 0, 0)
    assert layers[0]["content"] == "Configured request model: test-model"
    assert layers[0]["variables"] == ["MODEL"]
    assert "MODEL" in next(p for p in catalog() if p["id"] == "runtime_facts")["placeholders"]


@pytest.mark.parametrize("token", ["{{MODEL}}", "{{ MODEL }}", "{{model}}"])
def test_footer_model_placeholder_is_literal_single_pass(token):
    from hortator.prompt_templates import render

    assert (
        render(token + " / {bot_name} / {{UNKNOWN}}", {"MODEL": "route/{bot_name}", "bot_name": "Ada"})
        == "route/{bot_name} / Ada / {{UNKNOWN}}"
    )
    assert render(token, {"bot_name": "Ada"}) == token
