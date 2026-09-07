"""Discord text formatting without escaping a bot's native Markdown."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeBlock:
    text: str
    language: str = ""


def preview(text, budget=1800):
    data = text.encode("utf-16-le")
    return data[: budget * 2].decode("utf-16-le", errors="ignore")


def code_pages(text, language=""):
    # A commit title or inspected value must not break out of the enclosing block.
    text = text.replace("```", "``\u200b`")
    budget = 2000 - len(f"```{language}\n\n```".encode("utf-16-le")) // 2
    while text:
        chunk = preview(text, budget)
        if len(chunk) < len(text) and "\n" in chunk:
            chunk = chunk[: chunk.rfind("\n") + 1]
        yield f"```{language}\n{chunk}\n```"
        text = text[len(chunk) :]


def markdown_preview(text, budget=1700):
    result = preview(text, budget)
    if result.count("```") % 2:
        result += "\n```"
    return result
