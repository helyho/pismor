import sqlite3
from pathlib import Path
import sqlite_vec


from pismor.core import fts as _fts
from pismor.core import tokenizer as _tokenizer


def _fts_terms(summary):
    """mem_fts 触发器分词 UDF：summary → 空格分隔 token 串（不去重不截断，保 TF）。"""
    return " ".join(_tokenizer.tokenize(summary or ""))


def get_conn(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 每请求/每后台任务各建独立短连接（api 层不传 check_same_thread），
    # 连接只在自己线程内使用，因此保持默认 True 即可。
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)  # Python 3.12+ 默认禁止扩展加载，需先授权
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("SELECT vec_version()")  # 加载 sqlite-vec 扩展
    # mem_fts 触发器分词 UDF（deterministic：同 summary 恒同输出，可进触发器）
    conn.create_function("fts_terms", 1, _fts_terms, deterministic=True)
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
CREATE INDEX IF NOT EXISTS idx_entries_status_updated ON entries(status, updated_at);
CREATE TABLE IF NOT EXISTS review_history (
  entry_id TEXT, user_source TEXT, user_id TEXT,
  decision TEXT NOT NULL, actor TEXT NOT NULL, ts TEXT NOT NULL, note TEXT,
  entry_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_history_entry
  ON review_history(entry_id, user_source, user_id, ts);
CREATE TABLE IF NOT EXISTS events_seen (
  source TEXT, event_id TEXT, ts TEXT,
  PRIMARY KEY (source, event_id)
);
CREATE TABLE IF NOT EXISTS pending_clarifications (
  user_source TEXT, user_id TEXT,
  conflicting_ids TEXT NOT NULL, question TEXT NOT NULL,
  issued_at TEXT NOT NULL, ttl_days INTEGER NOT NULL DEFAULT 7,
  -- 主键 = (用户, 矛盾对集合)：同一用户同秒可挂多对；同对重复 issue 由 UPSERT 刷新
  PRIMARY KEY (user_source, user_id, conflicting_ids)
);
CREATE TABLE IF NOT EXISTS tombstones (
  user_source TEXT, user_id TEXT, deleted_at TEXT, requested_by TEXT,
  purged_at TEXT,
  PRIMARY KEY (user_source, user_id)
);
CREATE TABLE IF NOT EXISTS merge_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_source TEXT NOT NULL, user_id TEXT NOT NULL,
  taboo_count INTEGER NOT NULL DEFAULT 0,
  scope_filter TEXT,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','done','dismissed')),
  proposal_json TEXT, note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- 同一用户同时只保留一条 open 任务：读路径重复装配不重复建单
CREATE UNIQUE INDEX IF NOT EXISTS uq_merge_tasks_open
  ON merge_tasks(user_source, user_id) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_merge_tasks_status ON merge_tasks(status, updated_at);
CREATE TABLE IF NOT EXISTS admin_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
  ts TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit(ts);
CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  ok INTEGER,
  stats_json TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at);
CREATE TABLE IF NOT EXISTS fts_meta (
  key TEXT PRIMARY KEY, value TEXT
);
-- BM25 词法影子索引：terms = fts_terms(summary) 预分词串，rowid = entries.rowid。
-- token 均为空格分隔词（分词在 UDF 完成），unicode61 恰好按空白切分。
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(terms, tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS trg_mem_fts_ai AFTER INSERT ON entries BEGIN
  INSERT INTO mem_fts(rowid, terms) VALUES (new.rowid, fts_terms(new.summary));
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_fts_au AFTER UPDATE OF summary ON entries BEGIN
  DELETE FROM mem_fts WHERE rowid=new.rowid;
  INSERT INTO mem_fts(rowid, terms) VALUES (new.rowid, fts_terms(new.summary));
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_fts_ad AFTER DELETE ON entries BEGIN
  DELETE FROM mem_fts WHERE rowid=old.rowid;
END;
"""


def init_schema(conn: sqlite3.Connection, vec_dimensions: int | None) -> None:
    conn.executescript(SCHEMA)
    # 旧库升级：tombstones 增加 purged_at（软删除/彻底删除双态）
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tombstones)")}
    if "purged_at" not in columns:
        conn.execute("ALTER TABLE tombstones ADD COLUMN purged_at TEXT")
        # 旧版本的删除是硬删除，存量 tombstone 的数据已不存在，统一标记为已彻底删除，
        # 避免升级后把它们显示成“可恢复”。
        conn.execute("UPDATE tombstones SET purged_at=deleted_at WHERE purged_at IS NULL")
    # 行键迁移（旧库升级）：id 曾为裸 {dimension}_{topic}，与 sources.entry_id 的
    # "{id}@{scope}" 两套格式并存；统一为行键后 entries.id 可直接与 sources JOIN。
    conn.execute(
        "UPDATE entries SET id = dimension || '_' || topic || '@' || scope"
        " WHERE id <> dimension || '_' || topic || '@' || scope")
    # mem_fts 分词器一致性：配置与建索引时不符 → 全量重建（切换 jieba/bigram 生效）
    name = _tokenizer.current()
    row = conn.execute("SELECT value FROM fts_meta WHERE key='tokenizer'").fetchone()
    if row is None:
        conn.execute("INSERT INTO fts_meta (key, value) VALUES ('tokenizer', ?)", (name,))
        _fts.rebuild(conn)  # 旧库升级：存量 entries 回填
    elif row["value"] != name:
        _fts.rebuild(conn)
        conn.execute("UPDATE fts_meta SET value=? WHERE key='tokenizer'", (name,))
    # 行数漂移兜底（手工删改/异常中断）：不符即重建，低频启动路径代价可接受
    ne, nf = conn.execute(
        "SELECT (SELECT count(*) FROM entries), (SELECT count(*) FROM mem_fts)").fetchone()
    if ne != nf:
        _fts.rebuild(conn)
    if vec_dimensions:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0("
            f"entry_id TEXT PRIMARY KEY, user_source TEXT, user_id TEXT, embedding float[{vec_dimensions}])"
        )
    conn.commit()
