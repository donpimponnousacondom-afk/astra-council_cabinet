import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import configured
from hortator.discord_gateway import CouncilClient
from hortator.models import Bot, ControlError, OWNER_ID
from hortator.vision import ImageCache, image_count
from test_provider import install_client
from test_typing import prepare_bot
from test_vision import attachment, cached


async def test_bot_image_defaults_and_api_save_leave_shared_profile_unchanged(kernel, owner):
    bot = configured(kernel)
    bot.pop("allow_images", None)
    kernel.store.put("bots", bot)
    bot = kernel.store.get("bots", "ada")
    assert Bot.model_validate({k: v for k, v in bot.items() if k != "revision"}).allow_images is True
    assert kernel.service.public("bots", bot)["allow_images"] is True
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    other = kernel.store.get("bots", "hortator")
    saved = await kernel.service.save(owner, "bots", "ada", {"allow_images": False})
    assert saved["allow_images"] is False
    saved = await kernel.service.save(owner, "bots", "ada", {"persona": "Text observer"})
    assert saved["allow_images"] is False
    assert kernel.store.get("profiles", profile["id"]) == profile
    assert kernel.store.get("bots", "hortator") == other
    saved = await kernel.service.save(owner, "bots", "ada", {"allow_images": True})
    assert saved["allow_images"] is True


async def test_same_profile_bot_can_disable_cached_pixels_across_tool_rounds(kernel):
    bot, channel = prepare_bot(kernel)
    other = configured(kernel, "socrates", allow_images=False)
    kernel.store.context(other["id"], channel)
    _, item = await cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?", (json.dumps([item]), channel)
    )
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    profile.update(stream=False, context_window=100_000)
    ctx = kernel.engine.contexts
    ctx.images.prepare_rows = AsyncMock(side_effect=AssertionError("Disabled bot must not prepare pixels"))
    rows, summary, _, meta = await ctx.prepare(other, profile, channel, "disabled-images", [])
    assert meta["image_count"] == 0
    assert meta["image_plan"]["disabled_images"] == 1
    assert meta["image_plan"]["policy"] == "bot_images_disabled"
    ctx.images.prepare_rows.assert_not_awaited()
    enabled_messages, enabled_meta = await ctx.assemble(bot, profile, channel, rows, summary)
    assert image_count(enabled_messages) == 1
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Text"}, "finish_reason": "stop"}]}
        )

    await install_client(kernel, respond)
    await kernel.pool.complete(
        bot=bot,
        profile=profile,
        messages=enabled_messages,
        tools=[],
        turn_id="image-on",
        context=enabled_meta,
    )
    for round_index in (0, 1):
        # Even an older enabled plan cannot override the disabled bot's setting.
        messages, metadata = await ctx.assemble(
            other,
            profile,
            channel,
            rows,
            summary,
            round_index=round_index,
            image_plan=enabled_meta["image_plan"],
        )
        assert image_count(messages) == 0
        assert "Image inputs are disabled for this bot" in messages[0]["content"]
        assert '"pixels_in_this_request":false' in json.dumps(messages).replace('\\"', '"')
        await kernel.pool.complete(
            bot=other,
            profile=profile,
            messages=messages,
            tools=[],
            turn_id=f"image-off-{round_index}",
            context=metadata,
        )
    assert '"type": "image_url"' in json.dumps(seen[0])
    assert '"type": "image_url"' not in json.dumps(seen[1:])
    assert kernel.store.transcript(channel)[0]["attachments"][0]["vision"]["status"] == "ready"


async def test_disabled_bot_skips_gateway_capture_for_new_and_edited_attachments(kernel):
    bot = configured(kernel, "hortator", allow_images=False)
    kernel.connector.images.capture = AsyncMock(side_effect=AssertionError("No downloads for disabled bot"))
    message = SimpleNamespace(
        id=555555555555555555,
        channel=SimpleNamespace(id=888888888888888888, parent_id=None),
        guild=None,
        content="look",
        attachments=[SimpleNamespace(**attachment())],
        author=SimpleNamespace(id=int(OWNER_ID), bot=False, display_name="The Boss"),
        webhook_id=None,
        reference=None,
        nonce=None,
        created_at=SimpleNamespace(timestamp=lambda: 1000),
    )
    await kernel.connector.receive(bot["id"], message)
    client = CouncilClient(kernel.connector, bot["id"])
    try:
        await client.on_raw_message_edit(
            SimpleNamespace(message_id=message.id, data={"attachments": [attachment(id="789")]})
        )
        row = kernel.store.transcript(str(message.channel.id))[0]
        assert row["attachments"][0]["id"] == "789"
        assert "vision" not in row["attachments"][0]
        kernel.connector.images.capture.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.parametrize(
    "part",
    [
        {"type": "hortator_image", "image": {"sha256": "not-read"}},
        {"type": "image_url", "image_url": {"url": "https://example.org/picture.png"}},
        {"type": "input_image", "image_url": "https://example.org/picture.png"},
    ],
)
async def test_disabled_image_wire_guard_rejects_pixels_before_file_or_network_access(kernel, part):
    with pytest.raises(ControlError, match="Image inputs are disabled for this bot"):
        ImageCache(kernel.store).wire_messages(
            [{"role": "user", "content": [{"type": "text", "text": "Keep this text"}, part]}],
            allow_images=False,
        )
