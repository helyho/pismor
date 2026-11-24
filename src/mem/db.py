import sqlite3
from pathlib import Path
import sqlite_vec


def get_conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)  # Python 3.12+ 默认禁止扩展加载，需先授权
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("SELECT vec_version()")  # 加载 sqlite-vec 扩展
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  user_source TEXT NOT NULL, user_id TEXT NOT NULL,
  dimension TEXT NOT NULL, topic TEXT NOT NULL, scope TEXT NOT NULL,
  id TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending_review','orphan_topic','active','degraded','archived')),
  body TEXT NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  last_reinforced_at TEXT NOT NULL,
  degraded_reason TEXT, superseded_by TEXT, topic_new TEXT,
  conflict_with TEXT, reinforcements INTEGER NOT NULL DEFAULT 0, archived_at TEXT,
  PRIMARY KEY (user_source, user_id, dimension, topic, scope)
);
CREATE TABLE IF NOT EXISTS sources (
  seq INTEGER PRIMARY KEY,
  entry_id TEXT, user_source TEXT, user_id TEXT,
  origin_type TEXT NOT NULL, origin_ref TEXT NOT NULL,
  quote TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_entry ON sources(user_source, user_id, entry_id);
CREATE TABLE IF NOT EXISTS review_history (
  entry_id TEXT, user_source TEXT, user_id TEXT,
  verdict TEXT NOT NULL, actor TEXT NOT NULL, ts TEXT NOT NULL, note TEXT,
  entry_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events_seen (
  source TEXT, event_id TEXT, ts TEXT,
  PRIMARY KEY (source, event_id)
);
CREATE TABLE IF NOT EXISTS pending_clarifications (
  user_source TEXT, user_id TEXT,
  conflicting_ids TEXT NOT NULL, question TEXT NOT NULL,
  issued_at TEXT NOT NULL, ttl_days INTEGER NOT NULL DEFAULT 7,
  PRIMARY KEY (user_source, user_id, issued_at)
);
CREATE TABLE IF NOT EXISTS tombstones (
  user_source TEXT, user_id TEXT, deleted_at TEXT, requested_by TEXT,
  PRIMARY KEY (user_source, user_id)
);
"""


def init_schema(conn: sqlite3.Connection, vec_dimensions: int | None) -> None:
    conn.executescript(SCHEMA)
    if vec_dimensions:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0("
            f"entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[{vec_dimensions}])"
        )
    conn.commit()
