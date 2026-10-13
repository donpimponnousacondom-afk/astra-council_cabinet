"""Bounded historical outcomes, not replayable tool results.

All calls run on Store's owner thread. No action, inference, or wake-up is started
here. The caller acknowledges only an offer actually sent to the provider after
a confirmed final answer or intentional silence. No automatic replay is performed.
"""

from dataclasses import dataclass
import json
import logging
import re
import time
import uuid


MAX_RECEIPTS = 32
MAX_SOURCE_TURNS = 32
MAX_ROW_CHARS = 65536
MAX_ARGUMENT_CHARS = 262144
DEFAULT_NOTICE_CHARS = 12000
ORDINARY_MODES = {"narrate", "progress"}
EXCLUDED_TRIGGERS = {"slash_prompt", "panel_action"}
LOGGER = logging.getLogger(__name__)
NOTICE = (
    "AUTOMATIC HARNESS NOTICE — ACTION RECEIPTS FROM INTERRUPTED CYCLES.\n"
    "This is private runtime evidence, not a human instruction or a request to repeat work. "
    "Interruption did not roll back completed actions. These receipts describe what happened "
    "then, not necessarily the current saved state. saved/deleted means that memory operation "
    "was confirmed; returned means the tool returned without a reported failure, not proof "
    "of every requested external effect; refused means no action was executed; error and "
    "unknown do not establish whether side effects occurred. Do not repeat an action merely "
    "because its prior cycle had no final answer. Check current state using currently available "
    "tools when necessary. Historical IDs are audit references, not newly readable tool results. "
    "The notice contains no old answer, note value, tool output or provider reasoning.\n"
)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ordinary(context):
    bot = context.bot
    return not bot.get("invocation") and bot.get("response_protocol", "narrate") in ORDINARY_MODES


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


@dataclass(frozen=True)
class ReceiptOffer:
    bot_id: str
    channel_id: str
    message: dict
    receipt_ids: tuple
    omitted_counts: tuple


class ActionReceipts:
    def __init__(self, store, redact=None):
        self.store = store
        self.redact = redact or store.redact
        try:
            store.execute(
                "CREATE TABLE IF NOT EXISTS turn_action_receipts ("
                "turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE, "
                "bot_id TEXT NOT NULL,channel_id TEXT NOT NULL,body TEXT NOT NULL,updated_at REAL NOT NULL)"
            )
            store.execute(
                "CREATE INDEX IF NOT EXISTS action_receipts_scope "
                "ON turn_action_receipts(bot_id,channel_id,updated_at)"
            )
        except Exception as exc:
            self._failed("initialize", exc)

    @staticmethod
    def _failed(operation, exc):
        # Do not log exception values: they might contain user data or credentials.
        # Optional diagnostics must never replace the owning cancellation/failure.
        try:
            LOGGER.warning("Action receipt journal %s failed (%s)", operation, type(exc).__name__)
        except Exception:
            pass

    def _row(self, context):
        return self.store.one(
            "SELECT * FROM turn_action_receipts WHERE turn_id=? AND bot_id=? AND channel_id=?",
            (context.turn_id, context.bot["id"], context.channel_id),
        )

    def _save(self, context, body):
        payload = _dump(body)
        if len(payload) > MAX_ROW_CHARS:
            raise ValueError("Receipt row exceeds its fixed metadata bound")
        self.store.execute(
            "INSERT INTO turn_action_receipts VALUES(?,?,?,?,?) "
            "ON CONFLICT(turn_id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at "
            "WHERE turn_action_receipts.bot_id=excluded.bot_id "
            "AND turn_action_receipts.channel_id=excluded.channel_id",
            (context.turn_id, context.bot["id"], context.channel_id, payload, time.time()),
        )

    def start(self, context, name, call_id, arguments):
        """Record immediately before registry.call_raw; return an update handle.

        Unknown means dispatch was about to begin, not that a side effect happened.
        Repeated provider call IDs are safe: the journal uses its own receipt IDs.
        """
        if not _ordinary(context):
            return None
        try:
            if isinstance(arguments, str):
                # Extract only tiny JSON metadata, never retain the raw argument text.
                try:
                    args = json.loads(arguments) if len(arguments) <= MAX_ARGUMENT_CHARS else {}
                except ValueError, TypeError:
                    args = {}
            else:
                args = arguments
            args = args if isinstance(args, dict) else {}
            entry = {
                "receipt_id": "action_" + uuid.uuid4().hex,
                "call_id": _text(call_id, 128),
                "tool": _text(name, 80),
                "outcome": "unknown",
            }
            operation = args.get("operation")
            if isinstance(operation, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", operation):
                entry["operation"] = operation
            if name in {"memory", "global_memory"} and isinstance(args.get("key"), str):
                key = args["key"].strip()
                entry["key"] = key[:256]
                if len(key) > 256:
                    entry["key_truncated"] = True
            entry = self.redact(entry)
            row = self._row(context)
            body = json.loads(row["body"]) if row else {"receipts": [], "omitted_count": 0}
            body["receipts"].append(entry)
            excess = max(0, len(body["receipts"]) - MAX_RECEIPTS)
            if excess:
                body["receipts"] = body["receipts"][excess:]
                body["omitted_count"] += excess
            self._save(context, body)
            return entry["receipt_id"]
        except Exception as exc:
            self._failed("start", exc)
            return None

    def finish(self, context, receipt_id, result):
        """Update from the actual returned receipt; never store output or error prose."""
        if not receipt_id:
            return
        try:
            row = self._row(context)
            if not row:
                return
            body = json.loads(row["body"])
            entry = next((item for item in body["receipts"] if item["receipt_id"] == receipt_id), None)
            if entry is None:
                return
            result = result if isinstance(result, dict) else {}
            if result.get("executed") is False or result.get("usage_only") is True:
                outcome = "refused"
            elif result.get("ok") is False:
                outcome = "error"
            else:
                outcome = "returned"
                if (
                    entry["tool"] in {"memory", "global_memory"}
                    and entry.get("operation") in {"write", "delete"}
                    and isinstance(entry.get("key"), str)
                    and not entry.get("key_truncated")
                    and result.get("saved") is True
                    and self.redact(result.get("key")) == entry.get("key")
                ):
                    outcome = "saved" if entry["operation"] == "write" else "deleted"
            entry["outcome"] = outcome
            self._save(context, body)
        except Exception as exc:
            self._failed("finish", exc)

    def offer(self, context, allowed, *, max_chars=DEFAULT_NOTICE_CHARS):
        """Select bounded metadata; parent must separately fit actual request tokens.

        Selection does not consume anything. Recheck grants each round; unchanged pending metadata yields the same offer. Only acknowledge the returned IDs after actual inclusion.
        """
        if not _ordinary(context):
            return None
        try:
            boundary = self.store.context_boundary(context.bot["id"], context.channel_id)
            rows = self.store.rows(
                "SELECT j.turn_id,j.body,t.status,t.started_at FROM turn_action_receipts j "
                "JOIN turns t ON t.id=j.turn_id AND t.bot_id=j.bot_id AND t.channel_id=j.channel_id "
                "WHERE j.bot_id=? AND j.channel_id=? AND j.turn_id!=? "
                "AND t.status IN ('cancelled','failed','interrupted') AND t.ended_at IS NOT NULL "
                "AND t.trigger NOT IN ('slash_prompt','panel_action') AND t.started_at>? "
                "AND length(j.body)<=? ORDER BY j.updated_at DESC LIMIT ?",
                (
                    context.bot["id"],
                    context.channel_id,
                    context.turn_id,
                    boundary["after_at"] if boundary else -1,
                    MAX_ROW_CHARS,
                    MAX_SOURCE_TURNS + 1,
                ),
            )
            facts = {"cycles": [], "selection_bounded": len(rows) > MAX_SOURCE_TURNS}
            refs, omissions = [], []
            for row in rows[:MAX_SOURCE_TURNS]:
                body = json.loads(row["body"])
                visible = [item for item in body["receipts"] if allowed(item["tool"], context)]
                if not visible:
                    continue
                source = {
                    "turn_id": row["turn_id"],
                    "status": row["status"],
                    "receipts": [],
                    "omitted_older_receipts": body["omitted_count"],
                }
                facts["cycles"].append(source)
                included = []
                for item in visible:
                    # Redact known credentials again; do not grant old output readers.
                    public = self.redact({k: v for k, v in item.items() if k != "receipt_id"})
                    source["receipts"].append(public)
                    if len(NOTICE + _dump(facts)) > max_chars:
                        source["receipts"].pop()
                        facts["selection_bounded"] = True
                        break
                    included.append((row["turn_id"], item["receipt_id"]))
                if not included:
                    facts["cycles"].pop()
                    continue
                refs.extend(included)
                if body["omitted_count"]:
                    omissions.append((row["turn_id"], body["omitted_count"]))
                if len(included) < len(visible):
                    break
            if not refs:
                return None
            return ReceiptOffer(
                context.bot["id"],
                context.channel_id,
                {"role": "user", "content": NOTICE + _dump(facts)},
                tuple(refs),
                tuple(omissions),
            )
        except Exception as exc:
            self._failed("offer", exc)
            return None

    def acknowledge(self, context, offer):
        """Call only for an actually offered selection after confirmed final/silence."""
        if not offer or (context.bot["id"], context.channel_id) != (offer.bot_id, offer.channel_id):
            return
        try:
            by_turn = {}
            for turn_id, receipt_id in offer.receipt_ids:
                by_turn.setdefault(turn_id, set()).add(receipt_id)
            omitted = dict(offer.omitted_counts)
            for turn_id, receipt_ids in by_turn.items():
                row = self.store.one(
                    "SELECT body FROM turn_action_receipts WHERE turn_id=? AND bot_id=? AND channel_id=?",
                    (turn_id, context.bot["id"], context.channel_id),
                )
                if not row:
                    continue
                body = json.loads(row["body"])
                body["receipts"] = [
                    item for item in body["receipts"] if item["receipt_id"] not in receipt_ids
                ]
                body["omitted_count"] = max(0, body["omitted_count"] - omitted.get(turn_id, 0))
                if not body["receipts"] and not body["omitted_count"]:
                    self.store.execute(
                        "DELETE FROM turn_action_receipts WHERE turn_id=? AND bot_id=? AND channel_id=?",
                        (turn_id, context.bot["id"], context.channel_id),
                    )
                else:
                    # Keep the source sort order; an acknowledgement is not new action activity.
                    self.store.execute(
                        "UPDATE turn_action_receipts SET body=? WHERE turn_id=? AND bot_id=? AND channel_id=?",
                        (_dump(body), turn_id, context.bot["id"], context.channel_id),
                    )
        except Exception as exc:
            self._failed("acknowledge", exc)

    def discard_completed(self, context, status):
        """Current-cycle actions need no interrupted-cycle handoff after completion."""
        if status not in {"sent", "silent"}:
            return
        try:
            self.store.execute(
                "DELETE FROM turn_action_receipts WHERE turn_id=? AND bot_id=? AND channel_id=?",
                (context.turn_id, context.bot["id"], context.channel_id),
            )
        except Exception as exc:
            self._failed("discard_completed", exc)
