import hashlib
import io
from types import SimpleNamespace as NS

import httpx
import pytest
from PIL import Image

from conftest import configured
from hortator import vision
from hortator.console import OperationalConsole
from hortator.discord_gateway import CouncilClient
from test_addressing import message, CHANNEL
from test_vision import attachment


def jpeg(size=(80, 60), mode="RGB", progressive=False, orientation=1):
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    with Image.new(mode, size) as picture:
        picture.save(buffer, format="JPEG", progressive=progressive, exif=exif)
    return buffer.getvalue()


@pytest.mark.parametrize("mode,progressive", [("RGB", False), ("RGB", True), ("L", False), ("CMYK", False)])
def test_admitted_jpeg_bytes_and_resolution_are_preserved(mode, progressive):
    original = jpeg(mode=mode, progressive=progressive)
    output, details = vision.prepare_image(original)
    assert output == original
    assert (details["width"], details["height"]) == (80, 60)
    assert "warning" not in details and "transformation" not in details
    assert vision.MAX_PIXELS == 64_000_000


@pytest.mark.parametrize("mode,progressive", [("RGB", False), ("RGB", True), ("L", False), ("CMYK", False)])
def test_oversized_jpeg_keeps_proportions_and_records_actual_transformation(monkeypatch, mode, progressive):
    monkeypatch.setattr(vision, "MAX_PIXELS", 1200)
    original = jpeg(mode=mode, progressive=progressive)
    output, details = vision.prepare_image(original)
    assert output != original
    assert (details["width"], details["height"]) == (40, 30)
    assert details["transformation"]["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert details["transformation"]["original_size"] == len(original)
    assert details["transformation"]["jpeg_quality"] == 90
    assert "IMAGE RESIZED" in details["warning"]
    with Image.open(io.BytesIO(output)) as result:
        result.load()
        assert result.size == (40, 30) and result.mode == "RGB"


def test_resize_applies_exif_orientation(monkeypatch):
    monkeypatch.setattr(vision, "MAX_PIXELS", 1200)
    output, details = vision.prepare_image(jpeg(orientation=6))
    with Image.open(io.BytesIO(output)) as result:
        assert result.size == (30, 40)
        assert result.getexif().get(274, 1) == 1
    assert details["transformation"]["orientation_applied"] is True


def test_quality_reduction_never_shrinks_resolution_again(monkeypatch):
    monkeypatch.setattr(vision, "MAX_PIXELS", 1200)
    original_save = Image.Image.save
    qualities = []

    def save(self, fp, *args, **kwargs):
        quality = kwargs.get("quality")
        if quality is not None:
            qualities.append((self.size, quality))
        if quality == 90:
            fp.write(b"x" * (vision.MAX_IMAGE_BYTES + 1))
        else:
            original_save(self, fp, *args, **kwargs)

    original = jpeg()
    monkeypatch.setattr(Image.Image, "save", save)
    output, details = vision.prepare_image(original)
    assert qualities == [((40, 30), 90), ((40, 30), 85)]
    assert details["transformation"]["jpeg_quality"] == 85
    assert len(output) <= vision.MAX_IMAGE_BYTES


async def test_only_resized_copy_cached_and_warning_reaches_model_and_console(kernel, monkeypatch):
    monkeypatch.setattr(vision, "MAX_PIXELS", 1200)
    original = jpeg()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=original, headers={"content-type": "image/pjpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = vision.ImageCache(kernel.store, client)
        item = await cache.capture(
            attachment(filename="PHOTO.JPG", content_type="image/jpg", size=len(original))
        )
        info = item["vision"]
        assert info["status"] == "ready"
        assert not cache.path(hashlib.sha256(original).hexdigest()).exists()
        assert list(cache.root.iterdir()) == [cache.path(info["sha256"])]
        again = await cache.capture(item)
        assert again == item and len(calls) == 1
        parts = cache.content("describe", [{"discord_id": "1", "attachments": [item]}])
        assert "IMAGE RESIZED" in str(parts)
        wire = cache.wire_messages([{"role": "user", "content": parts}])
        assert wire[0]["content"][-1]["type"] == "image_url"
        stream = io.StringIO()
        with OperationalConsole(stream=stream, keys=False, color=False) as console:
            console.bind(kernel)
            cache.report_change(item, None, bot_id="ada", message_id="1")
            cache.report_change(again, item, bot_id="ada", message_id="1")
        assert "IMAGE RESIZED" in stream.getvalue()
        assert len(kernel.store.rows("SELECT * FROM events WHERE kind='attachment.image_resized'")) == 1


async def test_failed_capture_is_not_repeated_on_history_edit_or_new_cache(kernel, monkeypatch):
    monkeypatch.setattr(vision, "MAX_PIXELS", 1200)
    configured(kernel)
    source = io.BytesIO()
    with Image.new("RGB", (80, 60)) as picture:
        picture.save(source, format="PNG")
    raw = source.getvalue()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=raw, headers={"content-type": "image/png"})

    msg = message(555555555555555555)
    msg.attachments = [NS(**attachment(size=len(raw)))]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        kernel.connector.images = vision.ImageCache(kernel.store, client)
        await kernel.connector.receive("ada", msg)
        await kernel.connector.receive("ada", msg, historical=True)
        kernel.connector.images = vision.ImageCache(kernel.store, client)
        msg.attachments[0].url += "&refreshed=1"
        await kernel.connector.receive("ada", msg, historical=True)
        gateway = CouncilClient(kernel.connector, "ada")
        try:
            payload = NS(message_id=msg.id, data={"attachments": [vars(msg.attachments[0])]})
            await gateway.on_raw_message_edit(payload)
        finally:
            await gateway.close()
    assert len(calls) == 1
    assert len(kernel.store.rows("SELECT * FROM events WHERE kind='attachment.image_unavailable'")) == 1
    row = kernel.store.transcript(CHANNEL)[0]
    assert row["attachments"][0]["vision"]["observed_pixels"] == 4800
    assert "PIXELS UNAVAILABLE" in str(kernel.connector.images.content("look", [row]))


async def test_transport_failure_retries_only_after_refreshed_url(kernel):
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(403)
            if len(calls) == 1
            else httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = vision.ImageCache(kernel.store, client)
        item = await cache.capture(attachment())
        assert (await cache.capture(item))["vision"]["status"] == "unavailable"
        fresh = vision.reuse_vision({**attachment(), "url": attachment()["url"] + "&new=1"}, item)
        assert (await cache.capture(fresh))["vision"]["status"] == "ready"
    assert len(calls) == 2


async def test_old_twenty_megapixel_rejection_retries_once(kernel):
    original = attachment(
        vision={"status": "unavailable", "error": "Image exceeds the 20 megapixel decoded limit"}
    )
    kernel.store.ingest(
        discord_id="pixel-retry",
        channel_id="channel",
        author_id="owner",
        author_name="Owner",
        content="image",
        attachments=[original],
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = vision.ImageCache(kernel.store, client)
        await cache.prepare_rows(kernel.store.transcript("channel"))
        await cache.prepare_rows(kernel.store.transcript("channel"))
    assert len(calls) == 1


async def test_truncated_jpeg_never_creates_ready_cache(kernel, monkeypatch):
    monkeypatch.setattr(vision, "MAX_PIXELS", 1200)
    raw = jpeg()[:-60]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=raw, headers={"content-type": "image/jpeg"})
        )
    ) as client:
        cache = vision.ImageCache(kernel.store, client)
        result = await cache.capture(attachment(size=len(raw)))
    assert result["vision"]["status"] == "unavailable"
    assert not cache.root.exists()
