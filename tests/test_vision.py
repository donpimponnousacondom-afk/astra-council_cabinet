import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from conftest import configured
from test_provider import install_client
from test_typing import prepare_bot
from hortator.models import OWNER_ID, ControlError
from hortator.vision import ImageCache, MAX_IMAGES, trusted_discord_url, image_count

URL = "https://cdn.discordapp.com/attachments/123/456/image.png?ex=expired-soon"


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def attachment(**changes):
    return {
        "id": "456",
        "filename": "image.png",
        "url": URL,
        "size": len(png()),
        "content_type": "image/png",
        **changes,
    }


async def cached(kernel):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=png(), headers={"content-type": "image/png"})
        )
    )
    cache = ImageCache(kernel.store, client)
    result = await cache.capture(attachment())
    await client.aclose()
    assert result["vision"]["status"] == "ready"
    return cache, result


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/1/2/a.png",
        "https://cdn.discordapp.com.evil.test/attachments/1/2/a.png",
        "https://127.0.0.1/attachments/1/2/a.png",
        "https://user:password@cdn.discordapp.com/attachments/1/2/a.png",
        "https://cdn.discordapp.com:444/attachments/1/2/a.png",
        "https://cdn.discordapp.com/elsewhere/image.png",
        "https://cdn.discordapp.com/attachments/1/2/a.png#fragment",
    ],
)
def test_image_urls_are_confined_to_discord_attachment_cdn(url):
    assert not trusted_discord_url(url)


async def test_capture_persists_validated_pixels_surviving_signed_url_expiry(kernel):
    cache, item = await cached(kernel)
    assert cache.read(item["vision"]) == png()
    assert cache.path(item["vision"]["sha256"]).stat().st_mode & 0o777 == 0o600
    assert cache.root.stat().st_mode & 0o777 == 0o700
    item["url"] = URL + "-expired"
    # A new cache instance needs no network access, including after process restart.
    restored = await ImageCache(kernel.store).capture(item)
    assert restored["vision"] == item["vision"]
    rows = [{"discord_id": "123", "attachments": [restored]}]
    messages = [{"role": "user", "content": cache.content("look at this", rows)}]
    wire = cache.wire_messages(messages)
    assert (
        wire[0]["content"][-1]["image_url"]["url"]
        == "data:image/png;base64," + base64.b64encode(png()).decode()
    )
    assert messages[0]["content"][-1]["type"] == "hortator_image"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://127.0.0.1/private"}),
        httpx.Response(200, content=b"<script>bad</script>", headers={"content-type": "image/png"}),
        httpx.Response(200, content=png(), headers={"content-type": "text/html"}),
        httpx.Response(
            200, content=png(), headers={"content-type": "image/png", "content-length": str(9 * 1024 * 1024)}
        ),
    ],
)
async def test_invalid_image_or_redirect_becomes_explicit_unavailable_metadata(kernel, response):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        item = await ImageCache(kernel.store, client).capture(attachment())
    assert len(requests) == 1
    assert item["vision"]["status"] == "unavailable"
    content = ImageCache(kernel.store).content("image", [{"discord_id": "1", "attachments": [item]}])
    assert "PIXELS UNAVAILABLE" in content[-1]["text"]
    assert all(part["type"] == "text" for part in content)


async def test_missing_corrupt_and_oversized_request_images_fail_without_silent_drop(kernel):
    cache, item = await cached(kernel)
    rows = [{"discord_id": "1", "attachments": [item] * (MAX_IMAGES + 1)}]
    with pytest.raises(ControlError, match="8 images"):
        cache.wire_messages([{"role": "user", "content": cache.content("test", rows)}])
    cache.path(item["vision"]["sha256"]).write_bytes(b"corrupt")
    with pytest.raises(ControlError, match="unavailable"):
        cache.wire_messages([{"role": "user", "content": cache.content("test", rows[:1])}])


@pytest.mark.parametrize("bot_id", ["ada", "socrates", "dirac", "curie", "hortator"])
async def test_every_bot_sends_pixels_and_ledger_keeps_only_references(kernel, bot_id):
    bot, channel = prepare_bot(kernel, bot_id)
    cache, item = await cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?", (json.dumps([item]), channel)
    )
    rows = kernel.store.transcript(channel)
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    profile["stream"] = False
    messages, meta = kernel.engine.contexts.assemble(bot, profile, channel, rows, "")
    assert meta["image_count"] == 1
    assert "4096 reserve/image" in meta["estimator"]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "red pixels"}, "finish_reason": "stop"}]}
        )

    await install_client(kernel, handler)
    result = await kernel.pool.complete(
        bot=bot, profile=profile, messages=messages, tools=[], turn_id="vision", context=meta
    )
    assert any(
        part.get("type") == "image_url"
        for message in seen[0]["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
    )
    recorded = kernel.store.one("SELECT body,context FROM requests WHERE id=?", (result.request_id,))
    assert item["vision"]["sha256"] in recorded["body"]
    assert "base64," not in json.dumps(recorded)
    assert "hortator_image" in recorded["body"]


async def test_compaction_receives_pixels_and_deleted_images_are_omitted(kernel):
    bot, channel = prepare_bot(kernel)
    _, item = await cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?", (json.dumps([item]), channel)
    )
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    received = []

    async def complete(**kwargs):
        received.append(kwargs)
        return SimpleNamespace(content="An image of red pixels was posted.", finish_reason="stop")

    kernel.pool.complete = complete
    await kernel.engine.contexts.prepare(bot, profile, channel, "compact-image", [], force=True)
    assert received[0]["purpose"] == "compaction"
    assert image_count(received[0]["messages"]) == 1
    assert "base64," not in json.dumps(received)
    kernel.store.execute("UPDATE messages SET deleted=1 WHERE channel_id=?", (channel,))
    rows = kernel.store.transcript(channel)
    messages, _ = kernel.engine.contexts.assemble(bot, profile, channel, rows, "")
    assert image_count(messages) == 0
    assert "sha256" not in json.dumps(messages)


async def test_gateway_only_downloads_images_after_owner_authorization(kernel):
    bot = configured(kernel, "hortator")
    kernel.connector.images.capture = AsyncMock(side_effect=lambda item: item)
    channel = SimpleNamespace(id=888888888888888888, parent_id=None)
    message = SimpleNamespace(
        id=555555555555555555,
        channel=channel,
        guild=None,
        content="look",
        attachments=[SimpleNamespace(**attachment())],
        author=SimpleNamespace(id=1234, bot=False, display_name="outsider"),
        webhook_id=None,
        reference=None,
        nonce=None,
        created_at=SimpleNamespace(timestamp=lambda: 1000),
    )
    await kernel.connector.receive(bot["id"], message)
    kernel.connector.images.capture.assert_not_awaited()
    message.author.id = int(OWNER_ID)
    await kernel.connector.receive(bot["id"], message)
    kernel.connector.images.capture.assert_awaited_once()


async def test_unsupported_upstream_vision_error_is_visible_not_retried_as_text(kernel):
    from hortator.provider import ProviderError

    bot, channel = prepare_bot(kernel)
    cache, item = await cached(kernel)
    messages = [
        {"role": "user", "content": cache.content("look", [{"discord_id": "1", "attachments": [item]}])}
    ]
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "This model does not support image inputs"}})

    await install_client(kernel, handler)
    with pytest.raises(ProviderError, match="does not support image"):
        await kernel.pool.complete(
            bot=bot,
            profile=kernel.store.get("profiles", bot["model_profile_id"]),
            messages=messages,
            tools=[],
            turn_id="unsupported-vision",
            context={},
        )
    assert len(calls) == 1
    assert kernel.store.one("SELECT status FROM requests")["status"] == "failed"
    assert kernel.store.health("openrouter")["consecutive_failures"] == 0


async def test_legacy_capture_progress_is_persisted_before_cancellation(kernel):
    import asyncio

    bot, channel = prepare_bot(kernel)
    cache, item = await cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?",
        (json.dumps([attachment(), attachment(id="457")]), channel),
    )
    calls = 0

    async def capture(metadata):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return item

    cache.capture = capture
    with pytest.raises(asyncio.CancelledError):
        await cache.prepare_rows(kernel.store.transcript(channel))
    stored = kernel.store.transcript(channel)[0]["attachments"]
    assert stored[0]["vision"]["status"] == "ready"
    assert "vision" not in stored[1]


async def test_many_images_compact_in_bounded_batches_instead_of_dropping_pixels(kernel):
    bot, channel = prepare_bot(kernel)
    _, item = await cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?", (json.dumps([item]), channel)
    )
    for index in range(8):
        kernel.store.ingest(
            discord_id=str(100 + index),
            channel_id=channel,
            room_id="council",
            author_id=OWNER_ID,
            author_name="The Boss",
            content="Another image",
            attachments=[item],
        )
    profile = kernel.store.get("profiles", bot["model_profile_id"])
    # Image count alone must trigger compaction even when token reserve fits a very large context.
    profile.update(context_window=1_000_000, keep_recent_messages=20)
    received = []

    async def complete(**kwargs):
        received.append(kwargs)
        return SimpleNamespace(content="Red pixels were posted repeatedly.", finish_reason="stop")

    kernel.pool.complete = complete
    rows, summary, _, _ = await kernel.engine.contexts.prepare(bot, profile, channel, "many-images", [])
    messages, _ = kernel.engine.contexts.assemble(bot, profile, channel, rows, summary)
    assert received and all(image_count(request["messages"]) <= MAX_IMAGES for request in received)
    assert image_count(messages) <= MAX_IMAGES
    assert sum(image_count(request["messages"]) for request in received) + image_count(messages) == 9


async def test_attachment_removal_edit_removes_pixels_but_embed_edit_keeps_them(kernel):
    from hortator.discord_gateway import CouncilClient

    bot, channel = prepare_bot(kernel)
    _, item = await cached(kernel)
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?", (json.dumps([item]), channel)
    )
    row = kernel.store.transcript(channel)[0]
    client = CouncilClient(kernel.connector, bot["id"])
    try:
        await client.on_raw_message_edit(SimpleNamespace(message_id=row["discord_id"], data={"embeds": []}))
        assert kernel.store.transcript(channel)[0]["attachments"][0]["vision"]["status"] == "ready"
        await client.on_raw_message_edit(
            SimpleNamespace(message_id=row["discord_id"], data={"attachments": []})
        )
        rows = kernel.store.transcript(channel)
        assert rows[0]["attachments"] == []
        messages, _ = kernel.engine.contexts.assemble(
            bot, kernel.store.get("profiles", bot["model_profile_id"]), channel, rows, ""
        )
        assert image_count(messages) == 0
    finally:
        await client.close()


async def test_image_capture_failure_before_atomic_replace_leaves_no_ready_reference(kernel, monkeypatch):
    import hortator.vision as module

    def fail_replace(*args):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=png(), headers={"content-type": "image/png"})
        )
    ) as client:
        cache = ImageCache(kernel.store, client)
        result = await cache.capture(attachment())
    assert result["vision"]["status"] == "unavailable"
    assert list(cache.root.iterdir()) == []
