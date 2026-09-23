"""Text-only answer validation, with two deliberately distinct policies.

Ordinary answers use ``wrapped_reply`` to detect obsolete reply envelopes at
recognizable boundaries. Research reports use ``literal_tool_call_envelope`` to
also detect embedded, unhandled tool requests while excluding quoted examples.
Sharing their location does not make these policies interchangeable: broadening
the ordinary filter would reject valid tool-related discussion and JSON answers.
Neither detector parses an action for execution or rewrites saved evidence.

These are bounded heuristics, not a grammar for arbitrary provider output. Prefer
false negatives for unfamiliar or ambiguous prose over rejecting useful source
excerpts; expand recognition only with explicit policy and regression evidence.
"""

import json
import re


def wrapped_reply(content):
    """Recognize obsolete reply envelopes, never parse or execute text as a tool.

    Deliberately narrow: ordinary JSON, prose about tools, and fenced examples are valid answers.
    """
    text = content.lstrip()
    return bool(
        re.match(r'\{\s*"(?:tool|name)"\s*:\s*"council_speak"', text)
        or re.match(r'\{\s*"function"\s*:\s*\{\s*"name"\s*:\s*"council_speak"', text)
        or re.match(r"<(?:tool_call|function(?:=|\s|>))", text, re.I)
        or ("```" not in text and re.search(r"</(?:parameter|function)\s*>\s*$", text, re.I))
    )


_TOOL_ENVELOPE = re.compile(
    r"<\s*tool_call(?:\s[^<>]*?)?\s*>\s*<(?:function\b|parameter\b)"
    r"|<\s*tool_call(?:\s[^<>]*?)?\s*>\s*\{[\s\S]*?\}\s*</\s*tool_call\s*>"
    r"|<\s*function(?:\s*=\s*[\w.-]+|\s+name\s*=\s*['\"]?[\w.-]+['\"]?)\s*>"
    r"\s*(?:<\s*parameter\b|[^<>]*</\s*function\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_BARE_TOOL_OPEN = re.compile(r"(?:^|\n)\s*<\s*tool_call(?:\s[^<>]*?)?\s*>\s*$", re.IGNORECASE)
_INLINE_CODE = re.compile(r"(`+)(?!`)[^`\n]*?\1(?!`)")
_QUOTED_MARKUP = re.compile(r"(['\"])(?=<\s*(?:tool_call|function)\b)[^\n]*?\1", re.IGNORECASE)
_KNOWN_TOOL_NAMES = frozenset({"council_speak", "council_silence", "web_search"})


def _json_tool_wrapper(text: str) -> bool:
    """Only classify a whole JSON object with an explicit tool-call shape."""
    try:
        value = json.loads(text)
    except ValueError, TypeError:
        return False
    if not isinstance(value, dict):
        return False
    calls = value.get("tool_calls")
    if isinstance(calls, list) and any(isinstance(call, dict) for call in calls):
        return True
    function = value.get("function")
    if (
        isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and function["name"] in _KNOWN_TOOL_NAMES
    ):
        return True
    name = value.get("tool", value.get("name"))
    return isinstance(name, str) and (
        name == "council_speak"
        or (name in _KNOWN_TOOL_NAMES and ("arguments" in value or "parameters" in value))
    )


def literal_tool_call_envelope(content: str) -> bool:
    """Recognize unhandled tool envelopes in visible report text, never execute them.

    Markdown examples and quoted source excerpts are evidence, not researcher
    instructions. Keep the saved report untouched; filtering is validation only.
    """
    visible = []
    fence_char = None
    fence_width = 0
    for line in content.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence_char:
            if marker and marker.group(1)[0] == fence_char and len(marker.group(1)) >= fence_width:
                fence_char = None
            visible.append("[quoted example]")
            continue
        if marker:
            fence_char, fence_width = marker.group(1)[0], len(marker.group(1))
            visible.append("[quoted example]")
            continue
        if re.match(r"^\s*>", line):
            visible.append("[quoted example]")
            continue
        visible.append(_QUOTED_MARKUP.sub(" ", _INLINE_CODE.sub(" ", line)))
    text = "\n".join(visible)
    return bool(_TOOL_ENVELOPE.search(text) or _BARE_TOOL_OPEN.search(text) or _json_tool_wrapper(text))
