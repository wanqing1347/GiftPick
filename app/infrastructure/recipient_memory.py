"""SQLite 收礼人记忆存储，按买家与精确标签隔离并使用版本 CAS。"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.domain.buyer.preference import MemoryConflict
from app.domain.buyer.recipient_memory import RecipientMemory, normalize_recipient_label


class RecipientMemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recipient_memories (
                    memory_id TEXT PRIMARY KEY,
                    buyer_id TEXT NOT NULL,
                    recipient_label TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    created_at TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    UNIQUE(buyer_id, recipient_label, channel, statement)
                );
                CREATE INDEX IF NOT EXISTS recipient_memory_buyer
                    ON recipient_memories(buyer_id);
                CREATE INDEX IF NOT EXISTS recipient_memory_buyer_label
                    ON recipient_memories(buyer_id, recipient_label);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _memory(row) -> RecipientMemory:
        return RecipientMemory(**dict(row))

    async def list_for_recipient(self, buyer_id: str, recipient_label: str) -> list[RecipientMemory]:
        label = normalize_recipient_label(recipient_label)

        def read():
            with self._db() as db:
                return [self._memory(row) for row in db.execute(
                    "SELECT * FROM recipient_memories WHERE buyer_id=? AND recipient_label=? "
                    "ORDER BY created_at, memory_id", (buyer_id, label),
                )]

        return await asyncio.to_thread(read)

    async def append(self, memory: RecipientMemory) -> RecipientMemory:
        def write():
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT * FROM recipient_memories WHERE buyer_id=? AND recipient_label=? "
                    "AND channel=? AND statement=?",
                    (memory.buyer_id, memory.recipient_label, memory.channel, memory.statement),
                ).fetchone()
                if existing is not None:
                    return self._memory(existing)
                identifier = memory.memory_id or uuid.uuid4().hex
                try:
                    db.execute("INSERT INTO recipient_memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                        identifier, memory.buyer_id, memory.recipient_label, memory.channel,
                        memory.statement, memory.version, memory.created_at,
                        memory.source_kind, memory.source_ref,
                    ))
                except sqlite3.IntegrityError as error:
                    raise MemoryConflict("记忆 ID 已存在，请重新保存") from error
                return self._memory(db.execute(
                    "SELECT * FROM recipient_memories WHERE memory_id=?", (identifier,),
                ).fetchone())

        return await asyncio.to_thread(write)

    async def replace_by_id(self, buyer_id: str, recipient_label: str, memory_id: str,
                            expected_version: int, memory: RecipientMemory) -> bool:
        label = normalize_recipient_label(recipient_label)
        if memory.buyer_id != buyer_id or memory.recipient_label != label:
            raise ValueError("买家身份或收礼人标签不一致")

        def update():
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    changed = db.execute(
                        "UPDATE recipient_memories SET channel=?, statement=?, version=version+1, "
                        "source_kind=?, source_ref=? WHERE buyer_id=? AND recipient_label=? "
                        "AND memory_id=? AND version=?",
                        (memory.channel, memory.statement, memory.source_kind, memory.source_ref,
                         buyer_id, label, memory_id, expected_version),
                    ).rowcount
                except sqlite3.IntegrityError as error:
                    raise MemoryConflict("已有相同收礼人记忆，请明确删除重复项") from error
                if not changed:
                    raise MemoryConflict("记忆已变化或删除，请刷新后重试")
                return True

        return await asyncio.to_thread(update)

    async def delete_by_id(self, buyer_id: str, recipient_label: str, memory_id: str,
                           expected_version: int) -> bool:
        label = normalize_recipient_label(recipient_label)

        def remove():
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "DELETE FROM recipient_memories WHERE buyer_id=? AND recipient_label=? "
                    "AND memory_id=? AND version=?", (buyer_id, label, memory_id, expected_version),
                ).rowcount
                if not changed:
                    raise MemoryConflict("记忆已变化或删除，请刷新后重试")
                return True

        return await asyncio.to_thread(remove)
