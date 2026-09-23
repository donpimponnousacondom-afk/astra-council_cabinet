"""Shared vision test helpers."""

import io

import httpx
from PIL import Image

from hortator.vision import (
    ImageCache,
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
