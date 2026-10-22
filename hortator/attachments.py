"""Resolve direct attachment inputs through the existing, scoped export handlers."""

from __future__ import annotations

from dataclasses import replace

from .models import ControlError
from .store import uid
from .tool_feedback import errors_for


DESCRIPTION = (
    "Send files to this conversation and finish your reply in one call. "
    "Use files with saved filesystem paths, optionally prefixed by /workspace/. "
    "For private task files use {source:'workspace',task:'name',path:'file'}; "
    "for your document files use {source:'document_site',site:'name',path:'file'} "
    "and optionally revision. These sources still require their own tool grants. "
    "No export call is needed for saved files. Generated media can use current-turn artifact_ids. "
    "The optional message is sent with the files. If omitted, accompanying assistant text is used when present; otherwise the reply contains only files. "
    "An optional reply_to must be a message ID in this context. "
    "Successful delivery finishes this reply; do not call end_cycle or write another answer. "
    "If preparation fails, nothing is sent and the errors explain what to fix. "
)

_PATH = {"type": "string", "minLength": 1, "maxLength": 240}
_NAME = {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"}
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "files": {
            "type": "array",
            "maxItems": 4,
            "description": "Saved files to send. Strings address your saved filesystem; objects select another granted storage source.",
            "items": {
                "oneOf": [
                    {"type": "string", "minLength": 1, "maxLength": 251},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source": {"const": "workspace"},
                            "task": _NAME,
                            "path": _PATH,
                        },
                        "required": ["source", "task", "path"],
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source": {"const": "document_site"},
                            "site": _NAME,
                            "path": _PATH,
                            "revision": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 9007199254740991,
                            },
                        },
                        "required": ["source", "site", "path"],
                    },
                ]
            },
        },
        "artifact_ids": {
            "type": "array",
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
            "description": "Generated/exported artifacts belonging to this bot and current turn. Files and artifact_ids together allow at most four attachments.",
        },
        "message": {"type": "string", "description": "Optional text to send with the files."},
        "reply_to": {"type": "string", "minLength": 1},
    },
    "anyOf": [
        {"required": ["files"], "properties": {"files": {"minItems": 1}}},
        {"required": ["artifact_ids"], "properties": {"artifact_ids": {"minItems": 1}}},
    ],
    "examples": [
        {"files": ["reports/findings.md"], "message": "Here is the report."},
        {"files": [{"source": "workspace", "task": "analysis", "path": "report.md"}]},
        {"files": [{"source": "document_site", "site": "briefing", "path": "index.html"}]},
        {"artifact_ids": ["art_returned_by_generation"]},
    ],
}


class Attachments:
    """Snapshot files for final dispatch without granting another storage boundary."""

    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def _failure(errors):
        return {
            "ok": False,
            "executed": False,
            "error": "Attachment preparation failed. Fix every error below; nothing was sent.",
            "errors": errors,
            "error_count": len(errors),
        }

    @staticmethod
    def _source(item):
        if isinstance(item, str):
            path = item.removeprefix("/workspace/")
            return "filesystem", {"operation": "export", "path": path}
        if not isinstance(item, dict):
            return None
        source = item.get("source")
        keys = (
            {
                "workspace": ("task", "path"),
                "document_site": ("site", "path", "revision"),
            }.get(source)
            if isinstance(source, str)
            else None
        )
        if keys is None:
            return None
        return source, {"operation": "export", **{key: item[key] for key in keys if key in item}}

    def _check_artifact(self, artifact_id, context):
        path = self.registry.resolve_artifact(artifact_id, context)
        if not path.is_file():
            raise ControlError("Attachment data is missing; regenerate it or attach the saved source file")
        return path

    def _discard(self, created, context):
        # These IDs were created only by this attempt. Never remove existing
        # generated artifacts or another concurrent turn's snapshots.
        for artifact_id in created:
            try:
                path = self.registry.resolve_artifact(artifact_id, context)
                path.unlink(missing_ok=True)
                self.registry.store.execute(
                    "DELETE FROM artifacts WHERE id=? AND bot_id=? AND turn_id=?",
                    (artifact_id, context.bot["id"], context.turn_id),
                )
            except (ControlError, OSError) as exc:
                # A filesystem cleanup failure must not replace the owning
                # turn's cancellation or hide the actual selection errors.
                self.registry.store.emit(
                    "attachment.cleanup_failed",
                    self.registry.vault.redact({"artifact_id": artifact_id, "error": str(exc)}),
                    bot_id=context.bot["id"],
                    turn_id=context.turn_id,
                    level="warning",
                )

    async def prepare(self, args, context, *, errors=()):
        """Return owned artifact IDs, or all detectable argument/source errors.

        This method never sends. The engine validates reply targets and performs
        the final delivery through its existing cancellation/outbox contracts.
        """
        errors = [*errors, *errors_for(PARAMETERS, args)]
        if not isinstance(args, dict):
            return self._failure(errors)
        files = args.get("files", [])
        files = files if isinstance(files, list) else []
        supplied = args.get("artifact_ids", [])
        supplied = supplied if isinstance(supplied, list) else []
        if len(files) + len(supplied) > 4:
            errors.append({"path": "$", "message": "Select at most four files and artifact_ids combined"})
            return self._failure(errors)

        artifacts, created, sources = [], [], []
        successful = False
        try:
            for index, artifact_id in enumerate(supplied):
                if not isinstance(artifact_id, str):
                    continue
                try:
                    self._check_artifact(artifact_id, context)
                except (ControlError, OSError) as exc:
                    errors.append({"path": f"$.artifact_ids[{index}]", "message": str(exc)})
                else:
                    artifacts.append(artifact_id)

            for index, item in enumerate(files):
                selected = self._source(item)
                if selected is None:
                    continue
                source, export = selected
                field = f"$.files[{index}]"
                if not self.registry.allowed(source, context):
                    errors.append(
                        {
                            "path": field,
                            "message": f"{source} is not enabled for this bot and trusted request",
                        }
                    )
                    continue
                current_bot = self.registry.store.get("bots", context.bot["id"])
                current = replace(
                    context,
                    bot={**context.bot, "plugin_config": current_bot.get("plugin_config", {})},
                )
                result = await self.registry.call(source, export, current, uid("attach_source_"))
                if result.get("ok") is False or not result.get("artifact_id"):
                    details = result.get("errors") or [
                        {"message": result.get("error", "Source did not produce an attachment")}
                    ]
                    for detail in details:
                        errors.append(
                            {"path": field + detail.get("path", "$")[1:], "message": detail["message"]}
                        )
                    continue
                artifact_id = result["artifact_id"]
                created.append(artifact_id)
                artifacts.append(artifact_id)
                sources.append((source, field))

            # An in-flight operator grant change must not bypass the source's
            # access contract just because an earlier source was already copied.
            for source, field in sources:
                if not self.registry.allowed(source, context):
                    errors.append(
                        {
                            "path": field,
                            "message": f"{source} was disabled before attachment preparation completed",
                        }
                    )
            if errors:
                return self._failure(errors)
            successful = True
            return {"ok": True, "artifact_ids": artifacts}
        finally:
            if not successful:
                self._discard(created, context)
