"""Research-specific dispatch budgets, shared by every completion wave of a task."""

from __future__ import annotations

import time

from .models import ControlError

ID = "research_assistant"


class ResearchCycles:
    def __init__(self, researcher):
        self.researcher, self.store = researcher, researcher.store
        self.store.execute("""CREATE TABLE IF NOT EXISTS research_batches (
            bot_id TEXT NOT NULL, channel_id TEXT NOT NULL, root_turn_id TEXT NOT NULL,
            origin_turn_id TEXT NOT NULL, batch_number INTEGER NOT NULL,
            max_batches INTEGER NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(bot_id,channel_id,origin_turn_id),
            UNIQUE(bot_id,channel_id,root_turn_id,batch_number))""")

    def batch(self, bot_id, channel_id, turn_id):
        return self.store.one(
            "SELECT * FROM research_batches WHERE bot_id=? AND channel_id=? AND origin_turn_id=?",
            (bot_id, channel_id, turn_id),
        )

    def limit(self, bot_id):
        bot = self.store.get("bots", bot_id) or {}
        value = self.researcher.configuration(bot)["max_research_batches"]
        if type(value) is not int or value < 1:
            raise ControlError("Research batch allowance must be a positive integer")
        return value

    def summary(self, batch):
        used = self.store.one(
            "SELECT max(batch_number) AS n FROM research_batches "
            "WHERE bot_id=? AND channel_id=? AND root_turn_id=?",
            (batch["bot_id"], batch["channel_id"], batch["root_turn_id"]),
        )["n"]
        maximum = min(batch["max_batches"], self.limit(batch["bot_id"]))
        return {
            "root_turn_id": batch["root_turn_id"],
            "batch_number": batch["batch_number"],
            "batches_used": used,
            "max_batches": maximum,
            "remaining_batches": max(0, maximum - used),
        }

    def describe(self, job):
        batch = self.batch(job["bot_id"], job["channel_id"], job["origin_turn_id"])
        return {"research_cycle": self.summary(batch)} if batch else {}

    def parent(self, context):
        # The model cannot choose an ancestor. Only jobs durably claimed by this
        # running completion turn can confer a follow-up research allowance.
        parents = self.store.rows(
            "SELECT j.* FROM background_jobs j JOIN turns t ON t.id=j.continuation_turn_id "
            "WHERE j.bot_id=? AND j.channel_id=? AND j.plugin=? AND j.continuation_turn_id=? "
            "AND j.notification='claimed' AND t.bot_id=j.bot_id AND t.channel_id=j.channel_id "
            "AND t.status='running' AND t.trigger='background_completion'",
            (context.bot["id"], context.channel_id, ID, context.turn_id),
        )
        if not parents:
            turn = self.store.one("SELECT trigger FROM turns WHERE id=?", (context.turn_id,))
            if context.bot.get("background_completion") or (
                turn and turn["trigger"] == "background_completion"
            ):
                raise ControlError(
                    "A completion follow-up cannot launch research without its own claimed research results"
                )
            return None
        batches = []
        for parent in parents:
            self.researcher.jobs.check(parent)
            batch = self.batch(parent["bot_id"], parent["channel_id"], parent["origin_turn_id"])
            if not batch:
                raise ControlError(
                    "This research predates saved cycle budgets; report its result. "
                    "A new conversational request can start a new research task."
                )
            batches.append(batch)
        if len({b["root_turn_id"] for b in batches}) != 1:
            raise ControlError("Completion results do not belong to one research task")
        root = self.batch(context.bot["id"], context.channel_id, batches[0]["root_turn_id"])
        if not root:
            raise ControlError("Research task budget is no longer available; report saved findings")
        boundary = self.store.context_boundary(context.bot["id"], context.channel_id)
        if boundary and root["created_at"] <= boundary["after_at"]:
            raise ControlError("Research task predates the bot's clean-slate boundary")
        return root, parents[0]

    def context_status(self, context):
        parent = self.parent(context)
        current = self.batch(context.bot["id"], context.channel_id, context.turn_id)
        if current:
            return self.summary(current)
        if parent:
            return self.summary(parent[0])
        maximum = self.limit(context.bot["id"])
        return {"batches_used": 0, "max_batches": maximum, "remaining_batches": maximum}

    def cleanup(self):
        # Keep every budget entry while any retained job still references that
        # task, even if its initial batch has rolled out of job history.
        self.store.execute(
            "DELETE FROM research_batches WHERE NOT EXISTS ("
            "SELECT 1 FROM research_batches sibling JOIN background_jobs j "
            "ON j.bot_id=sibling.bot_id AND j.channel_id=sibling.channel_id "
            "AND j.origin_turn_id=sibling.origin_turn_id AND j.plugin=? "
            "WHERE sibling.bot_id=research_batches.bot_id AND sibling.channel_id=research_batches.channel_id "
            "AND sibling.root_turn_id=research_batches.root_turn_id)",
            (ID,),
        )

    def admit(self, context, job):
        # Called inside the background service's job-insertion transaction.
        self.cleanup()
        parent = self.parent(context)
        current = self.batch(job["bot_id"], job["channel_id"], job["origin_turn_id"])
        if parent:
            root, parent_job = parent
            job["reply_to"] = parent_job["reply_to"]
            if current and current["root_turn_id"] != root["root_turn_id"]:
                raise ControlError("Research dispatch belongs to another task")
            if current:
                if current["batch_number"] > min(root["max_batches"], self.limit(job["bot_id"])):
                    raise ControlError("Research batch allowance was lowered; report saved findings")
                return  # Additional researchers in this dispatch share its one batch.
            budget = self.summary(root)
            if budget["remaining_batches"] == 0:
                raise ControlError(
                    f"Research task batch budget exhausted: {budget['batches_used']}/{budget['max_batches']} "
                    "dispatch batches used, including the initial batch. Report findings and remaining gaps; "
                    "existing researchers continue. A completion wake-up does not renew this budget."
                )
            root_id, number, maximum = root["root_turn_id"], budget["batches_used"] + 1, root["max_batches"]
        elif current:
            return
        else:
            root_id, number, maximum = context.turn_id, 1, self.limit(job["bot_id"])
        self.store.execute(
            "INSERT INTO research_batches VALUES(?,?,?,?,?,?,?)",
            (job["bot_id"], job["channel_id"], root_id, context.turn_id, number, maximum, time.time()),
        )
