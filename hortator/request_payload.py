"""Serialize one detached request and measure bytes without retaining image data in logs."""

from urllib.parse import urlsplit

import httpx


def encode_request(body, endpoint):
    # Use the same JSON encoder as the HTTP client, exactly once. No vault/store
    # access here: large base64 payloads are prepared on a worker thread.
    payload = httpx.Request("POST", endpoint, json=body).content
    count = original_bytes = base64_bytes = 0
    for message in body.get("messages", []):
        if not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            image = part.get("image_url")
            if part.get("type") != "image_url" or not isinstance(image, dict):
                continue
            url = image.get("url")
            if not isinstance(url, str) or not url.startswith("data:image/"):
                continue
            _, sep, encoded = url.partition(";base64,")
            if not sep:
                continue
            count += 1
            base64_bytes += len(encoded)
            original_bytes += len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip("=")))
    facts = {
        "request_body_bytes": len(payload),
        "inline_image_count": count,
        "image_bytes": original_bytes,
        "image_base64_bytes": base64_bytes,
        "upstream_body_limit_bytes": None,
        "body_limit_basis": "unknown",
    }
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme == "https"
        and parsed.hostname == "api.deepseek.com"
        and parsed.port in (None, 443)
        and parsed.path in ("/chat/completions", "/v1/chat/completions")
    ):
        # Documentation verified 2026-09-12. This is explanatory evidence, not
        # a discovered server setting, a local enforcement cap, or a proxy limit.
        facts.update(
            upstream_body_limit_bytes=48 * 1024 * 1024,
            body_limit_basis="deepseek_documentation",
            upstream_body_limit_source="https://api-docs.deepseek.com/guides/vision/#limits",
        )
    return payload, facts
