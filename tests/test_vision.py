import base64
import io
import json
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from conftest import configured
from test_provider import install_client
from test_typing import prepare_bot
from hortator.models import OWNER_ID, ControlError
from hortator.provider import Completion
from hortator.vision import (
    ImageCache,
    MAX_IMAGE_BYTES,
    MAX_REQUEST_BYTES,
    MAX_IMAGES,
    trusted_discord_url,
    image_count,
    image_limits_exceeded,
    validate_image,
)

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
            200,
            content=png(),
            headers={"content-type": "image/png", "content-length": str(MAX_IMAGE_BYTES + 1)},
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
    messages, meta = await kernel.engine.contexts.assemble(bot, profile, channel, rows, "")
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
        return Completion(
            request_id="compact-image", content="An image of red pixels was posted.", finish_reason="stop"
        )

    kernel.pool.complete = complete
    await kernel.engine.contexts.prepare(bot, profile, channel, "compact-image", [], force=True)
    assert received[0]["purpose"] == "compaction"
    assert image_count(received[0]["messages"]) == 1
    assert "base64," not in json.dumps(received)
    kernel.store.execute("UPDATE messages SET deleted=1 WHERE channel_id=?", (channel,))
    rows = kernel.store.transcript(channel)
    messages, _ = await kernel.engine.contexts.assemble(bot, profile, channel, rows, "")
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


@pytest.mark.parametrize("image_limit", [8, 256])
async def test_many_images_compact_in_bounded_batches_instead_of_dropping_pixels(kernel, image_limit):
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
    profile.update(
        context_window=1_000_000,
        keep_recent_messages=20,
        max_request_images=image_limit,
        max_request_image_mib=512,
    )
    received = []

    async def complete(**kwargs):
        received.append(kwargs)
        return Completion(
            request_id=f"compact-images-{len(received)}",
            content="Red pixels were posted repeatedly.",
            finish_reason="stop",
        )

    kernel.pool.complete = complete
    rows, summary, _, _ = await kernel.engine.contexts.prepare(bot, profile, channel, "many-images", [])
    messages, _ = await kernel.engine.contexts.assemble(bot, profile, channel, rows, summary)
    if image_limit == 256:
        assert not received
        assert image_count(messages) == 9
        wire = ImageCache(kernel.store).wire_messages(messages, profile)
        assert (
            sum(
                p.get("type") == "image_url"
                for m in wire
                if isinstance(m.get("content"), list)
                for p in m["content"]
            )
            == 9
        )
        return
    event = next(e for e in kernel.store.events() if e["kind"] == "compaction.started")
    assert event["data"]["trigger"] == "image_count"
    assert event["data"]["image_count"] == 9
    assert event["data"]["image_limit"] == 8
    assert event["data"]["calibrated_tokens"] < event["data"]["token_threshold"]
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
        messages, _ = await kernel.engine.contexts.assemble(
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


def png_with_file_size(size):
    # A valid ancillary padding chunk exercises encoded-byte limits independently
    # of decoded dimensions and compression ratios, with exact byte boundaries.
    original = png()
    payload = b"p" * (size - len(original) - 12)
    kind = b"npAD"
    chunk = struct.pack(">I", len(payload)) + kind + payload
    chunk += struct.pack(">I", zlib.crc32(kind + payload))
    return original[:-12] + chunk + original[-12:]


@pytest.mark.parametrize("size", [10_583_216, 20 * 1024 * 1024])
async def test_large_image_reaches_provider_wire_without_resizing(kernel, size):
    data = png_with_file_size(size)
    assert len(data) == size
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=data, headers={"content-type": "image/png"})
        )
    ) as client:
        cache = ImageCache(kernel.store, client)
        item = await cache.capture(attachment(size=size))
    assert item["vision"]["status"] == "ready"
    assert item["vision"]["size"] == size
    messages = [
        {"role": "user", "content": cache.content("look", [{"discord_id": "1", "attachments": [item]}])}
    ]
    assert not image_limits_exceeded(messages)
    wire = cache.wire_messages(messages)
    assert base64.b64decode(wire[0]["content"][-1]["image_url"]["url"].split(",", 1)[1]) == data


@pytest.mark.parametrize("gate", ["metadata", "header", "stream", "validation"])
async def test_twenty_mib_ceiling_is_enforced_at_every_ingestion_gate(kernel, gate):
    if gate == "validation":
        with pytest.raises(ValueError, match="20 MiB"):
            validate_image(b"x" * (MAX_IMAGE_BYTES + 1))
        return
    called = []

    class Oversize(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * MAX_IMAGE_BYTES
            yield b"x"

    def handler(request):
        called.append(request)
        headers = {"content-type": "image/png"}
        if gate == "header":
            headers["content-length"] = str(MAX_IMAGE_BYTES + 1)
        return httpx.Response(200, headers=headers, stream=Oversize())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        item = await ImageCache(kernel.store, client).capture(
            attachment(size=MAX_IMAGE_BYTES + 1 if gate == "metadata" else 1)
        )
    assert len(called) == (0 if gate == "metadata" else 1)
    assert item["vision"] == {
        "status": "unavailable",
        "error": "Image exceeds the 20 MiB image limit",
        "size_limit_bytes": MAX_IMAGE_BYTES,
    }


async def test_combined_byte_budget_counts_original_bytes_and_stays_bounded(kernel):
    cache, item = await cached(kernel)
    data = png_with_file_size(MAX_IMAGE_BYTES)
    # Use valid cached bytes and matching durable metadata for exact wire-boundary testing.
    import hashlib

    item["vision"].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    cache.path(item["vision"]["sha256"]).write_bytes(data)
    rows = [{"discord_id": "1", "attachments": [item, item]}]
    messages = [{"role": "user", "content": cache.content("two large images", rows)}]
    assert MAX_REQUEST_BYTES == 40 * 1024 * 1024
    assert not image_limits_exceeded(messages)
    assert image_count(messages) == 2
    assert (
        len([part for part in cache.wire_messages(messages)[0]["content"] if part["type"] == "image_url"])
        == 2
    )
    rows[0]["attachments"].append(item)
    messages = [{"role": "user", "content": cache.content("too large together", rows)}]
    assert image_limits_exceeded(messages)
    with pytest.raises(ControlError, match="40 MiB"):
        cache.wire_messages(messages)


@pytest.mark.parametrize("status", [200, 403])
async def test_previous_eight_mib_rejections_retry_once_under_new_limit(kernel, status):
    _, channel = prepare_bot(kernel)
    legacy = attachment(
        size=10_583_216, vision={"status": "unavailable", "error": "Image exceeds the 8 MiB image limit"}
    )
    kernel.store.execute(
        "UPDATE messages SET attachments=? WHERE channel_id=?", (json.dumps([legacy]), channel)
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=png(), headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = ImageCache(kernel.store, client)
        await cache.prepare_rows(kernel.store.transcript(channel))
        await cache.prepare_rows(kernel.store.transcript(channel))
    assert len(calls) == 1
    stored = kernel.store.transcript(channel)[0]["attachments"][0]["vision"]
    assert stored["status"] == ("ready" if status == 200 else "unavailable")
    assert "8 MiB" not in stored.get("error", "")


async def test_current_rejections_and_deleted_images_are_not_retried(kernel):
    cache = ImageCache(kernel.store)
    cache.capture = AsyncMock()
    rows = [
        {
            "discord_id": "1",
            "deleted": True,
            "attachments": [
                attachment(vision={"status": "unavailable", "error": "Image exceeds the 8 MiB image limit"})
            ],
        },
        {
            "discord_id": "2",
            "attachments": [
                attachment(
                    size=MAX_IMAGE_BYTES + 1,
                    vision={
                        "status": "unavailable",
                        "error": "Image exceeds the 20 MiB image limit",
                        "size_limit_bytes": MAX_IMAGE_BYTES,
                    },
                )
            ],
        },
    ]
    await cache.prepare_rows(rows)
    cache.capture.assert_not_awaited()


async def test_custom_image_byte_budget_and_compaction_batch_share_profile(kernel):
    cache, item = await cached(kernel)
    rows = [{"discord_id": str(i), "attachments": [item]} for i in range(9)]
    ctx = kernel.engine.contexts
    count, _, _, _ = ctx.compaction_batch(
        [], rows, [], 1_000_000, {"max_request_images": 256, "max_request_image_mib": 512}
    )
    assert count == 9
    count, _, _, _ = ctx.compaction_batch([], rows, [], 1_000_000)
    assert count == 8
    messages = [
        {"role": "user", "content": [{"type": "hortator_image", "image": {"size": 41 * 1024 * 1024}}]}
    ]
    assert image_limits_exceeded(messages)
    assert not image_limits_exceeded(messages, {"max_request_images": 256, "max_request_image_mib": 512})
    public = kernel.service.public("profiles", {"id": "old"})
    assert public["max_request_images"] == 8
    assert public["max_request_image_mib"] == 40
