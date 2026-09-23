"""Ordinary answer and researcher validation retain different acceptance policies."""

import pytest

from hortator.answer_validation import literal_tool_call_envelope, wrapped_reply
from hortator.research_assistant import literal_tool_call_envelope as research_validator
from hortator.runtime import wrapped_reply as runtime_validator


@pytest.mark.parametrize(
    "content,ordinary,research",
    [
        ('{"name":"web_search","arguments":{"query":"prices"}}', False, True),
        ('{"name":"council_speak","arguments":{}}', True, True),
        ("I will search next.\n<tool_call>", False, True),
        ("A report with a quoted example: `<tool_call><function=web_search>`.", False, False),
        ("```xml\n<tool_call><function=web_search></function></tool_call>\n```", False, False),
        ('{"findings":[{"tool":"web_search","result":"A source"}]}', False, False),
        ("Unfamiliar prose about future tool use stays ordinary text.", False, False),
    ],
)
def test_policy_boundaries_and_existing_import_facades(content, ordinary, research):
    assert runtime_validator is wrapped_reply
    assert research_validator is literal_tool_call_envelope
    assert wrapped_reply(content) is ordinary
    assert literal_tool_call_envelope(content) is research
