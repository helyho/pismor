"""mem_fts（BM25 词法影子索引）测试：触发器同步 / BM25 排序 / 分词器同源与切换重建。"""
import sqlite3

import pytest

from pismor.core import fts, tokenizer
from pismor.core.db import get_conn, init_schema
from pismor.serving import assembler


def _entry(conn, topic, summary, *, dim="preference", uid="u1"):
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements)"
        " VALUES ('w',?,?,?,?,0.9,'active',?,?,?,?,?,0)",
        (uid, dim, topic, f"{dim}_{topic}", summary, summary,
         "2026-01-01", "2026-01-01", "2026-01-01"))


def test_trigger_insert_updates_delete(conn):
    _entry(conn, "coffee", "每天只喝浅烘咖啡豆")
    rowid = conn.execute("SELECT rowid FROM entries").fetchone()[0]
    assert conn.execute("SELECT terms FROM mem_fts WHERE rowid=?", (rowid,)).fetchone()[0] \
        .split()  # 非空：INSERT 触发器已写入预分词串
    # UPDATE OF summary → 索引跟随（jieba 将"深烘咖啡豆"切为 深/烘/咖啡/咖啡豆）
    conn.execute("UPDATE entries SET summary=? WHERE rowid=?", ("改成只喝深烘咖啡豆", rowid))
    assert "深" in conn.execute("SELECT terms FROM mem_fts WHERE rowid=?", (rowid,)).fetchone()[0].split()
    # DELETE → 索引行消失
    conn.execute("DELETE FROM entries WHERE rowid=?", (rowid,))
    assert conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0] == 0
    conn.commit()


def test_bm25_orders_by_relevance(conn):
    # 咖啡豆高频条目应排前（TF）；无关条目不召回
    _entry(conn, "a", "咖啡豆 咖啡豆 咖啡豆 手冲")  # 高频
    _entry(conn, "b", "咖啡豆 拿铁")                # 低频
    _entry(conn, "c", "完全是别的话题")
    conn.commit()
    pack = assembler.assemble(conn, None, "w", "u1", budget=2000, query="咖啡豆")
    ids = [e["id"] for e in pack["entries"]]
    assert ids == ["preference_a", "preference_b"]  # c 不召回；a 的 TF 高排前


def test_lexical_or_semantics(conn):
    _entry(conn, "a", "浅烘偏好")
    _entry(conn, "b", "深烘偏好")
    conn.commit()
    # 查询侧必须经 assembler.tokens 同源切词（jieba 下"浅烘"非整词，直接拼 MATCH 不命中）
    rows = assembler._lexical(conn, "w", "u1", assembler.tokens("浅烘或深烘"), None, top_k=20)
    assert {r["id"] for r in rows} == {"preference_a", "preference_b"}  # OR 语义：任一命中


def test_match_expr_escapes_quotes():
    assert fts.match_expr(['a"b', "c"]) == '"a""b"* OR "c"*'


def test_tokenizer_bigram_and_jieba():
    tokenizer.set_tokenizer("bigram")
    assert tokenizer.tokenize("浅烘咖啡豆 abc1") == ["浅烘", "烘咖", "咖啡", "啡豆", "abc1"]
    tokenizer.set_tokenizer("jieba")
    toks = tokenizer.tokenize("浅烘咖啡豆 abc1")
    assert "咖啡豆" in toks and "abc1" in toks  # jieba 语义词；与 bigram 切分不同
    with pytest.raises(ValueError):
        tokenizer.set_tokenizer("nope")
    tokenizer.set_tokenizer("jieba")  # 还原默认


def test_index_side_keeps_duplicates_for_tf(conn):
    tokenizer.set_tokenizer("jieba")
    _entry(conn, "a", "咖啡豆 咖啡豆 手冲")
    conn.commit()
    terms = conn.execute("SELECT terms FROM mem_fts").fetchone()[0]
    assert terms.split().count("咖啡豆") == 2  # 索引不去重（BM25 TF 依赖词频）


def test_init_schema_rebuilds_on_tokenizer_switch(tmp_path):
    # 首次 init（bigram）→ 索引为 bigram 产物；切 jieba 再 init → 自动全量重建
    path = tmp_path / "t.db"
    c1 = get_conn(path)
    tokenizer.set_tokenizer("bigram")
    init_schema(c1, None)
    c1.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements)"
        " VALUES ('w','u1','preference','t','preference_t',0.9,'active',"
        "'浅烘咖啡豆','浅烘咖啡豆','2026-01-01','2026-01-01','2026-01-01',0)")
    c1.commit()
    c1.close()
    tokenizer.set_tokenizer("jieba")
    c2 = get_conn(path)
    init_schema(c2, None)  # meta 不符 → rebuild
    terms = c2.execute("SELECT terms FROM mem_fts").fetchone()[0]
    assert "咖啡豆" in terms.split() and "浅烘" not in terms.split()  # jieba 语义词替换 bigram 产物
    assert c2.execute("SELECT value FROM fts_meta WHERE key='tokenizer'").fetchone()[0] == "jieba"
    c2.close()
    tokenizer.set_tokenizer("jieba")  # 还原默认


def test_init_schema_backfill_missing_rows(conn):
    # 行数漂移（索引缺行）→ init_schema 兜底重建
    _entry(conn, "a", "咖啡豆")
    conn.commit()
    conn.execute("DELETE FROM mem_fts")
    conn.commit()
    init_schema(conn, None)
    assert conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0] == 1
