import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from conftest import configured, ingest
from hortator.footer import DEFAULT_FOOTER_TEMPLATE, render_footer
from hortator.discord_gateway import CouncilClient
from hortator.discord_text import CodeBlock, model_message
from hortator.models import Bot, OWNER_ID
from test_discord_formatting import message
from test_provider import Fragments, install_client
from test_runtime import completion, settle
from test_typing import prepare_bot


UNKNOWN = "-# TTFT: — | TPS: —"


async def test_legacy_footer_defaults_are_read_only_and_explicit_disable_survives_save(kernel, owner):
    for bot_id in ("hortator", "ada", "socrates"):
        bot = kernel.store.get("bots", bot_id)
        for key in ("revision", "footer_enabled", "footer_template"):
            bot.pop(key, None)
        kernel.store.put("bots", bot)
    before = kernel.store.rows("SELECT * FROM entities")
    assert kernel.service.inspect("bots", "hortator")["footer_enabled"] is True
    assert kernel.service.inspect("bots", "ada")["footer_enabled"] is False
    assert kernel.store.rows("SELECT * FROM entities") == before
    saved = await kernel.service.save(owner, "bots", "hortator", {"footer_enabled": False})
    assert saved["footer_template"] == DEFAULT_FOOTER_TEMPLATE
    saved = await kernel.service.save(owner, "bots", "hortator", {"name": "Director"})
    assert saved["footer_enabled"] is False
    assert Bot(id="new", name="New", role="hortator", model_profile_id="balanced").footer_enabled


@pytest.mark.parametrize(
    "template",
    ["", " ", "x\n{{TTFT}}", "x\t{{TPS}}", "{{SECRET}}", "{TPS}", "{{TPS}", "```bad```", "x" * 301],
)
def test_invalid_footer_templates_are_rejected(template):
    with pytest.raises(ValidationError):
        Bot(id="bot", name="Bot", model_profile_id="balanced", footer_template=template)


def test_literal_templates_metrics_aliases_and_missing_values():
    bot = {"id": "hortator", "name": "Hortator", "role": "hortator"}
    measured = {
        "ttft_ms": 1858,
        "duration_ms": 11858,
        "output_tokens": 4777,
        "input_tokens": 8192,
        "model": "kimi-k3",
    }
    assert render_footer(bot, request=measured) == "-# TTFT: 1858ms | TPS: 477.7"
    bot["footer_template"] = "{{PROVIDER}} | {{CONTEXT}} | {{MODEL SELECTED}} | {{MODEL}} | {{BOT}}"
    assert (
        render_footer(bot, {"context_window": 262144}, {"name": "Compute"}, measured)
        == "-# Compute | 8192/262144 | kimi-k3 | kimi-k3 | Hortator"
    )
    bot["footer_template"] = DEFAULT_FOOTER_TEMPLATE
    for changes in ({"ttft_ms": None}, {"duration_ms": 1858}, {"output_tokens": None}, {"output_tokens": 0}):
        assert render_footer(bot, request={**measured, **changes}).endswith("TPS: —")
    assert render_footer(bot) == UNKNOWN
    assert render_footer({**bot, "footer_enabled": False}, request=measured) == ""
    normalized = Bot(
        id="bot",
        name="Bot",
        model_profile_id="balanced",
        footer_template="-# {{ttft}} | {{ MODEL SELECTED }}",
    )
    assert normalized.footer_template == "{{ttft}} | {{ MODEL SELECTED }}"


async def test_footer_redacts_before_escaping_and_bounds_unicode_metadata(kernel):
    secret = "fake_template_secret_*"
    kernel.vault.put("provider/openrouter/api_key", secret)
    bot = {"role": "hortator", "footer_template": "{{PROVIDER}} | {{MODEL}} | " + secret}
    footer = render_footer(bot, {"model": "```\n" + "🙂" * 300}, {"name": secret}, redact=kernel.vault.redact)
    assert secret not in footer and "fake" not in footer
    assert "REDACTED" in footer and "```" not in footer and "\n" not in footer
    assert len(footer.encode("utf-16-le")) // 2 <= 500


async def test_footer_commands_target_each_bot_preserve_configuration_and_require_owner(kernel):
    channel = SimpleNamespace(id=123, send=AsyncMock())
    bot = kernel.store.get("bots", "hortator")
    before = kernel.store.get("bots", "ada")
    for text in ("!footer ada enable", "!footer ada template {{BOT}} | {{MODEL SELECTED}} | {{TPS}}"):
        await kernel.connector.receive("hortator", message(text, channel))
    ada = kernel.store.get("bots", "ada")
    assert ada["footer_enabled"] and ada["footer_template"] == "{{BOT}} | {{MODEL SELECTED}} | {{TPS}}"
    assert {k: v for k, v in ada.items() if k not in {"revision", "footer_enabled", "footer_template"}} == {
        k: v for k, v in before.items() if k not in {"revision", "footer_enabled", "footer_template"}
    }
    await kernel.connector.receive("hortator", message("!footer disabled", channel))
    assert kernel.store.get("bots", "hortator")["footer_enabled"] is False
    assert "\n-# " not in channel.send.await_args.kwargs["content"]
    await kernel.connector.receive("hortator", message("!footer enable", channel))
    assert channel.send.await_args.kwargs["content"].endswith(UNKNOWN)
    await kernel.connector.receive("hortator", message("!footer ada template {{PASSWORD}}", channel))
    assert "Command failed" in channel.send.await_args.kwargs["content"]
    assert kernel.store.get("bots", "ada") == ada
    for spoof in ("human", "bot", "webhook"):
        incoming = message("!footer disable", channel)
        if spoof == "human":
            incoming.author.id = 999
        elif spoof == "bot":
            incoming.author.bot = True
        else:
            incoming.webhook_id = 999
        channel.send.reset_mock()
        await kernel.connector.receive(bot["id"], incoming)
        channel.send.assert_not_awaited()
    assert kernel.store.get("bots", "hortator")["footer_enabled"] is True
    assert not kernel.store.rows("SELECT * FROM requests")


@pytest.mark.parametrize("bot_id", ["ada", "socrates", "dirac", "curie", "hortator"])
async def test_each_identity_can_enable_and_disable_its_own_footer(kernel, bot_id):
    bot, channel_id = prepare_bot(kernel, bot_id)
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=999)))
    kernel.connector.clients[bot_id] = SimpleNamespace(is_ready=lambda: True, get_channel=lambda _: channel)
    bot["footer_enabled"] = True
    await kernel.connector.send(bot, channel_id, "**Hello**", None, [])
    assert channel.send.await_args.args[0] == "**Hello**\n" + UNKNOWN
    bot["footer_enabled"] = False
    await kernel.connector.send(bot, channel_id, "**Hello**", None, [])
    assert channel.send.await_args.args[0] == "**Hello**"


@pytest.mark.parametrize("content", ["```python\nprint('hello')", "🙂" * 975, "```python\n" + "🙂" * 2000])
async def test_footer_reserves_discord_budget_closes_code_and_preserves_attachments(
    kernel, tmp_path, content
):
    bot, channel_id = prepare_bot(kernel, "hortator")
    bot["footer_template"] = "x" * 299
    media = tmp_path / "sound.mp3"
    media.write_bytes(b"synthetic-media")
    captured = {}

    async def send(text, **kwargs):
        captured.update(text=text, kwargs=kwargs, files={f.filename: f.fp.read() for f in kwargs["files"]})
        return SimpleNamespace(id=999)

    kernel.connector.clients["hortator"] = SimpleNamespace(
        is_ready=lambda: True, get_channel=lambda _: SimpleNamespace(send=send)
    )
    await kernel.connector.send(bot, channel_id, content, "123", [media], nonce="out_test")
    assert captured["text"].endswith("\n-# " + "x" * 299)
    assert captured["text"].count("```") % 2 == 0
    assert len(captured["text"].encode("utf-16-le")) // 2 <= 2000
    assert captured["files"]["sound.mp3"] == b"synthetic-media"
    if "full-response.txt" in captured["files"]:
        assert captured["files"]["full-response.txt"].decode() == content
    assert captured["kwargs"]["reference"].message_id == 123
    assert not captured["kwargs"]["allowed_mentions"].everyone


@pytest.mark.parametrize("terminal", ["tool_call", "content"])
async def test_final_request_footer_survives_tool_rounds_and_is_separate_from_canonical_answer(
    kernel, terminal
):
    bot = configured(kernel, enabled_plugins=["memory"], footer_enabled=True)
    ingest(kernel)
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "memory1",
                                        "type": "function",
                                        "function": {
                                            "name": "memory",
                                            "arguments": '{"operation":"read","key":"idea"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        packets = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "speak1",
                                    "type": "function",
                                    "function": {
                                        "name": "council_speak",
                                        "arguments": '{"content":"A considered answer"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": {"prompt_tokens": 5678, "completion_tokens": 42}},
        ]
        if terminal == "content":
            packets[0] = {"choices": [{"delta": {"content": "A considered answer"}, "finish_reason": "stop"}]}
        stream = "".join("data: " + json.dumps(packet) + "\n\n" for packet in packets) + "data: [DONE]\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Fragments(stream.encode())
        )

    await install_client(kernel, handle)
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=999)))
    kernel.connector.clients["ada"] = SimpleNamespace(is_ready=lambda: True, get_channel=lambda _: channel)
    kernel.connector.typing = AsyncMock()
    await kernel.engine.tick()
    await settle(kernel)
    assert calls == 2
    requests = kernel.store.rows("SELECT * FROM requests ORDER BY started_at")
    assert requests[0]["ttft_ms"] is None and requests[1]["ttft_ms"] is not None
    final = requests[1]
    expected = (
        f"-# TTFT: {final['ttft_ms']:.0f}ms | TPS: {42000 / (final['duration_ms'] - final['ttft_ms']):.1f}"
    )
    assert channel.send.await_args.args[0] == "A considered answer\n" + expected
    outbox = kernel.store.one("SELECT * FROM outbox")
    assert outbox["status"] == "sent" and outbox["content"] == "A considered answer"
    queued = next(e for e in kernel.store.events() if e["kind"] == "delivery.queued")
    assert queued["data"]["footer"] == expected and queued["data"]["request_id"] == final["id"]
    echo = SimpleNamespace(
        id=999,
        channel=SimpleNamespace(id=222222222222222222),
        author=SimpleNamespace(id=int(bot["application_id"]), bot=True, display_name="Ada"),
        webhook_id=None,
        nonce=outbox["id"],
        guild=SimpleNamespace(id=111111111111111111),
        content=channel.send.await_args.args[0],
        created_at=datetime.now(timezone.utc),
        attachments=[],
        reference=None,
    )
    assert kernel.connector.reconcile(echo) == "A considered answer"
    echo.nonce = None
    await kernel.connector.receive("socrates", echo, historical=True)
    assert (
        kernel.store.one("SELECT content FROM messages WHERE discord_id='999'")["content"]
        == "A considered answer"
    )
    client = CouncilClient(kernel.connector, "ada")
    try:
        await client.on_raw_message_edit(SimpleNamespace(message_id=999, data={"content": echo.content}))
        assert (
            kernel.store.one("SELECT content FROM messages WHERE discord_id='999'")["content"]
            == "A considered answer"
        )
        await client.on_raw_message_edit(
            SimpleNamespace(message_id=999, data={"content": "Edited answer\n" + expected})
        )
        assert (
            kernel.store.one("SELECT content FROM messages WHERE discord_id='999'")["content"]
            == "Edited answer"
        )
    finally:
        await client.close()
    kernel.store.execute("DELETE FROM messages WHERE discord_id='999'")
    assert kernel.connector.reconcile(echo) == "A considered answer"
    echo.author.id = int(OWNER_ID)
    assert kernel.connector.reconcile(echo) is None


async def test_long_preview_embed_updates_recover_original_answer(kernel):
    bot = configured(kernel, footer_enabled=True)
    content = "```python\n" + "🙂" * 1500 + "\n```"
    kernel.store.ingest(
        discord_id="999",
        channel_id="123",
        author_id=bot["application_id"],
        author_name="Ada",
        content=content,
        bot_id="ada",
    )
    kernel.store.execute(
        "INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,status,created_at,discord_id) VALUES(?,?,?,?,?,?,?,?)",
        ("out_preview", "turn", "ada", "123", content, "sent", 1, "999"),
    )
    kernel.store.emit(
        "delivery.queued", {"outbox_id": "out_preview", "footer": UNKNOWN}, bot_id="ada", turn_id="turn"
    )
    wire, attached = model_message(content, UNKNOWN)
    assert attached
    original = kernel.store.one("SELECT * FROM messages WHERE discord_id='999'")
    assert kernel.connector.canonical_edit(original, wire) == content
    assert kernel.connector.canonical_edit(original, "Edited\n" + UNKNOWN) == "Edited"


async def test_command_reports_and_incidents_have_bounded_footers_without_old_timings(kernel):
    channel = SimpleNamespace(id=123, send=AsyncMock(return_value=SimpleNamespace(id=999)))
    bot = kernel.store.get("bots", "hortator")
    bot.pop("revision")
    bot["footer_template"] = "🙂" * 299
    kernel.store.put("bots", bot)
    for result in (CodeBlock("🙂" * 2000), {"report": "🙂" * 2000}, "```python\nprint('ok')"):
        channel.send.reset_mock()
        await kernel.connector.reply(channel, result)
        for call in channel.send.await_args_list:
            assert call.kwargs["content"].count("```") % 2 == 0
            assert "\n-# " in call.kwargs["content"]
            assert len(call.kwargs["content"].encode("utf-16-le")) // 2 <= 2000
    bot["footer_template"] = DEFAULT_FOOTER_TEMPLATE
    kernel.store.put("bots", bot)
    channel.guild = SimpleNamespace(id=456)
    kernel.connector.clients["hortator"] = SimpleNamespace(
        is_ready=lambda: True, get_channel=lambda _: channel
    )
    settings = {
        **kernel.store.get("settings", "global"),
        "control_channel_id": "123",
        "control_guild_id": "456",
    }
    event = kernel.store.emit("provider.failure", {"provider_id": "openrouter", "error": "Synthetic timeout"})
    await kernel.connector.notify_event(event, settings)
    assert channel.send.await_args.kwargs["content"].endswith(UNKNOWN)


async def test_intentional_silence_does_not_send_a_footer_only_message(kernel):
    configured(kernel, footer_enabled=True)
    ingest(kernel)
    await install_client(kernel, lambda _: completion(silence=True))
    kernel.engine.transport = AsyncMock()
    await kernel.engine.tick()
    await settle(kernel)
    kernel.engine.transport.send.assert_not_awaited()
    assert kernel.store.one("SELECT status FROM turns")["status"] == "silent"
