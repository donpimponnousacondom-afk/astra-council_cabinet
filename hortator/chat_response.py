"""Bounded SSE framing and OpenAI-compatible response validation.

Null/empty keepalives are compatibility input, not provider failures. Malformed
responses are explicit format failures; no text is ever interpreted as a tool.
"""

import codecs
import json
import re
from dataclasses import dataclass

from .diagnostics import text_content

" Ultra fast patch to solve issues with SSE stream cuts offs
" It was 8MB (it blows apart with SSE streams of ~94KB chars which is SAD)
"""
Astra comment to the future agent or human:
So roughly **95 KB of reasoning arrived inside an 8 MB stream**. Thousands of small chunks, each wrapped in repeated metadata, exhausted our transport-size allowance before the model produced the summary.

This also exposes a gap in our earlier compaction change: **we removed the outgoing token caps, but this independent response-byte cap remained.** It can still interrupt lengthy reasoning.

The previous context was preserved, and partial reasoning was retained for private inspection. No configuration was changed.

The appropriate fix is to separate limits on individual SSE frames and accumulated model content from the total framing overhead, so legitimate long streams aren’t rejected simply for sending many small chunks. Buffered responses currently share the same 8 MB ceiling, although they avoid that repeated framing overhead.
"""
RESPONSE_LIMIT = (512 * 1024 * 1024) + 1

class ResponseFormatError(Exception):
    def __init__(self, path, expected, value, *, message=None):
        actual = "null" if value is None else type(value).__name__
        super().__init__(message or f"Invalid response at {path}: expected {expected}, received {actual}")
        self.details = {"field": path, "expected": expected, "received_type": actual}


class UpstreamResponseError(Exception):
    def __init__(self, envelope):
        self.envelope = envelope


@dataclass
class Frame:
    data: str
    event: str = "message"
    id: str = ""
    index: int = 0


class SSEReader:
    """UTF-8/BOM, CR/LF/CRLF, comments, named events and multiline data.

    No automatic reconnect/replay of a paid POST. For compatible gateways, a
    final data event without a blank separator is accepted and recorded at EOF.
    """

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.parts, self.data = [], []
        self.event, self.event_id = "", ""
        self.previous_cr = False
        self.index, self.size = 0, 0
        self.current = None

    def note(self, key):
        counts = self.diagnostics.setdefault("compatibility", {})
        counts[key] = counts.get(key, 0) + 1

    def line(self, line):
        if not line:
            if not self.data and not self.event:
                return None
            self.index += 1
            frame = Frame("\n".join(self.data), self.event or "message", self.event_id, self.index)
            self.data, self.event = [], ""
            self.diagnostics["frames"] = self.index
            self.current = frame
            return frame
        if line.startswith(":"):
            self.note("comments")
            return None
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            self.data.append(value)
        elif name == "event":
            self.event = value
        elif name == "id" and "\0" not in value:
            self.event_id = value
        # retry/unknown fields do not reconnect or affect model content.
        return None

    def feed(self, text):
        if not text:
            return
        if self.previous_cr and text.startswith("\n"):
            text = text[1:]
        self.previous_cr = text.endswith("\r")
        start = 0
        for separator in re.finditer(r"\r\n|\r|\n", text):
            self.parts.append(text[start : separator.start()])
            frame = self.line("".join(self.parts))
            self.parts = []
            start = separator.end()
            if frame is not None:
                yield frame
        if start < len(text):
            self.parts.append(text[start:])

    async def frames(self, response):
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        try:
            async for chunk in response.aiter_bytes():
                self.size += len(chunk)
                self.diagnostics["response_bytes"] = self.size
                if self.size > RESPONSE_LIMIT:
                    raise ResponseFormatError(
                        "SSE body",
                        "at most 8 MB",
                        "oversize",
                        message="Provider stream exceeded the 8 MB response limit",
                    )
                for frame in self.feed(decoder.decode(chunk)):
                    yield frame
            for frame in self.feed(decoder.decode(b"", final=True)):
                yield frame
        except UnicodeError as exc:
            raise ResponseFormatError("SSE body", "UTF-8", "invalid encoding") from exc
        if self.parts:
            self.line("".join(self.parts))
            self.parts = []
        if self.data or self.event:
            self.note("final_event_without_separator")
            frame = self.line("")
            if frame is not None:
                yield frame


def object_value(value, path, *, nullable=False):
    if value is None and nullable:
        return {}
    if not isinstance(value, dict):
        raise ResponseFormatError(path, "object" + (" or null" if nullable else ""), value)
    return value


def array_value(value, path):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ResponseFormatError(path, "array or null", value)
    return value


def string_value(value, path):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ResponseFormatError(path, "string or null", value)
    return value


def assistant_text(value, path):
    try:
        return text_content(value)
    except ValueError as exc:
        raise ResponseFormatError(path, "string, text-part array or null", value) from exc


class ChatResponse:
    def __init__(self, result):
        self.result = result
        self.calls = {}
        self.trace = result.response_diagnostics
        self.current = None
        self.choices_seen = False

    def note(self, key):
        counts = self.trace.setdefault("compatibility", {})
        counts[key] = counts.get(key, 0) + 1

    def decode(self, data):
        def invalid_constant(value):
            raise ValueError("Non-finite JSON number")

        try:
            return json.loads(data, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            detail = f"Invalid response JSON: {type(exc).__name__}"
            if isinstance(exc, json.JSONDecodeError):
                detail += f" ({exc.msg}, line {exc.lineno}, column {exc.colno})"
            raise ResponseFormatError("data", "valid JSON", "invalid JSON", message=detail) from exc

    def packet(self, packet, *, streamed, event="message"):
        if event == "error" and not isinstance(packet, dict):
            raise UpstreamResponseError({"message": packet or "Provider sent an empty SSE error event"})
        if packet is None and streamed:
            self.note("null_packets")
            return
        packet = object_value(packet, "packet")
        if packet.get("error") or event == "error":
            # Capture reported usage even when the same packet reports an error.
            if isinstance(packet.get("usage"), dict):
                self.result.usage.update(packet["usage"])
            raise UpstreamResponseError(packet.get("error") or packet)
        self.result.metadata.update(
            {
                k: packet[k]
                for k in ("id", "model", "created", "system_fingerprint", "provider", "service_tier")
                if k in packet
            }
        )
        usage = object_value(packet.get("usage"), "usage", nullable=True)
        self.result.usage.update(usage)
        choices = array_value(packet.get("choices"), "choices")
        if not choices:
            if streamed and (
                not packet
                or "choices" in packet
                or "usage" in packet
                or set(packet)
                <= {"id", "model", "created", "system_fingerprint", "provider", "service_tier", "object"}
                or event in ("ping", "keepalive", "heartbeat")
            ):
                self.note("empty_or_usage_packets")
                return
            raise ResponseFormatError(
                "choices",
                "OpenAI-compatible choices array",
                packet.get("choices"),
                message="Response has no OpenAI-compatible choices; check the endpoint response format",
            )
        for i, choice in enumerate(choices):
            if choice is None and streamed:
                self.note("null_choices")
                continue
            path = f"choices[{i}]"
            choice = object_value(choice, path)
            index = choice.get("index", i)
            if index is None and len(choices) == 1:
                index = 0
            if type(index) is not int or index < 0:
                raise ResponseFormatError(path + ".index", "nonnegative integer", index)
            if index != 0:
                continue
            self.choices_seen = True
            field = "delta" if streamed and "delta" in choice else "message"
            msg = object_value(choice.get(field), path + "." + field, nullable=streamed)
            self.message(msg, path + "." + field, streamed=streamed and field == "delta")
            finish = string_value(choice.get("finish_reason"), path + ".finish_reason")
            if finish:
                if self.result.finish_reason and finish != self.result.finish_reason:
                    raise ResponseFormatError(
                        path + ".finish_reason", "consistent completion boundary", finish
                    )
                self.result.finish_reason = finish

    def message(self, msg, path, *, streamed):
        self.result.content += assistant_text(msg.get("content"), path + ".content")
        reasoning_added = False
        for name in ("reasoning_content", "reasoning", "thinking"):
            reasoning = assistant_text(msg.get(name), path + "." + name)
            if reasoning and not reasoning_added:
                self.result.reasoning_content += reasoning
                reasoning_added = True
        details = array_value(msg.get("reasoning_details"), path + ".reasoning_details")
        for detail in details:
            if detail is not None:
                self.result.reasoning_details.append(object_value(detail, path + ".reasoning_details[]"))
        tools = array_value(msg.get("tool_calls"), path + ".tool_calls")
        if len(tools) > 100:
            raise ResponseFormatError(path + ".tool_calls", "at most 100 tool calls", tools)
        for i, call in enumerate(tools):
            if call is None and streamed:
                self.note("null_tool_deltas")
                continue
            call_path = f"{path}.tool_calls[{i}]"
            call = object_value(call, call_path)
            index = call.get("index") if streamed else i
            call_id = string_value(call.get("id"), call_path + ".id")
            if index is None:
                matching = [k for k, v in self.calls.items() if call_id and v["id"] == call_id]
                if matching:
                    index = matching[0]
                elif len(tools) == 1 and len(self.calls) <= 1:
                    index = next(iter(self.calls), 0)
                else:
                    raise ResponseFormatError(call_path + ".index", "unambiguous tool index", index)
                self.note("inferred_single_tool_index")
            if type(index) is not int or not 0 <= index < 100:
                raise ResponseFormatError(call_path + ".index", "integer from 0 to 99", index)
            entry = self.calls.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            # Keep partial, non-executable evidence even if a later field fails.
            self.result.tool_calls = [self.calls[k] for k in sorted(self.calls)]
            if call.get("type") not in (None, "function"):
                raise ResponseFormatError(call_path + ".type", "function", call.get("type"))
            if call_id:
                if entry["id"] and entry["id"] != call_id:
                    raise ResponseFormatError(call_path + ".id", "consistent tool identity", call_id)
                entry["id"] = call_id
            fn = object_value(call.get("function"), call_path + ".function", nullable=True)
            entry["function"]["name"] += string_value(fn.get("name"), call_path + ".function.name")
            entry["function"]["arguments"] += string_value(
                fn.get("arguments"), call_path + ".function.arguments"
            )
        self.result.tool_calls = [self.calls[k] for k in sorted(self.calls)]

    def activity(self):
        return bool(
            self.result.content
            or self.result.reasoning_content
            or self.result.reasoning_details
            or any(c["function"]["arguments"] for c in self.calls.values())
        )

    def frame(self, frame):
        self.current = frame
        self.trace.update(frame_index=frame.index, event_type=frame.event)
        payload = frame.data.strip()
        if frame.event == "error" and not payload:
            raise UpstreamResponseError({"message": "Provider sent an empty SSE error event"})
        if not payload:
            self.note("empty_data")
            return False
        if payload == "[DONE]":
            if frame.event == "error":
                raise UpstreamResponseError({"message": "Provider sent an SSE error event"})
            self.trace["completion_boundary"] = "done"
            return True
        if frame.event in ("ping", "keepalive", "heartbeat") and payload in (
            "ping",
            "keepalive",
            "heartbeat",
        ):
            self.note("named_keepalives")
            return False
        try:
            packet = self.decode(payload)
        except ResponseFormatError:
            if frame.event == "error":
                raise UpstreamResponseError({"message": payload}) from None
            raise
        self.packet(packet, streamed=True, event=frame.event)
        return False

    def finish(self):
        if not self.choices_seen:
            raise ResponseFormatError(
                "choices",
                "at least one assistant choice",
                None,
                message="Response contained no assistant choice",
            )
        for call in self.result.tool_calls:
            if not call["id"] or not call["function"]["name"]:
                raise ResponseFormatError("tool_calls", "complete tool IDs and function names", call)

    def failure_evidence(self, redact):
        if self.current is None:
            return {}
        # Keep offending data only in private diagnostics. Redact before slicing.
        try:
            data = json.dumps(redact(json.loads(self.current.data)), ensure_ascii=False)
        except ValueError, RecursionError:
            data = redact(self.current.data)
        return {
            "frame_index": self.current.index,
            "event_type": self.current.event,
            "data_excerpt": data[:4000],
            "truncated": len(data) > 4000,
        }
