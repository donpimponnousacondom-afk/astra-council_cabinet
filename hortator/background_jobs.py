"""Durable, plugin-independent jobs and single-consumption completion notifications.

SQLite is owned by the event loop. Workers belong to Kernel's TaskGroup; a
conversation turn is a submitter, never the lifetime owner of a background job.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback

from .concurrency import cancel_and_wait, error_text
from .models import ControlError
from .store import dumps, uid
from .timekeeping import council_timezone, local_timestamp

ACTIVE = ("queued", "running")
RESULT_LIMIT = 2 * 1024 * 1024


class JobResultError(ControlError):
    """A worker returned unusable output that must remain inspectable."""

    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class BackgroundJobs:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.handlers = {}
        self.limits = {}
        self.tasks = {}
        self.background = None
        self.engine = None
        self.closed = False
        store.execute("""CREATE TABLE IF NOT EXISTS background_jobs (
            id TEXT PRIMARY KEY, plugin TEXT NOT NULL, bot_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, origin_turn_id TEXT NOT NULL,
            submission_id TEXT NOT NULL, created_at REAL NOT NULL,
            deadline REAL NOT NULL, updated_at REAL NOT NULL, state TEXT NOT NULL,
            payload TEXT NOT NULL, result TEXT, error TEXT,
            notify INTEGER NOT NULL, notification TEXT NOT NULL DEFAULT 'none',
            continuation_turn_id TEXT, reply_to TEXT,
            UNIQUE(bot_id,channel_id,plugin,submission_id))""")
        store.execute("CREATE INDEX IF NOT EXISTS background_job_owner ON background_jobs(bot_id,state)")

    def register(self, name, *, run, check, limit=None):
        if name in self.handlers:
            raise ValueError(f"Duplicate background job handler: {name}")
        self.handlers[name] = (run, check)
        self.limits[name] = limit or (lambda bot: 1)

    def capacity(self, bot_id, plugin):
        """Current operator allowance across this bot's channels, not a tool argument."""
        bot = self.store.get("bots", bot_id) or {}
        limit = self.limits.get(plugin, lambda bot: 1)(bot)
        if type(limit) is not int or limit < 1:
            raise ControlError("Background job allowance must be a positive integer")
        counts = self.store.one(
            "SELECT count(*) AS outstanding_jobs, "
            "coalesce(sum(state IN ('queued','running')),0) AS active_jobs "
            "FROM background_jobs WHERE bot_id=? AND plugin=? "
            "AND (state IN ('queued','running') OR notification='pending')",
            (bot_id, plugin),
        )
        return {
            "max_parallel_jobs": limit,
            **counts,
            "pending_completions": counts["outstanding_jobs"] - counts["active_jobs"],
            "available_slots": max(0, limit - counts["outstanding_jobs"]),
        }

    def recover(self):
        # No paid POST is replayed after a crash or restore. Completed receipts
        # persist; already-claimed notifications are never replayed either.
        for job in self.store.rows("SELECT * FROM background_jobs WHERE state IN ('queued','running')"):
            self.finish(job, "interrupted", error="Runtime restarted before the job finished")
        self.cleanup()

    def cleanup(self):
        self.store.execute(
            "DELETE FROM background_jobs WHERE state NOT IN ('queued','running') "
            "AND notification<>'pending' AND updated_at<?",
            (time.time() - 7 * 86400,),
        )
        # Rolling history, not a permanent admission roadblock.
        for row in self.store.rows("SELECT DISTINCT bot_id FROM background_jobs"):
            self.store.execute(
                "DELETE FROM background_jobs WHERE id IN (SELECT id FROM background_jobs "
                "WHERE bot_id=? AND state NOT IN ('queued','running') AND notification<>'pending' "
                "ORDER BY updated_at DESC LIMIT -1 OFFSET 100)",
                (row["bot_id"],),
            )

    def get(self, job_id):
        return self.store.one("SELECT * FROM background_jobs WHERE id=?", (job_id,))

    def inventory(self, *, bot_id=None, plugin=None):
        rows = self.store.rows(
            "WITH owned AS (SELECT id,plugin,bot_id,channel_id,created_at,updated_at,deadline,state,error,notification,"
            "continuation_turn_id,origin_turn_id,json_extract(result,'$.metrics') AS metrics_json "
            "FROM background_jobs WHERE (? IS NULL OR bot_id=?) AND (? IS NULL OR plugin=?)) "
            "SELECT * FROM owned WHERE state IN ('queued','running') OR notification='pending' OR id IN ("
            "SELECT id FROM owned WHERE state NOT IN ('queued','running') AND notification<>'pending' "
            "ORDER BY created_at DESC LIMIT 50) ORDER BY created_at DESC",
            (bot_id, bot_id, plugin, plugin),
        )
        for row in rows:
            row["metrics"] = json.loads(row.pop("metrics_json") or "null")
        return self.registry.vault.redact(rows)

    def check(self, job):
        from .plugins import ToolContext

        bot = self.store.get("bots", job["bot_id"])
        if not bot or not bot["enabled"] or not self.store.get("settings", "global")["enabled"]:
            raise ControlError("Background job stopped: bot or council is paused")
        context = ToolContext(bot, job["channel_id"], job["origin_turn_id"], bot["role"] == "hortator")
        if not self.registry.allowed(job["plugin"], context) or job["plugin"] not in self.handlers:
            raise ControlError("Background job plugin is disabled or unavailable")
        if not self.engine.channel_allowed(bot, job["channel_id"]):
            raise ControlError("Background job channel is outside the bot's current scope")
        boundary = self.store.context_boundary(bot["id"], job["channel_id"])
        if boundary and job["created_at"] <= boundary["after_at"]:
            raise ControlError("Background job predates the bot's clean-slate boundary")
        self.handlers[job["plugin"]][1](job)
        return bot

    def submit(self, context, plugin, submission_id, payload, *, seconds, notify=True):
        payload = self.registry.vault.redact(payload)
        existing = self.store.one(
            "SELECT * FROM background_jobs WHERE bot_id=? AND channel_id=? AND plugin=? AND submission_id=?",
            (context.bot["id"], context.channel_id, plugin, submission_id),
        )
        if existing:
            if json.loads(existing["payload"]) != payload or bool(existing["notify"]) != notify:
                raise ControlError("Submission ID already belongs to another assignment; use a new ID")
            self.owned(existing["id"], context, plugin)
            return self.status(existing)
        if context.bot.get("background_completion"):
            raise ControlError(
                "A completion follow-up cannot launch more background jobs; answer or read results"
            )
        if self.closed or not self.background or self.background.group is None:
            raise ControlError("Background job service is not running")
        self.cleanup()
        capacity = self.capacity(context.bot["id"], plugin)
        if capacity["available_slots"] == 0:
            raise ControlError(
                f"Background job allowance reached for {plugin}: "
                f"{capacity['outstanding_jobs']}/{capacity['max_parallel_jobs']} outstanding, "
                f"{capacity['active_jobs']} active, {capacity['pending_completions']} pending completions, "
                "0 available slots. Wait, read a finished result or cancel existing work before starting another job."
            )
        now = time.time()
        job = dict(
            id=uid("bg_"),
            plugin=plugin,
            bot_id=context.bot["id"],
            channel_id=context.channel_id,
            origin_turn_id=context.turn_id,
            submission_id=submission_id,
            created_at=now,
            deadline=now + seconds,
            updated_at=now,
            state="queued",
            payload=dumps(payload),
            notify=int(notify),
            reply_to=context.bot.get("activation", {}).get("message_id"),
        )
        self.check(job)
        self.store.execute(
            "INSERT INTO background_jobs(id,plugin,bot_id,channel_id,origin_turn_id,submission_id,created_at,"
            "deadline,updated_at,state,payload,notify,reply_to) VALUES(:id,:plugin,:bot_id,:channel_id,"
            ":origin_turn_id,:submission_id,:created_at,:deadline,:updated_at,:state,:payload,:notify,:reply_to)",
            job,
        )
        self.emit(job, "queued", **self.capacity(job["bot_id"], plugin))
        self.tasks[job["id"]] = self.background.spawn(
            self.run(job), name=f"background-job:{job['id']}", bot_id=job["bot_id"]
        )
        self.tasks[job["id"]].add_done_callback(lambda _: self.tasks.pop(job["id"], None))
        return self.status(self.get(job["id"]))

    def emit(self, job, state, **fields):
        self.store.emit(
            "background_job." + state,
            {"job_id": job["id"], "plugin": job["plugin"], "channel_id": job["channel_id"], **fields},
            bot_id=job["bot_id"],
            turn_id=job["origin_turn_id"],
            level="warning" if state in ("failed", "cancelled", "interrupted") else "info",
        )

    def finish(self, job, state, *, result=None, error=None, **diagnostics):
        self.store.execute(
            "UPDATE background_jobs SET state=?,result=?,error=?,updated_at=?,notification=? WHERE id=?",
            (
                state,
                dumps(result) if result is not None else None,
                self.store.redact(error) if error else None,
                time.time(),
                "pending" if job["notify"] and state != "cancelled" else "none",
                job["id"],
            ),
        )
        self.emit(job, state, error=self.store.redact(error) if error else None, **diagnostics)

    async def run(self, job):
        try:
            async with asyncio.timeout(max(0, job["deadline"] - time.time())):
                self.check(job)
                self.store.execute(
                    "UPDATE background_jobs SET state='running',updated_at=? WHERE id=?",
                    (time.time(), job["id"]),
                )
                self.emit(job, "started")
                failure = None
                try:
                    result = await self.handlers[job["plugin"]][0](job)
                except JobResultError as exc:
                    result, failure = exc.result, error_text(exc)
                self.check(job)
                result = self.registry.vault.redact(result)
                if len(dumps(result).encode()) > RESULT_LIMIT:
                    raise ControlError("Background result exceeds the 2 MiB saved-result limit")
                self.finish(job, "failed" if failure else "completed", result=result, error=failure)
        except asyncio.CancelledError:
            self.finish(
                job,
                "cancelled",
                error="Background job cancelled by operator, configuration change or shutdown",
            )
            raise
        except TimeoutError:
            self.finish(
                job,
                "failed",
                error=f"Background job total deadline exceeded: {job['deadline'] - job['created_at']:g} seconds",
            )
        except Exception as exc:
            self.finish(
                job,
                "failed",
                error=error_text(exc),
                traceback=self.store.redact("".join(traceback.format_exception(exc)))[:12000],
            )
        finally:
            self.tasks.pop(job["id"], None)

    def owned(self, job_id, context, plugin):
        job = self.get(job_id)
        if not job or (job["bot_id"], job["channel_id"], job["plugin"]) != (
            context.bot["id"],
            context.channel_id,
            plugin,
        ):
            raise ControlError("Background job does not exist in this bot/channel scope", 404)
        self.check(job)
        return job

    def status(self, job):
        result = json.loads(job["result"]) if job.get("result") else {}
        return {
            "job_id": job["id"],
            "state": job["state"],
            "error": job.get("error"),
            "elapsed_seconds": round(
                (time.time() if job["state"] in ACTIVE else job["updated_at"]) - job["created_at"], 1
            ),
            "deadline": local_timestamp(job["deadline"], council_timezone(self.store)),
            "notification": job.get("notification", "none"),
            "notify_on_completion": bool(job["notify"]),
            "poll_after_seconds": 10 if job["state"] in ACTIVE else None,
            "metrics": result.get("metrics"),
            "next": {"operation": "read_result", "job_id": job["id"], "offset": 0, "length": 6000},
            "capacity": self.capacity(job["bot_id"], job["plugin"]),
        }

    def pending(self, bot):
        if self.closed:
            return None
        for job in self.store.rows(
            "SELECT * FROM background_jobs WHERE bot_id=? AND notification='pending' ORDER BY created_at",
            (bot["id"],),
        ):
            try:
                self.check(job)
            except ControlError as exc:
                self.store.execute(
                    "UPDATE background_jobs SET notification='revoked' WHERE id=?", (job["id"],)
                )
                self.emit(job, "notification_revoked", reason=str(exc))
                continue
            # Let the originating turn finish first. Never inject an unfinished
            # tool exchange or interrupt another conversation to report a job.
            origin = self.store.one("SELECT status FROM turns WHERE id=?", (job["origin_turn_id"],))
            if origin and origin["status"] == "running":
                continue
            return job

    def claim(self, job, turn_id):
        return (
            self.store.execute(
                "UPDATE background_jobs SET notification='claimed',continuation_turn_id=? WHERE id=? AND notification='pending'",
                (turn_id, job["id"]),
            ).rowcount
            == 1
        )

    def notification(self, job):
        payload = json.loads(job["payload"])
        return {
            **self.status(job),
            "assignment": payload.get("task", ""),
            "origin_turn_id": job["origin_turn_id"],
        }

    async def cancel(self, bot_ids, reason):
        bot_ids = set(bot_ids)
        selected = [
            job
            for job in self.store.rows(
                "SELECT id,bot_id,channel_id,plugin,origin_turn_id,notify FROM background_jobs "
                "WHERE state IN ('queued','running') OR notification='pending'"
            )
            if job["bot_id"] in bot_ids
        ]
        tasks = [self.tasks[job["id"]] for job in selected if job["id"] in self.tasks]
        await cancel_and_wait(*tasks)
        for job in selected:
            current = self.get(job["id"])
            if not current:
                continue
            if current["state"] in ACTIVE:
                self.finish(job, "cancelled", error=reason)
            self.store.execute(
                "UPDATE background_jobs SET notification='revoked' WHERE id=? AND notification='pending'",
                (job["id"],),
            )

    async def cancel_job(self, job, reason):
        task = self.tasks.get(job["id"])
        if task:
            await cancel_and_wait(task)
        if self.get(job["id"])["state"] in ACTIVE:
            self.finish(job, "cancelled", error=reason)
        self.store.execute(
            "UPDATE background_jobs SET notification='revoked' WHERE id=? AND notification='pending'",
            (job["id"],),
        )

    async def close(self):
        self.closed = True
        await self.cancel(
            [r["bot_id"] for r in self.store.rows("SELECT DISTINCT bot_id FROM background_jobs")], "Shutdown"
        )
