"""Bounded Discord image capture; durable references are expanded only on the provider wire."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import math
import os
import secrets
import re
import warnings
from urllib.parse import urlsplit

import httpx
from PIL import Image, JpegImagePlugin, UnidentifiedImageError

from .models import ControlError
from .store import dumps

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * MAX_IMAGE_BYTES
MAX_IMAGES = 10  # Default per-request budget; profiles can override it.
MAX_CAPTURE_IMAGES = 10  # Intake bound per Discord message, independent of the profile.
MAX_PIXELS = 64_000_000
IMAGE_TOKEN_RESERVE = 4096  # Deliberately approximate: providers tokenize pixels differently.
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class ImageSizeLimit(ValueError):
    def __init__(self):
        super().__init__(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB image limit")


class ImagePixelLimit(ValueError):
    def __init__(self, width, height):
        self.width, self.height = width, height
        super().__init__(
            f"Image is {width} × {height} ({width * height:,} pixels); "
            f"decoded limit is {MAX_PIXELS:,} pixels ({MAX_PIXELS // 1_000_000} MP)"
        )


def retry_previous_size_limit(attachment):
    vision = attachment.get("vision") or {}
    if vision.get("status") != "unavailable":
        return False
    old_pixels = vision.get("pixel_limit")
    if old_pixels is None and vision.get("error") == "Image exceeds the 20 megapixel decoded limit":
        old_pixels = 20_000_000
    if isinstance(old_pixels, int) and old_pixels < MAX_PIXELS:
        return True
    old_limit = vision.get("size_limit_bytes")
    # Metadata from the original 8 MiB implementation predates structured failure limits.
    if old_limit is None and vision.get("error") in (
        "Image exceeds the 8 MiB image limit",
        "Image is empty or exceeds the 8 MiB image limit",
    ):
        old_limit = 8 * 1024 * 1024
    size = attachment.get("size")
    return (
        isinstance(old_limit, int)
        and old_limit < MAX_IMAGE_BYTES
        and isinstance(size, (int, float))
        and 0 < size <= MAX_IMAGE_BYTES
    )


def trusted_discord_url(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"cdn.discordapp.com", "media.discordapp.net"}
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and bool(re.fullmatch(r"/attachments/[0-9]+/[0-9]+/[^/]+", parsed.path))
        )
    except ValueError, TypeError:
        return False


def image_candidate(attachment):
    return str(attachment.get("content_type") or "").startswith("image/") or str(
        attachment.get("filename", "")
    ).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".bmp"))


def validate_image(data):
    if not data:
        raise ValueError("Image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageSizeLimit()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format not in FORMATS:
                    raise ValueError("Vision supports PNG, JPEG, WebP and GIF image attachments")
                width, height = picture.size
                if width * height > MAX_PIXELS:
                    raise ImagePixelLimit(width, height)
                mime = FORMATS[picture.format]
                picture.verify()
            # Verify encoded pixel data as well as container structure. Never decode animation frames.
            with Image.open(io.BytesIO(data)) as picture:
                picture.load()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Image bytes are invalid, truncated, or exceed the decoded image limit") from exc
    return {"content_type": mime, "width": width, "height": height, "size": len(data)}


def prepare_image(data):
    """Preserve admitted images; resize oversized JPEGs before caching only the replacement."""
    if not data or len(data) > MAX_IMAGE_BYTES or not data.startswith(b"\xff\xd8\xff"):
        return data, validate_image(data)
    try:
        # Open the JPEG header directly, then bound the actual decoder dimensions
        # after JPEG's native 1/2, 1/4 or 1/8 subsampling. Never disable Pillow's
        # process-wide bomb guard. Check the intermediate raster before loading it.
        with JpegImagePlugin.JpegImageFile(io.BytesIO(data)) as picture:
            width, height = picture.size
            if width * height <= MAX_PIXELS:
                return data, validate_image(data)
            ratio = math.sqrt(MAX_PIXELS / (width * height))
            target = max(1, int(width * ratio)), max(1, int(height * ratio))
            orientation = picture.getexif().get(274, 1)
            picture.draft("RGB", target)
            decode_pixels = picture.width * picture.height
            if decode_pixels > 4 * MAX_PIXELS:
                raise ValueError(
                    f"JPEG decoder needs {decode_pixels:,} intermediate pixels; "
                    f"local resize working limit is {4 * MAX_PIXELS:,} pixels"
                )
            picture.load()
            with picture.resize(target, Image.Resampling.LANCZOS) as resized:
                converted = resized.convert("RGB")
            picture.close()
            try:
                transforms = {
                    2: Image.Transpose.FLIP_LEFT_RIGHT,
                    3: Image.Transpose.ROTATE_180,
                    4: Image.Transpose.FLIP_TOP_BOTTOM,
                    5: Image.Transpose.TRANSPOSE,
                    6: Image.Transpose.ROTATE_270,
                    7: Image.Transpose.TRANSVERSE,
                    8: Image.Transpose.ROTATE_90,
                }
                if orientation in transforms:
                    rotated = converted.transpose(transforms[orientation])
                    converted.close()
                    converted = rotated
                # Quality controls encoded bytes only; do not shrink dimensions again.
                for quality in (90, 85, 80, 70, 60, 50, 40, 30, 20, 10):
                    output = io.BytesIO()
                    converted.save(output, format="JPEG", quality=quality)
                    if output.tell() <= MAX_IMAGE_BYTES:
                        encoded = output.getvalue()
                        final_width, final_height = converted.size
                        return encoded, {
                            "content_type": "image/jpeg",
                            "width": final_width,
                            "height": final_height,
                            "size": len(encoded),
                            "transformation": {
                                "operation": "resize",
                                "original_width": width,
                                "original_height": height,
                                "original_size": len(data),
                                "original_sha256": hashlib.sha256(data).hexdigest(),
                                "pixel_limit": MAX_PIXELS,
                                "jpeg_quality": quality,
                                "orientation_applied": orientation in transforms,
                            },
                            "warning": (
                                f"IMAGE RESIZED: original {width} × {height} ({width * height:,} pixels) "
                                f"exceeded the {MAX_PIXELS:,}-pixel limit. You receive {final_width} × "
                                f"{final_height} JPEG pixels at quality {quality}; proportions are preserved "
                                "but fine detail may be lost. Only this resized copy is retained locally. "
                                "Disclose this limitation when answering about the image."
                            ),
                        }
                raise ValueError(
                    "Resized JPEG could not fit the 20 MiB file limit at the bounded quality settings"
                )
            finally:
                converted.close()
    except (OSError, SyntaxError) as exc:
        raise ValueError("JPEG bytes are invalid or truncated; no resized image was retained") from exc


def reuse_vision(attachment, previous):
    """Reuse durable outcomes across history/edits, but not a replaced attachment."""
    if not previous or any(
        attachment.get(key) != previous.get(key) for key in ("id", "filename", "size", "content_type")
    ):
        return attachment
    try:
        old, new = urlsplit(previous.get("url", "")), urlsplit(attachment.get("url", ""))
        if (old.hostname, old.path) != (new.hostname, new.path):
            return attachment
    except ValueError:
        return attachment
    vision = previous.get("vision")
    if not vision:
        return attachment
    # Only transport failures can benefit from a refreshed signed URL. Permanent
    # format/size failures do not become new work on every reconnect.
    transient = vision.get("retry_on_refreshed_url") or vision.get("error", "").startswith(
        ("Discord image download returned HTTP", "Image capture failed")
    )
    if transient and previous.get("url") != attachment.get("url"):
        return attachment
    return {**attachment, "vision": copy.deepcopy(vision)}


class ImageCache:
    def __init__(self, store, client=None):
        self.store, self.client = store, client
        self.root = store.path.parent / "images"

    def report_change(self, attachment, previous, *, bot_id=None, message_id=None):
        vision, old = attachment.get("vision", {}), (previous or {}).get("vision", {})
        notice = vision.get("warning") or vision.get("error")
        if not notice or notice == (old.get("warning") or old.get("error")):
            return
        self.store.emit(
            "attachment.image_resized" if vision.get("warning") else "attachment.image_unavailable",
            {
                "message_id": message_id,
                "attachment_id": attachment.get("id"),
                "reason" if vision.get("warning") else "error": notice,
                **{
                    k: vision[k]
                    for k in ("width", "height", "size", "pixel_limit", "observed_pixels", "transformation")
                    if k in vision
                },
            },
            bot_id=bot_id,
            level="warning",
        )

    def path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid image reference")
        return self.root / digest

    def read(self, vision):
        path = self.path(vision.get("sha256"))
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Cached image is missing or exceeds the image limit")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != vision["sha256"]:
            raise ValueError("Cached image integrity check failed")
        return data

    async def capture(self, attachment):
        result = dict(attachment)
        if not image_candidate(result):
            return result
        try:
            prior = result.get("vision") or {}
            if prior.get("status") == "unavailable" and not retry_previous_size_limit(result):
                return result
            if prior.get("status") == "ready":
                try:
                    self.read(prior)
                    return result
                except ValueError, OSError:
                    pass  # A refreshed Discord URL can repair missing cached bytes.
            url = result.get("url")
            if not trusted_discord_url(url):
                raise ValueError("Image URL is not a trusted Discord attachment CDN URL")
            declared_size = result.get("size")
            if isinstance(declared_size, (int, float)) and declared_size > MAX_IMAGE_BYTES:
                raise ImageSizeLimit()

            async def fetch(client):
                async with asyncio.timeout(15):
                    async with client.stream("GET", url, follow_redirects=False, timeout=15) as response:
                        if response.status_code != 200:
                            raise ValueError(
                                f"Discord image download returned HTTP {response.status_code}; reattach if expired"
                            )
                        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if mime not in {
                            *FORMATS.values(),
                            "image/jpg",
                            "image/pjpeg",
                            "application/octet-stream",
                        }:
                            raise ValueError("Discord attachment response is not a supported raster image")
                        length = response.headers.get("content-length")
                        if length and int(length) > MAX_IMAGE_BYTES:
                            raise ImageSizeLimit()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                                raise ImageSizeLimit()
                            data.extend(chunk)
                        return bytes(data)

            if self.client is not None:
                data = await fetch(self.client)
            else:
                async with httpx.AsyncClient(trust_env=False) as client:
                    data = await fetch(client)
            data, details = await asyncio.to_thread(prepare_image, data)
            digest = hashlib.sha256(data).hexdigest()
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.path(digest)
            if target.exists():
                self.read({"sha256": digest})
            else:
                temporary = self.root / ("tmp-" + secrets.token_hex(16))
                try:
                    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                    directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    temporary.unlink(missing_ok=True)
            result["vision"] = {"status": "ready", "sha256": digest, **details}
        except (ValueError, OSError, httpx.HTTPError, TimeoutError) as exc:
            # Do not expose signed URLs or HTTP library request objects in event/prompt text.
            error = (
                str(exc)
                if isinstance(exc, ValueError)
                else f"Image capture failed ({type(exc).__name__}); reattach to retry"
            )
            result["vision"] = {"status": "unavailable", "error": error}
            if isinstance(exc, ImageSizeLimit):
                result["vision"]["size_limit_bytes"] = MAX_IMAGE_BYTES
            if isinstance(exc, ImagePixelLimit):
                result["vision"].update(
                    pixel_limit=MAX_PIXELS,
                    width=exc.width,
                    height=exc.height,
                    observed_pixels=exc.width * exc.height,
                )
            if not isinstance(exc, ValueError) or error.startswith("Discord image download returned HTTP"):
                result["vision"]["retry_on_refreshed_url"] = True
        return result

    async def prepare_rows(self, rows, *, bot_id=None):
        for row in rows:
            if row.get("deleted"):
                continue
            for index, attachment in enumerate(row["attachments"]):
                # Retry a previously rejected size once when a larger limit now admits it.
                # Other failures wait for a refreshed history URL or a new attachment.
                if image_candidate(attachment) and (
                    not attachment.get("vision") or retry_previous_size_limit(attachment)
                ):
                    row["attachments"][index] = await self.capture(attachment)
                    # Persist each completed attachment so a deadline/cancellation does not discard progress.
                    self.store.execute(
                        "UPDATE messages SET attachments=? WHERE discord_id=?",
                        (dumps(row["attachments"]), row["discord_id"]),
                    )
                    self.report_change(
                        row["attachments"][index], attachment, bot_id=bot_id, message_id=row["discord_id"]
                    )

    def content(self, text, rows):
        parts = [{"type": "text", "text": text}]
        for row in rows:
            if row.get("deleted"):
                continue
            for attachment in row["attachments"]:
                if not image_candidate(attachment):
                    continue
                vision = attachment.get("vision") or {}
                label = f"Image from message {row['discord_id']}, attachment {attachment.get('id')}: {attachment.get('filename')}"
                if vision.get("status") == "ready":
                    parts.append(
                        {
                            "type": "text",
                            "text": label + (". " + vision["warning"] if vision.get("warning") else ""),
                        }
                    )
                    parts.append(
                        {
                            "type": "hortator_image",
                            "image": dict(vision),
                            "message_id": row["discord_id"],
                            "attachment_id": attachment.get("id"),
                        }
                    )
                else:
                    parts.append(
                        {
                            "type": "text",
                            "text": label
                            + ". PIXELS UNAVAILABLE: "
                            + vision.get("error", "image has not been downloaded"),
                        }
                    )
        return parts if len(parts) > 1 else text

    def wire_messages(self, messages, profile=None):
        max_images, max_bytes = image_limits(profile)
        result = copy.deepcopy(messages)
        count = total = 0
        for message in result:
            if not isinstance(message.get("content"), list):
                continue
            for part in message["content"]:
                if part.get("type") != "hortator_image":
                    continue
                count += 1
                if count > max_images:
                    raise ControlError(
                        f"Request exceeds model profile limit of {max_images} images; adjust Model profiles → Context & retained summary or compact the conversation"
                    )
                try:
                    data = self.read(part["image"])
                except (OSError, ValueError) as exc:
                    raise ControlError(
                        "A referenced image is unavailable in the local cache; reattach it before retrying"
                    ) from exc
                total += len(data)
                if total > max_bytes:
                    raise ControlError(
                        f"Request images exceed model profile limit of {max_bytes // (1024 * 1024)} MiB; adjust Model profiles → Context & retained summary or compact the conversation"
                    )
                mime = part["image"]["content_type"]
                if mime not in FORMATS.values():
                    raise ControlError("Unsupported cached image format")
                part.clear()
                part.update(
                    type="image_url",
                    image_url={"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")},
                )
        return result


def image_count(messages):
    return sum(
        1
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "hortator_image"
    )


def image_bytes(messages):
    return sum(
        part["image"].get("size", 0)
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "hortator_image"
    )


def image_limits(profile=None):
    profile = profile or {}
    return profile.get("max_request_images", MAX_IMAGES), profile.get(
        "max_request_image_mib", 40
    ) * 1024 * 1024


def image_limits_exceeded(messages, profile=None):
    max_images, max_bytes = image_limits(profile)
    return image_count(messages) > max_images or image_bytes(messages) > max_bytes
