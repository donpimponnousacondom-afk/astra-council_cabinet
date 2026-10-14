"""Durable, provisional compaction progress; never an active context checkpoint."""

import hashlib
import time

from .store import dumps


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def prefix_hashes(transcript):
    """Detached worker input only. Relative age changes are not source edits."""
    result = []
    previous = ""
    for item in transcript:
        previous = digest([previous, {k: v for k, v in item.items() if k != "seconds_ago"}])
        result.append(previous)
    return result


class CompactionProgress:
    def __init__(self, store, bot_id, channel_id):
        self.store = store
        self.scope = (bot_id, channel_id)

    def get(self):
        return self.store.one("SELECT * FROM compaction_progress WHERE bot_id=? AND channel_id=?", self.scope)

    def clear(self):
        self.store.execute("DELETE FROM compaction_progress WHERE bot_id=? AND channel_id=?", self.scope)

    def save(self, *, basis, source_hash, message_count, summary, summary_tokens, batches, request_id):
        self.store.execute(
            "INSERT INTO compaction_progress VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(bot_id,channel_id) DO UPDATE SET basis=excluded.basis,"
            "source_hash=excluded.source_hash,message_count=excluded.message_count,"
            "summary=excluded.summary,summary_tokens=excluded.summary_tokens,"
            "completed_batches=excluded.completed_batches,request_id=excluded.request_id,"
            "updated_at=excluded.updated_at",
            (
                *self.scope,
                basis,
                source_hash,
                message_count,
                summary,
                summary_tokens,
                batches,
                request_id,
                time.time(),
            ),
        )

    def source_revision(self):
        row = self.store.one(
            "SELECT revision FROM context_source_revisions WHERE channel_id=?", (self.scope[1],)
        )
        return row["revision"] if row else 0
