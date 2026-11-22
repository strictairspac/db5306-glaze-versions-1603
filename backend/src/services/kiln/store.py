"""窑务台账的 SQLite 存储层。

只使用标准库。每张表的写入都在短事务内完成；发布一笔封单时，
版次写入、撤版标记（发布时不存在）与水位推进在同一个事务里提交，
保证“整笔一次性切换”，中断后靠重放事务天然续跑。
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS manifests (
    seq              INTEGER PRIMARY KEY,
    prev_seq         INTEGER NOT NULL,
    expected_groups  TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id               INTEGER PRIMARY KEY,
    seq              INTEGER NOT NULL,
    group_id         TEXT NOT NULL,
    content          TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    event_id         TEXT,
    expected         INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(seq, group_id, content_hash),
    UNIQUE(event_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_seq ON deliveries(seq);

CREATE TABLE IF NOT EXISTS delivery_conflicts (
    id               INTEGER PRIMARY KEY,
    seq              INTEGER NOT NULL,
    group_id         TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    content          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(seq, group_id, content_hash)
);

CREATE TABLE IF NOT EXISTS published (
    seq              INTEGER PRIMARY KEY,
    published_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS editions (
    id               INTEGER PRIMARY KEY,
    seq              INTEGER NOT NULL,
    group_id         TEXT NOT NULL,
    content          TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    UNIQUE(seq, group_id)
);

CREATE INDEX IF NOT EXISTS idx_editions_group ON editions(group_id, seq);

CREATE TABLE IF NOT EXISTS withdrawals (
    id               INTEGER PRIMARY KEY,
    group_id         TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    reason           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(group_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_withdrawals_group ON withdrawals(group_id, seq);

CREATE TABLE IF NOT EXISTS inbound_log (
    event_id         TEXT PRIMARY KEY,
    seq              INTEGER NOT NULL,
    group_id         TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
"""


class Store:
    """封装所有 SQL，业务规则放在 ``kiln.py``。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- 封单 -------------------------------------------------------------

    def get_manifest(self, conn: sqlite3.Connection, seq: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM manifests WHERE seq=?", (seq,)).fetchone()

    def insert_manifest(
        self, conn: sqlite3.Connection, seq: int, prev_seq: int,
        expected_groups: str, created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO manifests(seq, prev_seq, expected_groups, created_at) "
            "VALUES (?, ?, ?, ?)",
            (seq, prev_seq, expected_groups, created_at),
        )

    def list_manifests(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM manifests ORDER BY seq"))

    # -- 来件 -------------------------------------------------------------

    def get_delivery_hashes(self, conn: sqlite3.Connection, seq: int, group_id: str) -> set[str]:
        rows = conn.execute(
            "SELECT content_hash FROM deliveries WHERE seq=? AND group_id=?",
            (seq, group_id),
        ).fetchall()
        return {row["content_hash"] for row in rows}

    def insert_delivery(
        self, conn: sqlite3.Connection, seq: int, group_id: str, content: str,
        content_hash: str, event_id: str | None, expected: bool, created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO deliveries(seq, group_id, content, content_hash, event_id, "
            "expected, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq, group_id, content, content_hash, event_id, int(expected), created_at),
        )

    def list_deliveries(self, conn: sqlite3.Connection, seq: int) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM deliveries WHERE seq=? ORDER BY id", (seq,)))

    def record_conflict(
        self, conn: sqlite3.Connection, seq: int, group_id: str,
        content_hash: str, content: str, created_at: str,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO delivery_conflicts(seq, group_id, content_hash, "
            "content, created_at) VALUES (?, ?, ?, ?, ?)",
            (seq, group_id, content_hash, content, created_at),
        )

    def list_conflicts(self, conn: sqlite3.Connection, seq: int) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM delivery_conflicts WHERE seq=? ORDER BY id", (seq,)))

    # -- 去重日志 ----------------------------------------------------------

    def logged_event(self, conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM inbound_log WHERE event_id=?", (event_id,)).fetchone()

    def log_event(
        self, conn: sqlite3.Connection, event_id: str, seq: int,
        group_id: str, content_hash: str, created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO inbound_log(event_id, seq, group_id, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, seq, group_id, content_hash, created_at),
        )

    # -- 发布 -------------------------------------------------------------

    def is_published(self, conn: sqlite3.Connection, seq: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM published WHERE seq=?", (seq,)).fetchone() is not None

    def published_sequences(self, conn: sqlite3.Connection) -> list[int]:
        return [row["seq"] for row in conn.execute("SELECT seq FROM published ORDER BY seq")]

    def insert_edition(
        self, conn: sqlite3.Connection, seq: int, group_id: str,
        content: str, content_hash: str, published_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO editions(seq, group_id, content, content_hash, published_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (seq, group_id, content, content_hash, published_at),
        )

    def mark_published(self, conn: sqlite3.Connection, seq: int, published_at: str) -> None:
        conn.execute(
            "INSERT INTO published(seq, published_at) VALUES (?, ?)",
            (seq, published_at),
        )

    # -- 副本读取与撤版 -----------------------------------------------------

    def watermark(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT MAX(seq) AS wm FROM published").fetchone()
        return row["wm"] or 0

    def edition_at(
        self, conn: sqlite3.Connection, group_id: str, at_seq: int,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM editions WHERE group_id=? AND seq<=? "
            "ORDER BY seq DESC LIMIT 1",
            (group_id, at_seq),
        ).fetchone()

    def latest_edition(self, conn: sqlite3.Connection, group_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM editions WHERE group_id=? ORDER BY seq DESC LIMIT 1",
            (group_id,),
        ).fetchone()

    def list_groups(self, conn: sqlite3.Connection) -> list[str]:
        return [row["group_id"] for row in conn.execute(
            "SELECT DISTINCT group_id FROM editions ORDER BY group_id")]

    def withdrawal_at(
        self, conn: sqlite3.Connection, group_id: str, at_seq: int,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM withdrawals WHERE group_id=? AND seq<=? "
            "ORDER BY seq DESC LIMIT 1",
            (group_id, at_seq),
        ).fetchone()

    def insert_withdrawal(
        self, conn: sqlite3.Connection, group_id: str, seq: int,
        reason: str, created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO withdrawals(group_id, seq, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (group_id, seq, reason, created_at),
        )

    def list_withdrawals(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM withdrawals ORDER BY id DESC"))
