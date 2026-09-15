"""Bounded visible Discord text, independent of sender authority and commands.

Keep sources separate for partial MESSAGE_UPDATE payloads. Components are data:
never execute custom IDs, click buttons, or fetch arbitrary embedded URLs.
"""

import json

MAX_RICH_CHARS = 16000  # Per source (embeds/components), plus unchanged ordinary content.
MAX_COMPONENT_NODES = 100
MAX_COMPONENT_DEPTH = 8
TRUNCATED = "[Discord rich content truncated]"


def payload(value):
    if isinstance(value, dict):
        return value
    return value.to_dict() if hasattr(value, "to_dict") else {}


class Text:
    def __init__(self):
        self.parts = []
        self.remaining = MAX_RICH_CHARS
        self.truncated = False

    def add(self, value, label=""):
        if not isinstance(value, str) or not value.strip():
            return
        text = label + value
        if len(text) > self.remaining:
            self.truncated = True
        if self.remaining:
            self.parts.append(text[: self.remaining])
            self.remaining = max(0, self.remaining - len(text) - 2)

    def result(self):
        result = "\n\n".join(self.parts)
        if self.truncated:
            result += "\n\n" + TRUNCATED
        return result


def embed_text(embeds):
    text = Text()
    if not isinstance(embeds, (list, tuple)):
        return ""
    if len(embeds) > 10:
        text.truncated = True
    for value in embeds[:10]:
        embed = payload(value)
        text.add(payload(embed.get("author")).get("name"), "Author: ")
        text.add(embed.get("title"))
        text.add(embed.get("description"))
        text.add(embed.get("url"), "Link: ")
        fields = embed.get("fields") or []
        if isinstance(fields, list):
            if len(fields) > 25:
                text.truncated = True
            for field in fields[:25]:
                field = payload(field)
                text.add(field.get("name"))
                text.add(field.get("value"))
        text.add(payload(embed.get("footer")).get("text"), "Footer: ")
        for name in ("image", "thumbnail", "video"):
            text.add(payload(embed.get(name)).get("url"), f"{name.title()} link (metadata only): ")
    return text.result()


def component_text(components):
    text = Text()
    nodes = 0

    def walk(values, depth=0):
        nonlocal nodes
        if not isinstance(values, (list, tuple)):
            return
        if depth > MAX_COMPONENT_DEPTH:
            text.truncated = True
            return
        for value in values:
            nodes += 1
            if nodes > MAX_COMPONENT_NODES or not text.remaining:
                text.truncated = True
                return
            node = payload(value)
            kind = node.get("type")
            if kind == 10:  # Text Display
                text.add(node.get("content"))
            elif kind == 2:  # Visible button information, never a callable action.
                text.add(node.get("label"), "Button label: ")
                text.add(node.get("url"), "Button link: ")
            elif kind in (11, 13):
                media = payload(node.get("media" if kind == 11 else "file"))
                text.add(node.get("description"), "Media description: ")
                text.add(media.get("url"), "Media link (metadata only): ")
            elif kind == 12:
                items = node.get("items") or []
                if isinstance(items, list):
                    if len(items) > 10:
                        text.truncated = True
                    for item in items[:10]:
                        item = payload(item)
                        text.add(item.get("description"), "Media description: ")
                        text.add(payload(item.get("media")).get("url"), "Media link (metadata only): ")
            elif kind in (1, 9, 17):  # Action Row, Section, Container
                walk(node.get("components"), depth + 1)
                if kind == 9 and node.get("accessory"):
                    walk([node["accessory"]], depth + 1)
            # Unknown/interactive controls have no implicit text or executable meaning.

    walk(components)
    return text.result()


def from_message(message, content, *, council=False):
    return {
        "content": content,
        "embeds": "" if council else embed_text(getattr(message, "embeds", [])),
        "components": "" if council else component_text(getattr(message, "components", [])),
    }


def saved_parts(row):
    value = row.get("discord_parts") or {}
    if isinstance(value, str):
        value = json.loads(value)
    return {"content": row["content"], "embeds": "", "components": "", **value}


def compose(parts):
    result = parts.get("content") or ""
    for field, label in (("embeds", "Discord embeds"), ("components", "Discord components")):
        if parts.get(field):
            result += ("\n\n" if result else "") + f"[{label}]\n" + parts[field]
    return result
