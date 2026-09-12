"""Turn-scoped image inputs, independent of the durable transcript checkpoint."""

import json

from .vision import image_candidate, image_limits


def select_images(rows, after_sequence, profile):
    """Prefer the newest unhandled attachments without modifying stored rows."""
    max_count, max_bytes = image_limits(profile)
    selected, omitted, historical = [], [], 0
    total = 0
    for row in reversed(rows):
        if row.get("deleted"):
            continue
        for attachment in row["attachments"]:
            if not image_candidate(attachment):
                continue
            if row["seq"] <= after_sequence:
                historical += 1
                continue
            vision = attachment.get("vision") or {}
            if vision.get("status") != "ready":
                continue  # Fresh download failures remain explicit in transcript metadata.
            size = vision.get("size", 0)
            identity = {"message_id": row["discord_id"], "attachment_id": attachment.get("id")}
            reasons = []
            if len(selected) >= max_count:
                reasons.append("image_count")
            if total + size > max_bytes:
                reasons.append("image_bytes")
            if reasons:
                omitted.append({**identity, "reasons": reasons})
                continue
            selected.append(
                {
                    "type": "hortator_image",
                    **identity,
                    "filename": attachment.get("filename"),
                    "image": dict(vision),
                }
            )
            total += size
    return {
        "policy": "new_messages_per_turn",
        "after_sequence": after_sequence,
        "inputs": selected,
        "historical_images": historical,
        "budget_omitted": omitted,
    }


def current_inputs(store, channel_id, plan):
    """An edit/deletion during a long turn cannot keep revoked pixels alive."""
    sources, result = {}, []
    for part in plan["inputs"]:
        mid = part["message_id"]
        if mid not in sources:
            row = store.one(
                "SELECT deleted,attachments FROM messages WHERE channel_id=? AND discord_id=?",
                (channel_id, mid),
            )
            sources[mid] = json.loads(row["attachments"]) if row and not row["deleted"] else []
        if any(
            str(a.get("id")) == str(part["attachment_id"])
            and (a.get("vision") or {}).get("sha256") == part["image"]["sha256"]
            and (a.get("vision") or {}).get("status") == "ready"
            for a in sources[mid]
        ):
            result.append(part)
    return result


def with_inputs(text, inputs):
    if not inputs:
        return text
    parts = [{"type": "text", "text": text}]
    for part in inputs:
        parts.extend(
            [
                {
                    "type": "text",
                    "text": f"Current-turn image from message {part['message_id']}, attachment {part['attachment_id']}: {part['filename']}",
                },
                part,
            ]
        )
    return parts


def attachment_metadata(message_id, attachments, inputs):
    attached = {(p["message_id"], p["attachment_id"], p["image"]["sha256"]) for p in inputs}
    return [
        {
            **a,
            **(
                {
                    "pixels_in_this_request": (message_id, a.get("id"), (a.get("vision") or {}).get("sha256"))
                    in attached
                }
                if image_candidate(a)
                else {}
            ),
        }
        for a in attachments
    ]
