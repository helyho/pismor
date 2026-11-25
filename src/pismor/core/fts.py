"""mem_fts（FTS5 影子索引）辅助：MATCH 表达式构造与全量重建。

同步机制：mem_fts 由 db.SCHEMA 的触发器（AFTER INSERT/UPDATE OF summary/DELETE ON
entries）+ UDF fts_terms() 自动同步——任何写路径（管线/API/测试直插）均一致，
写入侧零改动。本模块只提供查询侧表达式与重建工具。

BM25 语义：sqlite bm25() 返回负值，越小越相关 → ORDER BY bm25(mem_fts) 升序。
"""
import sqlite3

from pismor.core import tokenizer


def match_expr(tokens: list[str]) -> str:
    """token 列表 → FTS5 OR 前缀查询串（任一命中即候选，对齐旧 LIKE OR 语义）。

    FTS5 空格分隔默认是隐式 AND，"词面任一命中"必须显式 OR；每 token 追加
    前缀通配 *（"tok"*）：jieba 切分随上下文漂移（"深烘偏好"→整词"深烘"、
    "浅烘偏好"→"浅"/"烘"），同源切词也无法保证查询 token 与索引 token 相等，
    前缀匹配补齐召回（"深"* 命中索引 token "深烘"，"咖啡"* 命中 "咖啡豆"），
    精度由 BM25 排序 + top_k 截断兜底。token 双引号内 " 以 "" 转义。
    """
    return " OR ".join('"%s"*' % t.replace('"', '""') for t in tokens if t)


def rebuild(conn: sqlite3.Connection) -> None:
    """全量重建 mem_fts（分词器切换/行数漂移/存量数据回填）。同事务原子。"""
    with conn:
        conn.execute("DELETE FROM mem_fts")
        conn.execute(
            "INSERT INTO mem_fts(rowid, terms) SELECT rowid, fts_terms(summary) FROM entries")
