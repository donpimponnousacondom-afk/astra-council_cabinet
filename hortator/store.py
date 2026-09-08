from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path


def uid(prefix=""):
    return prefix + uuid.uuid4().hex[:20]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Store:
    """One event-loop writer. Short WAL transactions never span network awaits."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.redact = lambda value: value
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO schema_version VALUES(1);
        CREATE TABLE IF NOT EXISTS entities (
          kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
          updated_at REAL NOT NULL, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS entity_tombstones (
          kind TEXT NOT NULL,id TEXT NOT NULL,deleted_at REAL NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY,guild_id TEXT,parent_id TEXT);
        INSERT OR IGNORE INTO schema_version VALUES(2);
        CREATE TABLE IF NOT EXISTS secrets (scope TEXT PRIMARY KEY, value BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_sessions (
          token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, at REAL NOT NULL,
          kind TEXT NOT NULL, level TEXT NOT NULL, bot_id TEXT, turn_id TEXT, request_id TEXT, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS event_turn ON events(turn_id,seq);
        CREATE INDEX IF NOT EXISTS event_bot ON events(bot_id,seq);
        CREATE TABLE IF NOT EXISTS messages (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, discord_id TEXT UNIQUE NOT NULL, channel_id TEXT NOT NULL,
          room_id TEXT, author_id TEXT NOT NULL, author_name TEXT NOT NULL, bot_id TEXT,
          content TEXT NOT NULL, at REAL NOT NULL, reply_to TEXT, attachments TEXT NOT NULL DEFAULT '[]',
          deleted INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS message_channel ON messages(channel_id,seq);
        CREATE TABLE IF NOT EXISTS contexts (
          bot_id TEXT NOT NULL, channel_id TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
          checkpoint INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0,
          estimated_tokens INTEGER NOT NULL DEFAULT 0, compactions INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL, PRIMARY KEY(bot_id,channel_id));
        CREATE TABLE IF NOT EXISTS memories (
          bot_id TEXT NOT NULL, channel_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
          updated_at REAL NOT NULL, PRIMARY KEY(bot_id,channel_id,key));
        CREATE TABLE IF NOT EXISTS bot_runtime (
          bot_id TEXT PRIMARY KEY, next_at REAL NOT NULL DEFAULT 0, last_sent REAL NOT NULL DEFAULT 0,
          gateway_status TEXT NOT NULL DEFAULT 'offline', error TEXT, heartbeat_ms REAL);
        CREATE TABLE IF NOT EXISTS provider_health (
          provider_id TEXT PRIMARY KEY, consecutive_failures INTEGER NOT NULL DEFAULT 0,
          circuit_until REAL NOT NULL DEFAULT 0, last_error TEXT, last_success REAL);
        CREATE TABLE IF NOT EXISTS turns (
          id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, channel_id TEXT NOT NULL,
          profile_id TEXT NOT NULL, provider_id TEXT NOT NULL, model TEXT NOT NULL,
          started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL, decision TEXT,
          error TEXT, trigger TEXT NOT NULL, revision INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS turn_bot ON turns(bot_id,started_at);
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, bot_id TEXT NOT NULL, provider_id TEXT NOT NULL,
          profile_id TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL, started_at REAL NOT NULL,
          ended_at REAL, ttft_ms REAL, first_visible_ms REAL, duration_ms REAL,
          input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cached_tokens INTEGER,
          cost REAL, cost_source TEXT, usage TEXT, status TEXT NOT NULL, error TEXT,
          body TEXT NOT NULL, context TEXT NOT NULL, response TEXT, http_status INTEGER);
        CREATE INDEX IF NOT EXISTS request_bot ON requests(bot_id,started_at);
        CREATE INDEX IF NOT EXISTS request_turn ON requests(turn_id,started_at);
        CREATE TABLE IF NOT EXISTS request_diagnostics (
          request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
          body TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (
          id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, bot_id TEXT NOT NULL, channel_id TEXT NOT NULL,
          content TEXT NOT NULL, reply_to TEXT, artifacts TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
          created_at REAL NOT NULL, sent_at REAL, discord_id TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS artifacts (
          id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, turn_id TEXT NOT NULL,
          filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL);
        """)
        if "addressing" not in {row["name"] for row in self.rows("PRAGMA table_info(messages)")}:
            self.execute("ALTER TABLE messages ADD COLUMN addressing TEXT NOT NULL DEFAULT '{}'")
        if "retry_until" not in {row["name"] for row in self.rows("PRAGMA table_info(bot_runtime)")}:
            self.execute("ALTER TABLE bot_runtime ADD COLUMN retry_until REAL NOT NULL DEFAULT 0")
        self.execute(
            "CREATE TABLE IF NOT EXISTS human_attention_claims (bot_id TEXT NOT NULL, message_id TEXT NOT NULL, "
            "turn_id TEXT NOT NULL, claimed_at REAL NOT NULL, PRIMARY KEY(bot_id,message_id))"
        )
        path.chmod(0o600)

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def execute(self, sql, args=()):
        return self.db.execute(sql, args)

    def list(self, kind):
        return [
            {**json.loads(row["body"]), "revision": row["revision"]}
            for row in self.rows("SELECT body,revision FROM entities WHERE kind=? ORDER BY id", (kind,))
        ]

    def get(self, kind, entity_id):
        row = self.one("SELECT body,revision FROM entities WHERE kind=? AND id=?", (kind, entity_id))
        return {**json.loads(row["body"]), "revision": row["revision"]} if row else None

    def put(self, kind, body):
        self.execute(
            """INSERT INTO entities(kind,id,body,updated_at) VALUES(?,?,?,?)
          ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body, revision=entities.revision+1,
          updated_at=excluded.updated_at""",
            (kind, body["id"], dumps(body), time.time()),
        )
        return self.get(kind, body["id"])

    def emit(self, kind, data=None, *, bot_id=None, turn_id=None, request_id=None, level="info"):
        event = dict(
            id=uid("ev_"),
            at=time.time(),
            kind=kind,
            level=level,
            bot_id=bot_id,
            turn_id=turn_id,
            request_id=request_id,
            data=self.redact(data or {}),
        )
        cur = self.execute(
            """INSERT INTO events(id,at,kind,level,bot_id,turn_id,request_id,data)
          VALUES(:id,:at,:kind,:level,:bot_id,:turn_id,:request_id,:data)""",
            {**event, "data": dumps(event["data"])},
        )
        event["seq"] = cur.lastrowid
        logging.getLogger("hortator.events").log(
            getattr(logging, level.upper(), logging.INFO), kind, extra={"council_event": event}
        )
        return event

    def events(self, *, after=0, before=None, limit=100, bot_id=None, turn_id=None, level=None):
        where, args = ["seq> ?"], [after]
        for field, val in (("bot_id", bot_id), ("turn_id", turn_id), ("level", level)):
            if val:
                where.append(field + "=?")
                args.append(val)
        if before:
            where.append("seq<?")
            args.append(before)
        order = "ASC" if after else "DESC"
        rows = self.rows(
            f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY seq {order} LIMIT ?",
            (*args, min(limit, 500)),
        )
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    def context(self, bot_id, channel_id):
        self.execute(
            "INSERT OR IGNORE INTO contexts(bot_id,channel_id,updated_at) VALUES(?,?,?)",
            (bot_id, channel_id, time.time()),
        )
        return self.one("SELECT * FROM contexts WHERE bot_id=? AND channel_id=?", (bot_id, channel_id))

    def transcript(self, channel_id, after=0, through=None, limit=10000):
        rows = self.rows(
            """SELECT * FROM messages WHERE channel_id=? AND seq>? AND seq<=?
          ORDER BY seq LIMIT ?""",
            (channel_id, after, through or 9223372036854775807, limit),
        )
        for row in rows:
            row["attachments"] = json.loads(row["attachments"])
            row["addressing"] = json.loads(row["addressing"])
        return rows

    def ingest(
        self,
        *,
        discord_id,
        channel_id,
        author_id,
        author_name,
        content,
        at=None,
        room_id=None,
        bot_id=None,
        reply_to=None,
        attachments=None,
        guild_id=None,
        parent_id=None,
        addressing=None,
    ):
        room = self.get("rooms", room_id) if room_id else None
        if guild_id is None and room and room["channel_id"] == channel_id:
            guild_id = room["guild_id"]
        self.execute(
            "INSERT INTO channels(id,guild_id,parent_id) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET guild_id=coalesce(excluded.guild_id,channels.guild_id),parent_id=coalesce(excluded.parent_id,channels.parent_id)",
            (channel_id, guild_id, parent_id),
        )
        content = self.redact(content)
        cur = self.execute(
            """INSERT OR IGNORE INTO messages(discord_id,channel_id,room_id,author_id,
          author_name,bot_id,content,at,reply_to,attachments,addressing) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                discord_id,
                channel_id,
                room_id,
                author_id,
                author_name,
                bot_id,
                content,
                at or time.time(),
                reply_to,
                dumps(self.redact(attachments or [])),
                dumps(self.redact(addressing or {"author_kind": "bot" if bot_id else "unknown"})),
            ),
        )
        if cur.rowcount:
            self.emit(
                "message.received",
                {
                    "message_id": discord_id,
                    "channel_id": channel_id,
                    "author_name": author_name,
                    "content": content,
                    "seq": cur.lastrowid,
                    "addressing": self.redact(addressing or {}),
                },
                bot_id=bot_id,
            )
        return bool(cur.rowcount)

    def runtime(self, bot_id):
        self.execute("INSERT OR IGNORE INTO bot_runtime(bot_id) VALUES(?)", (bot_id,))
        return self.one("SELECT * FROM bot_runtime WHERE bot_id=?", (bot_id,))

    def health(self, provider_id):
        self.execute("INSERT OR IGNORE INTO provider_health(provider_id) VALUES(?)", (provider_id,))
        return self.one("SELECT * FROM provider_health WHERE provider_id=?", (provider_id,))

    def recover(self):
        self.execute("UPDATE bot_runtime SET gateway_status='offline',heartbeat_ms=NULL")
        for row in self.rows("SELECT * FROM outbox WHERE status IN ('pending','sending')"):
            status = "unknown" if row["status"] == "sending" else "suppressed"
            self.execute(
                "UPDATE outbox SET status=?,error='Process restarted before delivery was confirmed' WHERE id=?",
                (status, row["id"]),
            )
            self.emit(
                "delivery." + status,
                {"outbox_id": row["id"], "reason": "process_restart"},
                bot_id=row["bot_id"],
                turn_id=row["turn_id"],
                level="warning",
            )
        self.execute(
            "UPDATE turns SET status='interrupted',ended_at=?,error='Process restarted' WHERE ended_at IS NULL",
            (time.time(),),
        )
        self.execute(
            "UPDATE requests SET status='interrupted',ended_at=?,error='Process restarted; usage may be incomplete' WHERE ended_at IS NULL",
            (time.time(),),
        )
        self.execute(
            "UPDATE request_diagnostics SET body=json_set(body,'$.status','interrupted'),updated_at=? "
            "WHERE json_extract(body,'$.status')='running' AND request_id IN (SELECT id FROM requests WHERE status='interrupted')",
            (time.time(),),
        )

    def close(self):
        self.db.close()
