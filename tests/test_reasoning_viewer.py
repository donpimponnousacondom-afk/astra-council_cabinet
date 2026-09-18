import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from conftest import configured, ingest
from test_provider import install_client
from test_runtime import settle
from test_slash_commands import interaction, enable as enable_slash
from hortator.plugins import ToolContext
from hortator.reasoning_viewer import ReasoningViewer, answer_view, captured_text, page_capture, reader_view
from hortator.slash_commands import ACK_RETRY_DELAY

CHANNEL = "222222222222222222"
MESSAGE = "888888888888888888"
PRIVATE = "777777777777777777"


def enable(k, **kwargs):
    bot = configured(k, enabled_plugins=["reasoning_viewer"], **kwargs)
    plugin = k.store.get("plugins", "reasoning_viewer")
    plugin.pop("revision")
    plugin["enabled"] = True
    k.store.put("plugins", plugin)
    return bot


def request(
    k,
    identity="req-final",
    *,
    text="Saved final reasoning.",
    bot="ada",
    turn="turn-r",
    at=1,
    status="completed",
    details=None,
    inline=None,
    calls=None,
):
    k.store.execute(
        """INSERT INTO requests(id,turn_id,bot_id,provider_id,profile_id,model,purpose,
        started_at,ended_at,status,body,context,response,reasoning_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            identity,
            turn,
            bot,
            "openrouter",
            "balanced",
            "test-model",
            "generation",
            at,
            at + 1,
            status,
            "{}",
            "{}",
            json.dumps({"tool_calls": calls or [], "finish_reason": "stop"}),
            7,
        ),
    )
    k.store.execute(
        "INSERT INTO request_diagnostics VALUES(?,?,?)",
        (
            identity,
            json.dumps(
                {
                    "capture": "recorded",
                    "status": status,
                    "reasoning_content": text,
                    "reasoning_details": details or [],
                    "inline_reasoning": inline or [],
                    "request_reasoning": [{"reasoning_content": "DO NOT EXPORT REPLAY"}],
                }
            ),
            1,
        ),
    )


async def binding(k, bot, *, final="req-final"):
    k.store.execute(
        """INSERT INTO outbox(id,turn_id,bot_id,channel_id,content,status,created_at,discord_id)
        VALUES('out-r','turn-r',?,?,'Visible answer','sent',1,?)""",
        (bot["id"], CHANNEL, MESSAGE),
    )
    row = await k.registry.reasoning_viewer.prepare(bot, "turn-r", final, "out-r", CHANNEL)
    if row:
        k.registry.reasoning_viewer.bind(row, MESSAGE)
        return k.store.one("SELECT * FROM reasoning_bindings WHERE id=?", (row["id"],))


def click(bot, custom, *, message=MESSAGE, ephemeral=False, iid="987654321012345678"):
    item = interaction(bot, interaction_id=iid)
    item.type = discord.InteractionType.component
    item.channel_id = int(CHANNEL)
    item.data = {"custom_id": custom, "component_type": 2}
    item.message = SimpleNamespace(
        id=int(message),
        author=SimpleNamespace(id=int(bot["application_id"])),
        flags=SimpleNamespace(ephemeral=ephemeral),
    )

    async def validate_response(**kwargs):
        if kwargs.get("view"):
            controls = [c for row in kwargs["view"].to_components() for c in row["components"]]
            ids = [c["custom_id"] for c in controls]
            assert len(ids) == len(set(ids)), "Discord rejects duplicate IDs, including disabled buttons"
        return SimpleNamespace(id=int(PRIVATE if not ephemeral else message))

    item.edit_original_response.side_effect = validate_response
    return item


async def open_view(k, bot, row):
    item = click(bot, f"hrv:{row['id']}:open")
    await k.connector.slash.receive(bot["id"], item)
    return item


async def test_plugin_is_opt_in_keyless_and_never_a_model_tool(kernel):
    bot = configured(kernel)
    plugin = kernel.service.public("plugins", kernel.store.get("plugins", "reasoning_viewer"))
    assert plugin["keyless"] and not plugin["enabled"]
    request(kernel)
    assert await binding(kernel, bot) is None
    bot = enable(kernel)
    ctx = ToolContext(bot, CHANNEL, "t")
    assert "reasoning_viewer" not in [v["function"]["name"] for v in kernel.registry.schemas(ctx)]
    for result in (
        await kernel.registry.call("reasoning_viewer", {}, ctx, "a"),
        await kernel.registry.call_raw("reasoning_viewer", "{}", ctx, "b"),
    ):
        assert not result["ok"]


@pytest.mark.parametrize("kind", ["absent", "count_only", "encrypted", "summary", "whitespace"])
async def test_no_button_without_actual_readable_reasoning(kernel, kind):
    bot = enable(kernel)
    details = (
        [{"type": "reasoning." + kind, "text": "opaque", "data": "opaque"}]
        if kind in ("encrypted", "summary")
        else []
    )
    request(kernel, text="   " if kind == "whitespace" else "", details=details)
    if kind == "absent":
        kernel.store.execute("DELETE FROM request_diagnostics")
    assert await binding(kernel, bot) is None


async def test_final_and_prior_rounds_are_separate_private_paged_and_restart_safe(kernel):
    bot = enable(kernel)
    request(kernel, "req-tool", text="Earlier tool reasoning " * 200, at=1, calls=[{"id": "call1"}])
    request(kernel, text="Final private reasoning", at=2)
    request(kernel, "req-other-bot", text="OTHER BOT SECRET", bot="hortator", at=3)
    request(kernel, "req-future", text="AFTER ANSWER SECRET", at=4)
    row = await binding(kernel, bot)
    assert json.loads(row["request_ids"]) == ["req-tool", "req-final"]
    wire = json.dumps(answer_view(row).to_components())
    assert "REASONING" in wire and "private reasoning" not in wire
    await install_client(kernel, lambda _: pytest.fail("Viewer must never call a provider"))
    first = await open_view(kernel, bot, row)
    first.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    body = first.edit_original_response.await_args.kwargs
    assert "Final answer" in body["content"] and "Final private reasoning" in body["content"]
    assert "Earlier tool" not in body["content"] and not body["allowed_mentions"].everyone
    session = kernel.store.one("SELECT * FROM reasoning_views")
    # Both the public button and private navigation are backed by persistent records.
    kernel.registry.reasoning_viewer = ReasoningViewer(kernel.store, kernel.vault)
    page = click(
        bot, f"hrv:{session['id']}:page:0:1", message=PRIVATE, ephemeral=True, iid="987654321012345679"
    )
    await kernel.connector.slash.receive(bot["id"], page)
    page.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    text = page.edit_original_response.await_args.kwargs["content"]
    assert "Tool round" in text and "Earlier tool reasoning" in text and "Page 2/" in text
    assert "Final private" not in text and len(text.encode("utf-16-le")) // 2 <= 2000
    await kernel.connector.slash.receive(bot["id"], first)
    assert first.response.defer.await_count == 1
    assert not kernel.store.rows("SELECT * FROM turns") and not kernel.store.rows("SELECT * FROM messages")
    events = str(kernel.store.rows("SELECT * FROM events"))
    for secret in (
        "Final private reasoning",
        "Earlier tool reasoning",
        "SECRET-INTERACTION-TOKEN",
        "OTHER BOT SECRET",
    ):
        assert secret not in events


async def test_prior_capture_can_enable_button_when_final_has_no_text_and_partial_is_labelled(kernel):
    bot = enable(kernel)
    request(kernel, "req-partial", text="Partial thought", status="failed", at=1)
    request(kernel, text="", at=2)
    row = await binding(kernel, bot)
    first = await open_view(kernel, bot, row)
    assert "no readable reasoning text" in first.edit_original_response.await_args.kwargs["content"]
    session = kernel.store.one("SELECT * FROM reasoning_views")
    item = click(bot, f"hrv:{session['id']}:page:0:0", message=PRIVATE, ephemeral=True)
    await kernel.connector.slash.receive(bot["id"], item)
    assert "Partial capture" in item.edit_original_response.await_args.kwargs["content"]


@pytest.mark.parametrize(
    "tamper",
    [
        "actor",
        "bot_actor",
        "app",
        "author",
        "channel",
        "message",
        "grant",
        "global",
        "changed_app",
        "uncertain",
        "unknown",
        "other_bot",
        "missing_request",
    ],
)
async def test_wrong_or_revoked_control_never_reveals_reasoning(kernel, tamper):
    bot = enable(kernel)
    request(kernel)
    row = await binding(kernel, bot)
    item = click(bot, f"hrv:{row['id']}:open")
    if tamper == "actor":
        item.user.id += 1
    elif tamper == "bot_actor":
        item.user.bot = True
    elif tamper == "app":
        item.application_id += 1
    elif tamper == "author":
        item.message.author.id += 1
    elif tamper == "channel":
        item.channel_id += 1
    elif tamper == "message":
        item.message.id += 1
    elif tamper in ("grant", "changed_app"):
        current = kernel.store.get("bots", bot["id"])
        current.pop("revision")
        current.update(
            enabled_plugins=[] if tamper == "grant" else ["reasoning_viewer"],
            application_id="123456789012345678" if tamper == "changed_app" else bot["application_id"],
        )
        kernel.store.put("bots", current)
    elif tamper == "global":
        kernel.store.execute(
            "UPDATE entities SET body=json_set(body,'$.enabled',json('false')) WHERE kind='plugins' AND id='reasoning_viewer'"
        )
    elif tamper == "uncertain":
        kernel.store.execute("UPDATE outbox SET status='uncertain'")
    elif tamper == "unknown":
        item.data["custom_id"] = "hrv:unknown:open"
    elif tamper == "missing_request":
        kernel.store.execute("DELETE FROM requests")
    target = "hortator" if tamper == "other_bot" else bot["id"]
    await kernel.connector.slash.receive(target, item)
    item.response.defer.assert_not_awaited()
    item.response.send_message.assert_awaited_once()
    assert item.response.send_message.await_args.kwargs["ephemeral"] is True
    assert "Saved final reasoning" not in str(item.response.send_message.await_args)


async def test_revocation_after_ack_and_during_read_is_rechecked(kernel, monkeypatch):
    bot = enable(kernel)
    request(kernel)
    row = await binding(kernel, bot)
    item = click(bot, f"hrv:{row['id']}:open")

    async def revoke(**kwargs):
        current = kernel.store.get("bots", "ada")
        current.pop("revision")
        current["enabled_plugins"] = []
        kernel.store.put("bots", current)

    item.response.defer.side_effect = revoke
    await kernel.connector.slash.receive(bot["id"], item)
    assert "disabled" in item.edit_original_response.await_args.kwargs["content"]
    assert "Saved final reasoning" not in str(item.edit_original_response.await_args)


async def test_download_has_complete_text_masks_credentials_and_never_replays_input(kernel):
    bot = enable(kernel)
    secret = "new-secret-added-after-capture"
    text = "A thought 🐱\n```<think>literal</think>```\n" + secret + "\n" + "Long thought.\n" * 400
    request(kernel, text=text)
    kernel.vault.put("plugin/fake/api_key", secret)
    row = await binding(kernel, bot)
    await open_view(kernel, bot, row)
    session = kernel.store.one("SELECT * FROM reasoning_views")
    item = click(bot, f"hrv:{session['id']}:download:0:0", message=PRIVATE, ephemeral=True)
    downloaded = []

    async def receive_file(**kwargs):
        downloaded.append(kwargs["attachments"][0].fp.read().decode())
        return SimpleNamespace(id=999)

    item.edit_original_response.side_effect = receive_file
    await kernel.connector.slash.receive(bot["id"], item)
    assert downloaded == [text.replace(secret, "[REDACTED]")]
    assert "DO NOT EXPORT REPLAY" not in downloaded[0]
    assert item.response.defer.await_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ingress", ["ordinary", "slash", "panel"])
async def test_answer_delivery_adds_only_button_preserving_footer_files_and_transcript(
    kernel, stream, ingress
):
    bot = (
        enable_slash(kernel)
        if ingress == "slash"
        else enable(kernel, footer_enabled=True, footer_template="Unchanged footer")
    )
    if ingress == "slash":
        bot.pop("revision")
        bot.update(
            enabled_plugins=["slash_commands", "reasoning_viewer"],
            footer_enabled=True,
            footer_template="Unchanged footer",
        )
        kernel.store.put("bots", bot)
        plugin = kernel.store.get("plugins", "reasoning_viewer")
        plugin.pop("revision")
        plugin["enabled"] = True
        kernel.store.put("plugins", plugin)
    ingest(kernel)
    visible = "Ordinary final answer " * 110
    body = {
        "choices": [
            {
                "message": {"content": visible, "reasoning_content": "PRIVATE ORIGINAL REASONING"},
                "finish_reason": "stop",
            }
        ]
    }
    if ingress == "panel":
        from test_discord_panels import enable as enable_panel, prepare

        bot = enable_panel(kernel, footer_enabled=True, footer_template="Unchanged footer")
        bot.pop("revision")
        bot["enabled_plugins"].append("reasoning_viewer")
        kernel.store.put("bots", bot)

        async def prep(request):
            turn = kernel.store.one("SELECT id FROM turns WHERE status='running'")
            await prepare(kernel, bot, turn=turn["id"])
            return respond(request)

    def respond(request):
        if not stream:
            return httpx.Response(200, json=body)
        from test_provider import Fragments

        packet = {"choices": [{"delta": body["choices"][0]["message"], "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Fragments(("data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n").encode()),
        )

    await install_client(kernel, prep if ingress == "panel" else respond)
    if ingress == "slash":
        item = interaction(bot)
        await kernel.connector.slash.receive(bot["id"], item)
        await settle(kernel)
        sent = item.edit_original_response.await_args.kwargs
        wire = (sent.get("content") or "") + json.dumps(sent["view"].to_components())
        assert len(sent["attachments"]) == 1
    else:
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=int(MESSAGE))))
        kernel.connector.clients["ada"] = SimpleNamespace(
            is_ready=lambda: True, get_channel=lambda _: channel
        )
        await kernel.engine.tick()
        await settle(kernel)
        sent = channel.send.await_args.kwargs
        wire = (channel.send.await_args.args[0] or "") + json.dumps(sent["view"].to_components())
        assert len(sent["files"]) == 1
        if ingress == "panel":
            assert isinstance(sent["view"], discord.ui.LayoutView)
    assert "REASONING" in wire and "Unchanged footer" in wire
    assert "PRIVATE ORIGINAL REASONING" not in wire
    row = kernel.store.one("SELECT * FROM reasoning_bindings")
    assert row["message_id"] == MESSAGE
    assert kernel.store.one("SELECT status FROM turns")["status"] == "sent"
    assert "PRIVATE ORIGINAL REASONING" not in str(kernel.store.rows("SELECT * FROM messages"))


def test_capture_prefers_full_text_excludes_encrypted_and_pages_unicode_losslessly():
    data = {
        "reasoning_content": "Native",
        "reasoning_details": [{"type": "reasoning.text", "text": "Mirror"}],
    }
    assert captured_text(json.dumps(data)) == "Native"
    data["reasoning_content"] = ""
    assert captured_text(json.dumps(data)) == "Mirror"
    assert captured_text(json.dumps({"inline_reasoning": [{"text": "Inline"}]})) == "Inline"
    raw = json.dumps({"reasoning_content": "🐱" * 10000})
    _, pages = page_capture(raw, 0)
    content = "".join(page_capture(raw, p)[0][4:-4] for p in range(pages))
    assert content == "🐱" * 10000


async def test_serialized_navigation_ids_are_unique_at_every_boundary():
    for rounds in (1, 2, 3):
        for pages in (1, 2, 3):
            for index in range(rounds):
                for page in range(pages):
                    payload = reader_view("view_test", index, page, rounds, pages).to_components()
                    ids = [c["custom_id"] for row in payload for c in row["components"]]
                    assert len(ids) == len(set(ids)) == 5
                    assert all(1 <= len(identity) <= 100 for identity in ids)


@pytest.mark.parametrize(
    "label,expected_round,expected_page",
    [("Previous round", 1, 1), ("Next round", 3, 1), ("Previous page", 2, 1), ("Next page", 2, 3)],
)
async def test_emitted_navigation_handles_read_the_expected_private_page(
    kernel, label, expected_round, expected_page
):
    bot = enable(kernel)
    for at, identity in enumerate(("req-early", "req-middle", "req-final"), 1):
        request(kernel, identity, text="Saved thought " * 400, at=at)
    row = await binding(kernel, bot)
    await open_view(kernel, bot, row)
    session = kernel.store.one("SELECT * FROM reasoning_views")
    middle = click(bot, f"hrv:{session['id']}:page:1:1", message=PRIVATE, ephemeral=True)
    await kernel.connector.slash.receive(bot["id"], middle)
    payload = middle.edit_original_response.await_args.kwargs["view"].to_components()
    control = next(c for row in payload for c in row["components"] if c["label"] == label)
    assert not control["disabled"]
    item = click(bot, control["custom_id"], message=PRIVATE, ephemeral=True)
    await kernel.connector.slash.receive(bot["id"], item)
    item.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    body = item.edit_original_response.await_args.kwargs
    assert f"Request {expected_round}/3" in body["content"]
    assert f"Page {expected_page}/" in body["content"]
    assert not kernel.store.rows("SELECT * FROM turns")


@pytest.mark.parametrize("failure", ["duplicate_ids", "connection"])
async def test_failed_view_replaces_thinking_with_plain_private_notice(kernel, failure):
    bot = enable(kernel)
    request(kernel)
    row = await binding(kernel, bot)
    item = click(bot, f"hrv:{row['id']}:open")
    error = (
        discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"),
            {"code": 50035, "message": "Invalid Form Body: Component custom id cannot be duplicated"},
        )
        if failure == "duplicate_ids"
        else OSError("connection reset")
    )
    item.edit_original_response.side_effect = [error, SimpleNamespace(id=int(PRIVATE))]
    await kernel.connector.slash.receive(bot["id"], item)
    item.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert item.edit_original_response.await_count == 2
    notice = item.edit_original_response.await_args.kwargs
    assert notice["view"] is None and notice["attachments"] == []
    assert not notice["allowed_mentions"].everyone
    assert "Saved reasoning is unchanged" in notice["content"]
    assert "Click REASONING again" in notice["content"]
    assert "Saved final reasoning" not in notice["content"]
    if failure == "duplicate_ids":
        assert "navigation buttons shared an ID" in notice["content"]
    event = json.loads(
        kernel.store.one("SELECT data FROM events WHERE kind='discord.reasoning_failed'")["data"]
    )
    assert event["phase"] == "edit_reasoning_response"
    assert event["reason"] == notice["content"]
    assert kernel.store.one("SELECT message_id FROM reasoning_views")["message_id"] is None
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_failed_private_notice_is_logged_without_retries_or_public_fallback(kernel):
    bot = enable(kernel)
    request(kernel)
    row = await binding(kernel, bot)
    item = click(bot, f"hrv:{row['id']}:open")
    item.followup = SimpleNamespace(send=AsyncMock())
    item.edit_original_response.side_effect = OSError("connection reset")
    await kernel.connector.slash.receive(bot["id"], item)
    assert item.edit_original_response.await_count == 2
    item.response.send_message.assert_not_awaited()
    item.followup.send.assert_not_awaited()
    event = kernel.store.one("SELECT bot_id,data FROM events WHERE kind='discord.reasoning_notice_failed'")
    assert event["bot_id"] == bot["id"]
    assert "dismiss the pending viewer" in json.loads(event["data"])["reason"]
    assert "Saved final reasoning" not in event["data"]


@pytest.mark.parametrize("receipt_ok", [True, False])
async def test_private_page_ack_recovery_requires_exact_saved_viewer(kernel, receipt_ok):
    bot = enable(kernel)
    request(kernel, text="Long thought. " * 400)
    row = await binding(kernel, bot)
    await open_view(kernel, bot, row)
    session = kernel.store.one("SELECT * FROM reasoning_views")
    item = click(bot, f"hrv:{session['id']}:page:0:1", message=PRIVATE, ephemeral=True)
    item.response.defer.side_effect = discord.InteractionResponded(item)
    item.original_response = AsyncMock(
        return_value=SimpleNamespace(
            id=int(PRIVATE) if receipt_ok else int(PRIVATE) + 1,
            flags=SimpleNamespace(ephemeral=True, loading=False),
            application_id=item.application_id,
            interaction_metadata=SimpleNamespace(id=123),
        )
    )
    await kernel.connector.slash.receive(bot["id"], item)
    assert item.edit_original_response.await_count == int(receipt_ok)
    assert not kernel.store.rows("SELECT * FROM turns")


async def test_open_ack_retries_transient_errors_then_reads_once(kernel):
    bot = enable(kernel)
    request(kernel)
    row = await binding(kernel, bot)
    item = click(bot, f"hrv:{row['id']}:open")
    item.response.defer.side_effect = [OSError("connection reset"), None]
    await kernel.connector.slash.receive(bot["id"], item)
    assert item.response.defer.await_count == 2 and item.edit_original_response.await_count == 1
    data = kernel.store.one("SELECT data FROM events WHERE kind='discord.reasoning_ack_retry'")
    assert json.loads(data["data"])["retry_in_seconds"] == ACK_RETRY_DELAY


@pytest.mark.parametrize(
    "tamper",
    [
        "public",
        "other_message",
        "invalid_page",
        "invalid_round",
        "disabled",
        "missing_capture",
        "over_file_limit",
    ],
)
async def test_private_navigation_failures_are_truthful_and_do_not_reveal(kernel, tamper):
    bot = enable(kernel)
    request(kernel)
    row = await binding(kernel, bot)
    await open_view(kernel, bot, row)
    session = kernel.store.one("SELECT * FROM reasoning_views")
    item = click(bot, f"hrv:{session['id']}:page:0:0", message=PRIVATE, ephemeral=True)
    if tamper == "public":
        item.message.flags.ephemeral = False
    elif tamper == "other_message":
        item.message.id += 1
    elif tamper == "invalid_page":
        item.data["custom_id"] = f"hrv:{session['id']}:page:0:999"
    elif tamper == "invalid_round":
        item.data["custom_id"] = f"hrv:{session['id']}:page:9:0"
    elif tamper == "disabled":
        bot = kernel.store.get("bots", "ada")
        bot.pop("revision")
        bot["enabled_plugins"] = []
        kernel.store.put("bots", bot)
    elif tamper == "missing_capture":
        kernel.store.execute("DELETE FROM request_diagnostics")
    elif tamper == "over_file_limit":
        item.data["custom_id"] = f"hrv:{session['id']}:download:0:0"
        item.filesize_limit = 1
    await kernel.connector.slash.receive(bot["id"], item)
    assert "Saved final reasoning" not in str(item.edit_original_response.await_args)
    assert "Saved final reasoning" not in str(item.response.send_message.await_args)
    if tamper == "missing_capture":
        assert (
            "No readable reasoning was recorded" in item.edit_original_response.await_args.kwargs["content"]
        )


async def test_revocation_during_page_preparation_and_cancellation_do_not_leak(kernel, monkeypatch):
    bot = enable(kernel)
    request(kernel)
    row = await binding(kernel, bot)
    item = click(bot, f"hrv:{row['id']}:open")
    original = asyncio.to_thread

    async def prepare_then_revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        current = kernel.store.get("bots", "ada")
        current.pop("revision")
        current["enabled_plugins"] = []
        kernel.store.put("bots", current)
        return result

    monkeypatch.setattr("hortator.reasoning_viewer.asyncio.to_thread", prepare_then_revoke)
    await kernel.connector.slash.receive(bot["id"], item)
    assert "disabled" in item.edit_original_response.await_args.kwargs["content"]
    enable(kernel)
    cancelled = click(bot, f"hrv:{row['id']}:open", iid="987654321012345679")
    cancelled.response.defer.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await kernel.connector.slash.receive(bot["id"], cancelled)
    cancelled.edit_original_response.assert_not_awaited()
