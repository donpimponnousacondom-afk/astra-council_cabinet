"""Shared literal double-brace syntax; callers own values and output formatting."""

import re


DOUBLE_BRACE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def double_brace_name(match):
    return match[1].upper()
