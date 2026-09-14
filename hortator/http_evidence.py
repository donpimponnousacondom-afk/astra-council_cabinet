"""Received HTTP evidence is separate from our extraction and transport diagnoses."""

from dataclasses import dataclass, field
from email.message import Message
import base64

from .models import SECRET_FIELDS
from .working_set import ToolEvidence

MAX_URL_CHARS = 65_536


def headers_evidence(items):
    # Preserve duplicate headers and their order. Credentials remain private.
    return [
        {
            "name": name,
            "value": "[REDACTED]"
            if name.lower()
            in SECRET_FIELDS
            | {
                "cookie",
                "set-cookie",
                "csrf",
                "proxy-authorization",
            }
            else value,
        }
        for name, value in items
    ]


@dataclass
class HTTPResponse:
    data: bytes
    content_type: str
    url: str
    status: int | None = None
    reason: str | None = None
    headers: list = field(default_factory=list)
    redirects: list = field(default_factory=list)
    complete: bool = True
    limit: int = 1_000_000
    local_issue: str | None = None
    transport_error: str | None = None

    def evidence(self):
        header = Message()
        header["Content-Type"] = self.content_type
        encoding = header.get_content_charset() or "utf-8"
        try:
            body = self.data.decode(encoding, errors="strict")
            body_encoding = encoding
        except UnicodeError, LookupError:
            body = base64.b64encode(self.data).decode("ascii")
            body_encoding = "base64"
        return {
            "http_status": self.status,
            "http_reason": self.reason,
            "url": self.url,
            "response_headers": self.headers,
            "content_type": self.content_type,
            "body": body,
            "body_encoding": body_encoding,
            "body_bytes": len(self.data),
            "capture_complete": self.complete,
            "body_limit_bytes": self.limit,
            "redirects": self.redirects,
            "local_issue": self.local_issue,
            "transport_error": self.transport_error,
            "trust": "Untrusted upstream response; HTTP status does not establish task completion.",
        }


def record_response(store, vault, context, tool, response):
    evidence = vault.redact(response.evidence())
    saved = ToolEvidence(store, vault).record(context, tool, "", evidence)
    return {
        **{k: v for k, v in evidence.items() if k not in {"body", "redirects"}},
        "redirects": [{k: v for k, v in item.items() if k != "body"} for item in evidence["redirects"]],
        "body": evidence["body"][:4000],
        "body_truncated": len(evidence["body"]) > 4000,
        "response_result_id": saved["result_id"],
        "read_response": {
            "operation": "read_result",
            "result_id": saved["result_id"],
            "offset": 0,
            "length": 18000,
        },
    }
