"""SQLite 存储层：建表、迁移、读写。

约定：写入一律走事务；对外只暴露 async 方法，内部用 to_thread 避免阻塞事件循环。
schema 版本见 SCHEMA_VERSION，升级走 _migrate()，不删库。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import MemoryItem, Message

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT    NOT NULL CHECK (role IN ('user','assistant','system')),
    content     TEXT    NOT NULL,
    channel     TEXT    NOT NULL DEFAULT 'cli',
    created_at  TEXT    NOT NULL,
    meta        TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    topic      TEXT,
    title      TEXT    NOT NULL,
    first_seen TEXT    NOT NULL,
    last_seen  TEXT    NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL CHECK (kind IN
                    ('fact','preference','event','relation','promise','emotion','self')),
    topic         TEXT,
    content       TEXT    NOT NULL,
    importance    INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    confidence    REAL    NOT NULL DEFAULT 0.8,
    event_time    TEXT,
    entities      TEXT    NOT NULL DEFAULT '[]',
    source_msg_id INTEGER REFERENCES messages(id),
    created_at    TEXT    NOT NULL,
    last_used_at  TEXT,
    use_count     INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    superseded_by INTEGER,
    due_at        TEXT,
    episode_id    INTEGER REFERENCES episodes(id)
);

CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    kind       TEXT,
    first_seen TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    PRIMARY KEY (memory_id, entity_id)
);

CREATE TABLE IF NOT EXISTS summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_msg_id INTEGER NOT NULL,
    to_msg_id   INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS experiences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    share_after TEXT NOT NULL,
    shared_at   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','shared','dropped')),
    mood        TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proactive_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    slot       TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id),
    UNIQUE(kind, slot)
);

CREATE TABLE IF NOT EXISTS heartbeat_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    action     TEXT NOT NULL,
    reason     TEXT,
    motivation REAL,
    gate       TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_summaries_end ON summaries(period_end);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """v1 → v4：记忆加 topic / superseded_by / due_at / episode_id；弃用 FTS。"""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(memories)")}
        if "topic" not in columns:
            self._conn.execute("ALTER TABLE memories ADD COLUMN topic TEXT")
        if "superseded_by" not in columns:
            self._conn.execute("ALTER TABLE memories ADD COLUMN superseded_by INTEGER")
        if "due_at" not in columns:
            self._conn.execute("ALTER TABLE memories ADD COLUMN due_at TEXT")
        if "episode_id" not in columns:
            self._conn.execute("ALTER TABLE memories ADD COLUMN episode_id INTEGER")
        self._conn.execute("DROP TABLE IF EXISTS memories_fts")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- 同步实现

    def _insert_message(self, role: str, content: str, channel: str, created_at: str, meta: dict[str, Any]) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages(role, content, channel, created_at, meta) VALUES(?,?,?,?,?)",
                (role, content, channel, created_at, json.dumps(meta, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def _recent_messages(self, limit: int, roles: tuple[str, ...] | None = None) -> list[Message]:
        with self._lock:
            if roles:
                marks = ",".join("?" for _ in roles)
                rows = self._conn.execute(
                    f"SELECT id, role, content, created_at FROM messages WHERE role IN ({marks})"
                    " ORDER BY id DESC LIMIT ?",
                    (*roles, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, role, content, created_at FROM messages ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            Message(role=r["role"], content=r["content"], id=r["id"], created_at=r["created_at"])
            for r in reversed(rows)
        ]

    def _count_messages(self, role: str | None = None) -> int:
        with self._lock:
            if role:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM messages WHERE role=?", (role,)).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"])

    def _count_memories(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return int(row["n"])

    def _upsert_entity(self, name: str, now: str) -> int:
        row = self._conn.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
        if row:
            return int(row["id"])
        cur = self._conn.execute(
            "INSERT INTO entities(name, first_seen) VALUES(?,?)", (name, now)
        )
        return int(cur.lastrowid)

    def _add_memory(
        self,
        *,
        kind: str,
        topic: str | None,
        content: str,
        importance: int,
        confidence: float,
        event_time: str | None,
        entities: list[str],
        source_msg_id: int | None,
        now: str,
        due_at: str | None = None,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO memories(kind, topic, content, importance, confidence, event_time,"
                " entities, source_msg_id, created_at, due_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    topic,
                    content,
                    max(1, min(5, int(importance))),
                    float(confidence),
                    event_time,
                    json.dumps(entities, ensure_ascii=False),
                    source_msg_id,
                    now,
                    due_at,
                ),
            )
            memory_id = int(cur.lastrowid)
            for name in entities:
                entity_id = self._upsert_entity(name, now)
                self._conn.execute(
                    "INSERT OR IGNORE INTO memory_entities(memory_id, entity_id) VALUES(?,?)",
                    (memory_id, entity_id),
                )
            return memory_id

    def _mark_superseded(self, old_ids: list[int], new_id: int) -> int:
        if not old_ids:
            return 0
        marks = ",".join("?" for _ in old_ids)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE memories SET superseded_by=? WHERE id IN ({marks}) AND superseded_by IS NULL",
                (new_id, *old_ids),
            )
            return cur.rowcount

    def _all_memories(self) -> list[MemoryItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.id, m.kind, m.topic, m.content, m.importance, m.confidence,"
                " m.event_time, m.created_at, m.entities, m.superseded_by, m.last_used_at,"
                " m.use_count, m.source_msg_id, m.episode_id,"
                " COALESCE(e.count, 1) AS episode_count,"
                " COALESCE(e.last_seen, '') AS episode_last_seen"
                " FROM memories m LEFT JOIN episodes e ON e.id = m.episode_id"
                " WHERE m.status='active'"
            ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def _touch_memories(self, ids: list[int], now: str) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE memories SET last_used_at=?, use_count=use_count+1 WHERE id IN ({marks})",
                (now, *ids),
            )

    def _promises_due(self, now_iso: str, floor_iso: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, due_at FROM memories"
                " WHERE kind='promise' AND status='active' AND due_at IS NOT NULL"
                " AND due_at <= ? AND due_at >= ? ORDER BY due_at LIMIT 5",
                (now_iso, floor_iso),
            ).fetchall()
        return [dict(r) for r in rows]

    def _archive_memory(self, memory_id: int) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE memories SET status='archived' WHERE id=? AND status='active'",
                (int(memory_id),),
            )
            return int(cur.rowcount)

    def _stale_memories(self, cutoff: str, max_importance: int) -> list[MemoryItem]:
        """久没被用到、又不重要的记忆。承诺不算在内。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.id, m.kind, m.topic, m.content, m.importance, m.confidence,"
                " m.event_time, m.created_at, m.entities, m.superseded_by, m.last_used_at,"
                " m.use_count, m.source_msg_id, m.episode_id,"
                " COALESCE(e.count, 1) AS episode_count,"
                " COALESCE(e.last_seen, '') AS episode_last_seen"
                " FROM memories m LEFT JOIN episodes e ON e.id = m.episode_id"
                " WHERE m.status='active' AND m.importance <= ? AND m.kind != 'promise'"
                " AND COALESCE(m.last_used_at, m.created_at) < ?",
                (int(max_importance), cutoff),
            ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def _messages_around(self, message_id: int, span: int) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, created_at FROM messages"
                " WHERE id BETWEEN ? AND ? AND role IN ('user','assistant') ORDER BY id",
                (max(1, message_id - span), message_id + span),
            ).fetchall()
        return [
            Message(role=r["role"], content=r["content"], id=r["id"], created_at=r["created_at"])
            for r in rows
        ]

    def _list_memories(self, limit: int) -> list[MemoryItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.id, m.kind, m.topic, m.content, m.importance, m.confidence,"
                " m.event_time, m.created_at, m.entities, m.superseded_by, m.last_used_at,"
                " m.use_count, m.source_msg_id, m.episode_id, m.status,"
                " COALESCE(e.count, 1) AS episode_count,"
                " COALESCE(e.last_seen, '') AS episode_last_seen"
                " FROM memories m LEFT JOIN episodes e ON e.id = m.episode_id"
                " WHERE m.status='active'"
                " ORDER BY m.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_memory(r) for r in rows]

    # ---------------------------------------------------------------- 事件归并

    def _create_episode(self, kind: str, topic: str | None, title: str, now: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO episodes(kind, topic, title, first_seen, last_seen, count)"
                " VALUES(?,?,?,?,?,1)",
                (kind, topic, title[:80], now, now),
            )
            return int(cur.lastrowid)

    def _touch_episode(self, episode_id: int, now: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE episodes SET count=count+1, last_seen=? WHERE id=?",
                (now, int(episode_id)),
            )

    def _set_memory_episode(self, memory_id: int, episode_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE memories SET episode_id=? WHERE id=?", (int(episode_id), int(memory_id))
            )

    def _seed_memories(self, rows: Iterable[dict[str, Any]], now: str) -> int:
        with self._lock:
            if self._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]:
                return 0
        inserted = 0
        for row in rows:
            self._add_memory(
                kind=str(row.get("kind", "fact")),
                topic=row.get("topic"),
                content=str(row["content"]),
                importance=int(row.get("importance", 3)),
                confidence=float(row.get("confidence", 0.9)),
                event_time=row.get("event_time"),
                entities=[str(e) for e in (row.get("entities") or [])],
                source_msg_id=None,
                now=now,
            )
            inserted += 1
        return inserted

    def _add_summary(self, period_start: str, period_end: str, content: str, now: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO summaries(period_start, period_end, content, created_at) VALUES(?,?,?,?)",
                (period_start, period_end, content, now),
            )
            return int(cur.lastrowid)

    def _summaries(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, period_start, period_end, content, created_at"
                " FROM summaries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _messages_after(self, after_id: int, limit: int) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, created_at FROM messages"
                " WHERE id > ? AND role IN ('user','assistant') ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [
            Message(role=r["role"], content=r["content"], id=r["id"], created_at=r["created_at"])
            for r in rows
        ]

    def _add_rollup(self, from_id: int, to_id: int, content: str, now: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO rollups(from_msg_id, to_msg_id, content, created_at) VALUES(?,?,?,?)",
                (from_id, to_id, content, now),
            )
            return int(cur.lastrowid)

    def _rollups(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, from_msg_id, to_msg_id, content, created_at"
                " FROM rollups ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _last_rollup_to_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(to_msg_id) AS m FROM rollups").fetchone()
        return int(row["m"] or 0)

    # ---------------------------------------------------------------- 延迟分享

    def _add_experience(self, content: str, occurred_at: str, share_after: str, mood: str | None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO experiences(content, occurred_at, share_after, mood) VALUES(?,?,?,?)",
                (content, occurred_at, share_after, mood),
            )
            return int(cur.lastrowid)

    def _experiences_for_day(self, day_prefix: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, occurred_at, share_after, status, mood"
                " FROM experiences WHERE occurred_at LIKE ? ORDER BY id",
                (f"{day_prefix}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def _expire_experiences(self, before_iso: str) -> int:
        # 用 dropped 表示"过期没机会说了"——表里本来就有这个状态，不必改 schema
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE experiences SET status='dropped'"
                " WHERE status='pending' AND share_after < ?",
                (before_iso,),
            )
            return int(cur.rowcount)

    def _drop_experience(self, experience_id: int) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE experiences SET status='dropped' WHERE id=? AND status='pending'",
                (int(experience_id),),
            )
            return int(cur.rowcount)

    def _experiences_since(self, start_iso: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, occurred_at, share_after, status, mood"
                " FROM experiences WHERE occurred_at >= ? AND shared_at IS NULL"
                " ORDER BY occurred_at DESC LIMIT ?",
                (start_iso, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def _due_experiences(self, now_iso: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, occurred_at, share_after, status, mood"
                " FROM experiences WHERE status='pending' AND share_after <= ?"
                " ORDER BY share_after",
                (now_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _mark_experiences_shared(self, ids: list[int], now_iso: str) -> int:
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE experiences SET status='shared', shared_at=? WHERE id IN ({marks})",
                (now_iso, *ids),
            )
            return cur.rowcount

    def _count_proactive(self, day_prefix: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM proactive_log WHERE sent_at LIKE ?",
                (f"{day_prefix}%",),
            ).fetchone()
        return int(row["n"])

    def _has_proactive_slot(self, kind: str, slot: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM proactive_log WHERE kind=? AND slot=? LIMIT 1", (kind, slot)
            ).fetchone()
        return row is not None

    def _last_proactive_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT sent_at FROM proactive_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["sent_at"] if row else None

    def _log_proactive(self, kind: str, slot: str, sent_at: str, message_id: int | None) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO proactive_log(kind, slot, sent_at, message_id) VALUES(?,?,?,?)",
                (kind, slot, sent_at, message_id),
            )
            return cur.rowcount > 0

    def _log_heartbeat(self, at: str, action: str, reason: str, motivation: float, gate: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO heartbeat_log(at, action, reason, motivation, gate) VALUES(?,?,?,?,?)",
                (at, action, reason, motivation, gate),
            )

    def _recent_heartbeats(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, action, reason, motivation, gate FROM heartbeat_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _last_user_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["created_at"] if row else None

    def _get_state(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def _set_state(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # ---------------------------------------------------------------- 异步接口

    async def insert_message(
        self, role: str, content: str, channel: str = "cli", created_at: str = "", meta: dict[str, Any] | None = None
    ) -> int:
        return await asyncio.to_thread(self._insert_message, role, content, channel, created_at, meta or {})

    async def recent_messages(self, limit: int, roles: tuple[str, ...] | None = None) -> list[Message]:
        return await asyncio.to_thread(self._recent_messages, limit, roles)

    async def count_messages(self, role: str | None = None) -> int:
        return await asyncio.to_thread(self._count_messages, role)

    async def count_memories(self) -> int:
        return await asyncio.to_thread(self._count_memories)

    async def add_memory(self, **kwargs: Any) -> int:
        return await asyncio.to_thread(self._add_memory, **kwargs)

    async def mark_superseded(self, old_ids: list[int], new_id: int) -> int:
        return await asyncio.to_thread(self._mark_superseded, old_ids, new_id)

    async def all_memories(self) -> list[MemoryItem]:
        return await asyncio.to_thread(self._all_memories)

    async def touch_memories(self, ids: list[int], now: str) -> None:
        await asyncio.to_thread(self._touch_memories, ids, now)

    async def list_memories(self, limit: int = 20) -> list[MemoryItem]:
        return await asyncio.to_thread(self._list_memories, limit)

    async def promises_due(self, now_iso: str, floor_iso: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._promises_due, now_iso, floor_iso)

    async def expire_experiences(self, before_iso: str) -> int:
        return await asyncio.to_thread(self._expire_experiences, before_iso)

    async def drop_experience(self, experience_id: int) -> int:
        return await asyncio.to_thread(self._drop_experience, experience_id)

    async def experiences_since(self, start_iso: str, limit: int = 5) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._experiences_since, start_iso, limit)

    async def archive_memory(self, memory_id: int) -> int:
        return await asyncio.to_thread(self._archive_memory, memory_id)

    # ---------------------------------------------------------------- 备份

    def _backup_to(self, dest: Path) -> None:
        # 用 sqlite 自己的 backup，WAL 模式下也一致；不要直接拷文件
        with self._lock:
            target = sqlite3.connect(dest)
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def _prune_backups(self, dest_dir: Path, keep: int) -> None:
        files = sorted(dest_dir.glob("aigirl-*.db"))
        for old in files[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)

    async def backup(self, dest_dir: Path, keep: int = 7, stamp: str | None = None) -> Path:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"aigirl-{stamp}.db"
        await asyncio.to_thread(self._backup_to, dest)
        await asyncio.to_thread(self._prune_backups, dest_dir, keep)
        return dest

    async def stale_memories(self, cutoff: str, max_importance: int) -> list[MemoryItem]:
        return await asyncio.to_thread(self._stale_memories, cutoff, max_importance)

    async def messages_around(self, message_id: int, span: int = 2) -> list[Message]:
        return await asyncio.to_thread(self._messages_around, message_id, span)

    async def create_episode(self, kind: str, topic: str | None, title: str, now: str) -> int:
        return await asyncio.to_thread(self._create_episode, kind, topic, title, now)

    async def touch_episode(self, episode_id: int, now: str) -> None:
        await asyncio.to_thread(self._touch_episode, episode_id, now)

    async def set_memory_episode(self, memory_id: int, episode_id: int) -> None:
        await asyncio.to_thread(self._set_memory_episode, memory_id, episode_id)

    async def seed_memories(self, rows: Iterable[dict[str, Any]], now: str) -> int:
        return await asyncio.to_thread(self._seed_memories, rows, now)

    async def add_summary(self, period_start: str, period_end: str, content: str, now: str) -> int:
        return await asyncio.to_thread(self._add_summary, period_start, period_end, content, now)

    async def summaries(self, limit: int = 3) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._summaries, limit)

    async def add_experience(self, content: str, occurred_at: str, share_after: str, mood: str | None = None) -> int:
        return await asyncio.to_thread(self._add_experience, content, occurred_at, share_after, mood)

    async def experiences_for_day(self, day_prefix: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._experiences_for_day, day_prefix)

    async def due_experiences(self, now_iso: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._due_experiences, now_iso)

    async def mark_experiences_shared(self, ids: list[int], now_iso: str) -> int:
        return await asyncio.to_thread(self._mark_experiences_shared, ids, now_iso)

    async def messages_after(self, after_id: int, limit: int = 60) -> list[Message]:
        return await asyncio.to_thread(self._messages_after, after_id, limit)

    async def add_rollup(self, from_id: int, to_id: int, content: str, now: str) -> int:
        return await asyncio.to_thread(self._add_rollup, from_id, to_id, content, now)

    async def rollups(self, limit: int = 3) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._rollups, limit)

    async def last_rollup_to_id(self) -> int:
        return await asyncio.to_thread(self._last_rollup_to_id)

    async def count_proactive(self, day_prefix: str) -> int:
        return await asyncio.to_thread(self._count_proactive, day_prefix)

    async def last_proactive_at(self) -> str | None:
        return await asyncio.to_thread(self._last_proactive_at)

    async def log_proactive(self, kind: str, slot: str, sent_at: str, message_id: int | None) -> bool:
        return await asyncio.to_thread(self._log_proactive, kind, slot, sent_at, message_id)

    async def has_proactive_slot(self, kind: str, slot: str) -> bool:
        return await asyncio.to_thread(self._has_proactive_slot, kind, slot)

    async def log_heartbeat(self, at: str, action: str, reason: str, motivation: float, gate: str) -> None:
        await asyncio.to_thread(self._log_heartbeat, at, action, reason, motivation, gate)

    async def recent_heartbeats(self, limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._recent_heartbeats, limit)

    async def last_user_at(self) -> str | None:
        return await asyncio.to_thread(self._last_user_at)

    async def get_state(self, key: str, default: Any = None) -> Any:
        value = await asyncio.to_thread(self._get_state, key)
        return default if value is None else value

    async def set_state(self, key: str, value: Any) -> None:
        await asyncio.to_thread(self._set_state, key, value)


def _row_to_memory(row: sqlite3.Row) -> MemoryItem:
    try:
        entities = json.loads(row["entities"] or "[]")
    except json.JSONDecodeError:
        entities = []
    keys = row.keys()
    return MemoryItem(
        id=int(row["id"]),
        kind=str(row["kind"]),
        topic=row["topic"],
        content=str(row["content"]),
        importance=int(row["importance"]),
        confidence=float(row["confidence"]),
        event_time=row["event_time"],
        created_at=str(row["created_at"]),
        entities=[str(e) for e in entities],
        superseded_by=row["superseded_by"],
        last_used_at=row["last_used_at"],
        use_count=int(row["use_count"]),
        source_msg_id=row["source_msg_id"] if "source_msg_id" in keys else None,
        episode_id=row["episode_id"] if "episode_id" in keys else None,
        episode_count=int(row["episode_count"]) if "episode_count" in keys else 1,
        episode_last_seen=str(row["episode_last_seen"]) if "episode_last_seen" in keys else "",
    )
