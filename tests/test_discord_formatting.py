from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hortator.discord_gateway import COMMANDS
from hortator.discord_text import code_pages
from hortator.models import OWNER_ID
from test_typing import prepare_bot


def message(content, channel):
    return SimpleNamespace(
        content=content,
        channel=channel,
        author=SimpleNamespace(id=int(OWNER_ID), bot=False),
        webhook_id=None,
        guild=None,
        attachments=[],
    )


async def test_help_is_complete_fenced_pages_without_preface_or_model_calls(kernel):
    channel = SimpleNamespace(id=123, send=AsyncMock())
    await kernel.connector.receive("hortator", message("!help", channel))
    pages = [call.kwargs["content"] for call in channel.send.await_args_list]
    assert pages
    for page in pages:
        assert page.startswith("```\n") and page.endswith("\n```\n-# TTFT: — | TPS: —")
        assert len(page.encode("utf-16-le")) // 2 <= 2000
        assert OWNER_ID not in page and "Only The Boss" not in page
    assert "".join(page.rsplit("\n-# ", 1)[0][4:-4] for page in pages) == "\n".join(
        f"{cmd} — {desc}" for cmd, desc in COMMANDS
    )
    assert any("!version | !ver" in page for page in pages)
    assert all("file" not in call.kwargs for call in channel.send.await_args_list)
    assert not kernel.store.rows("SELECT * FROM requests")


@pytest.mark.parametrize("command", ["!version", "!ver"])
async def test_version_aliases_are_immediate_fenced_and_redacted(kernel, command):
    channel = SimpleNamespace(id=123, send=AsyncMock())
    kernel.vault.put("provider/openrouter/api_key", "fake-version-test-secret")
    kernel.service._version["commit_title"] = "Build title fake-version-test-secret"
    await kernel.connector.receive("hortator", message(command, channel))
    output = channel.send.await_args.kwargs["content"]
    assert output.startswith("```\nHortator · running server")
    assert kernel.service.version()["short_commit"] in output
    from hortator.timekeeping import local_timestamp

    assert local_timestamp(kernel.service.version()["committed_at"]) in output
    assert local_timestamp(kernel.service.version()["started_at"]) in output
    assert "fake-version-test-secret" not in output
    assert "[REDACTED]" in output
    assert not kernel.store.rows("SELECT * FROM requests")


@pytest.mark.parametrize("spoof", ["other-human", "bot", "webhook"])
async def test_tidy_help_and_version_keep_owner_enforcement(kernel, spoof):
    channel = SimpleNamespace(id=123, send=AsyncMock())
    incoming = message("!ver", channel)
    if spoof == "other-human":
        incoming.author.id = 999
    elif spoof == "bot":
        incoming.author.bot = True
    else:
        incoming.webhook_id = 999
    for command in ("!help", "!version", "!ver"):
        incoming.content = command
        await kernel.connector.receive("hortator", incoming)
    channel.send.assert_not_awaited()
    assert not kernel.store.rows("SELECT * FROM requests")


def test_code_pages_preserve_unicode_and_balance_fences_at_discord_limit():
    content = "🙂" * 2500 + "\n" + "some ``` embedded fences"
    pages = list(code_pages(content))
    assert all(len(page.encode("utf-16-le")) // 2 <= 2000 for page in pages)
    assert all(page.count("```") == 2 for page in pages)
    assert "".join(page[4:-4] for page in pages) == content.replace("```", "``\u200b`")


@pytest.mark.parametrize("bot_id", ["ada", "socrates", "dirac", "curie", "hortator"])
async def test_every_bot_delivers_native_discord_markdown_without_escaping(kernel, bot_id):
    bot, channel_id = prepare_bot(kernel, bot_id)
    channel = SimpleNamespace(id=int(channel_id), send=AsyncMock(return_value=SimpleNamespace(id=999)))
    kernel.connector.clients[bot_id] = SimpleNamespace(is_ready=lambda: True, get_channel=lambda _: channel)
    content = "# Heading\n-# Subtext\n**bold** *italic* __underline__ ~~strike~~ ||spoiler||\n- item\n1. ordered\n> quote\n[link](https://example.com) `code`\n```python\nprint('🙂')\n```"
    await kernel.connector.send(bot, channel_id, content, None, [], nonce="test-nonce")
    sent = channel.send.await_args
    expected = content + ("\n-# TTFT: — | TPS: —" if bot_id == "hortator" else "")
    assert sent.args[0] == expected and sent.kwargs["nonce"] == "test-nonce"
    assert not sent.kwargs["allowed_mentions"].everyone and not sent.kwargs["mention_author"]
    identity = kernel.engine.contexts.layers(bot, channel_id)[0]["content"]
    assert "Discord Markdown" in identity


async def test_long_markdown_preview_closes_fence_and_retains_full_attachment(kernel):
    bot, channel_id = prepare_bot(kernel)
    content = "```python\n" + "print('🙂')\n" * 400 + "```"
    captured = {}

    async def send(text, **kwargs):
        captured["text"] = text
        captured["file"] = kwargs["files"][0].fp.read().decode()
        return SimpleNamespace(id=999)

    channel = SimpleNamespace(send=send)
    kernel.connector.clients[bot["id"]] = SimpleNamespace(
        is_ready=lambda: True, get_channel=lambda _: channel
    )
    await kernel.connector.send(bot, channel_id, content, None, [])
    assert captured["file"] == content
    assert captured["text"].count("```") == 2
    assert captured["text"].endswith("```\n\n↳ Full response attached.")
    assert len(captured["text"].encode("utf-16-le")) // 2 <= 2000
