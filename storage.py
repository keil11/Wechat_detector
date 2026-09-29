"""Local state for selected groups, message cursors, and task suggestions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    last_seq INTEGER,
                    latest_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    group_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    local_id INTEGER NOT NULL,
                    sender TEXT,
                    sent_at INTEGER,
                    type TEXT,
                    content TEXT,
                    analyzed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, seq, local_id)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    due_at TEXT,
                    assignee TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT '待确认',
                    source_seq INTEGER NOT NULL,
                    source_local_id INTEGER NOT NULL,
                    evidence TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (group_id, source_seq, source_local_id, title)
                );
                CREATE INDEX IF NOT EXISTS ix_messages_pending
                    ON messages (group_id, analyzed, seq);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(groups)")}
            if "latest_at" not in columns:
                db.execute("ALTER TABLE groups ADD COLUMN latest_at INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def upsert_groups(self, groups):
        with self._connect() as db:
            db.executemany(
                "INSERT INTO groups(id,name,latest_at) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, latest_at=MAX(groups.latest_at, excluded.latest_at)",
                [(g["id"], g["name"], int(g.get("latest_at") or 0)) for g in groups],
            )

    def list_groups(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,name,enabled,last_seq,latest_at FROM groups "
                "ORDER BY latest_at DESC,name COLLATE NOCASE"
            )]

    def set_group_enabled(self, group_id, enabled):
        with self._connect() as db:
            db.execute("UPDATE groups SET enabled=? WHERE id=?", (int(enabled), group_id))

    def enabled_groups(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,name,last_seq,latest_at FROM groups WHERE enabled=1 ORDER BY latest_at DESC,name"
            )]

    def save_messages(self, group_id, messages):
        if not messages:
            return 0
        rows = [(
            group_id, int(m["sort_seq"]), int(m.get("local_id") or 0),
            str(m.get("sender_username") or ("我" if m.get("sender_id") == 2 else "")),
            int(m.get("create_time") or 0), str(m.get("type") or ""),
            str(m.get("content") or ""),
        ) for m in messages]
        with self._connect() as db:
            before = db.total_changes
            db.executemany(
                "INSERT OR IGNORE INTO messages "
                "(group_id,seq,local_id,sender,sent_at,type,content) "
                "VALUES (?,?,?,?,?,?,?)", rows,
            )
            inserted = db.total_changes - before
            db.execute(
                "UPDATE groups SET last_seq=MAX(COALESCE(last_seq,0),?), "
                "latest_at=MAX(COALESCE(latest_at,0),?) WHERE id=?",
                (max(r[1] for r in rows), max(r[4] for r in rows), group_id),
            )
            return inserted

    def get_setting(self, key, default=""):
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self._connect() as db:
            db.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    def pending_batch(self, group_id, limit=50):
        with self._connect() as db:
            pending = [dict(r) for r in db.execute(
                "SELECT * FROM messages WHERE group_id=? AND analyzed=0 "
                "ORDER BY seq,local_id LIMIT ?", (group_id, limit)
            )]
            if not pending:
                return [], []
            context = [dict(r) for r in db.execute(
                "SELECT * FROM messages WHERE group_id=? AND analyzed=1 AND seq<? "
                "ORDER BY seq DESC,local_id DESC LIMIT 5",
                (group_id, pending[0]["seq"]),
            )]
            return pending, list(reversed(context))

    def apply_analysis(self, group_id, messages, tasks):
        ids = {(str(i + 1)): m for i, m in enumerate(messages)}
        with self._connect() as db:
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                title = str(task.get("title") or "").strip()[:240]
                source_ids = task.get("source_ids") or []
                source = next((ids[str(i)] for i in source_ids if str(i) in ids), None)
                assignee = str(task.get("assignee") or "uncertain")
                if not title or source is None or assignee not in ("me", "uncertain"):
                    continue
                try:
                    confidence = max(0.0, min(1.0, float(task.get("confidence") or 0)))
                except (TypeError, ValueError):
                    confidence = 0.0
                due = str(task.get("due_at") or "").strip() or None
                evidence = str(task.get("evidence") or "").strip()[:500]
                db.execute(
                    "INSERT OR IGNORE INTO tasks "
                    "(group_id,title,due_at,assignee,confidence,source_seq,source_local_id,evidence) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (group_id, title, due, assignee, confidence,
                     source["seq"], source["local_id"], evidence),
                )
            db.executemany(
                "UPDATE messages SET analyzed=1 WHERE group_id=? AND seq=? AND local_id=?",
                [(group_id, m["seq"], m["local_id"]) for m in messages],
            )

    def list_tasks(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT t.*,g.name AS group_name FROM tasks t JOIN groups g ON g.id=t.group_id "
                "ORDER BY CASE t.status WHEN '待确认' THEN 0 WHEN '进行中' THEN 1 "
                "WHEN '已完成' THEN 2 ELSE 3 END, COALESCE(t.due_at,'9999'),t.id DESC"
            )]

    def update_task(self, task_id, *, title=None, due_at=None, status=None):
        allowed = {"待确认", "进行中", "已完成", "忽略"}
        if status is not None and status not in allowed:
            raise ValueError("无效状态")
        with self._connect() as db:
            if title is not None:
                db.execute("UPDATE tasks SET title=? WHERE id=?", (title.strip(), task_id))
            if due_at is not None:
                db.execute("UPDATE tasks SET due_at=? WHERE id=?", (due_at.strip() or None, task_id))
            if status is not None:
                db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def update_tasks_status(self, task_ids, status):
        allowed = {"待确认", "进行中", "已完成", "忽略"}
        if status not in allowed:
            raise ValueError("无效状态")
        if any(isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0
               for task_id in task_ids):
            raise ValueError("事项编号无效")
        ids = list(dict.fromkeys(task_ids))
        if not ids:
            raise ValueError("请先选择事项")
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            found = db.execute(
                f"SELECT COUNT(*) FROM tasks WHERE id IN ({placeholders})", ids
            ).fetchone()[0]
            if found != len(ids):
                raise ValueError("部分事项已不存在，请刷新后重试")
            db.execute(
                f"UPDATE tasks SET status=? WHERE id IN ({placeholders})",
                [status, *ids],
            )
            return len(ids)
