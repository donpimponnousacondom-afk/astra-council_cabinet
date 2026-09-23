"""Shared discord formatting test helpers."""

from types import SimpleNamespace

from hortator.models import OWNER_ID


def message(content, channel):
    return SimpleNamespace(
        content=content,
        channel=channel,
        author=SimpleNamespace(id=int(OWNER_ID), bot=False),
        webhook_id=None,
        guild=None,
        attachments=[],
    )
