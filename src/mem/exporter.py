"""md 导出器：SQLite 真相源 → spec §8.2 文件集只读视图（小节格式 spec §9）。

- 归宿规则：archived → archive.md；pending_review/orphan_topic → pending.md；
  active/degraded 按 taxonomy.dimensions[].file 留在原维度文件（degraded 是状态不是位置）。
- 键约定（上游裁决）：sources.entry_id 是行唯一复合键 "{entries.id}@{scope}"；
  唯一例外是 UPDATE 轨迹行（topic 以 ~ 前缀）——写入器转移证据时存裸 id
  （writer._update），JOIN 键按行形态二分。
- 确定性输出：查询固定排序 + yaml.safe_dump(sort_keys=False)，同库同输出；
  导出器只读库（仅 SELECT），手工编辑导出文件会在下次导出被覆盖。
"""
from pathlib import Path
import yaml

from mem.taxonomy import load_taxonomy

_REPO = Path(__file__).resolve().parents[2]  # editable/repo 布局下 config/ 在包仓根
_SECTION_KEYS = ("id", "dimension", "scope", "confidence", "status",
                 "created_at", "updated_at", "sources", "degraded_reason", "superseded_by")


def export(conn, user_source, user_id, out_root: Path) -> Path:
    taxonomy = load_taxonomy(_REPO / "config" / "taxonomy.yaml")
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_source=? AND user_id=?"
        " ORDER BY dimension, scope, id",
        (user_source, user_id)).fetchall()
    # 证据行随条目一次取出；轨迹行走裸 id 键，其余走 "{id}@{scope}"（见模块 docstring）
    src_by_entry: dict[tuple, list] = {}
    for s in conn.execute(
        "SELECT e.dimension AS e_dim, e.topic AS e_topic, e.scope AS e_scope,"
        "       s.seq, s.origin_type, s.origin_ref, s.quote, s.kind"
        " FROM entries e JOIN sources s"
        "  ON s.user_source=e.user_source AND s.user_id=e.user_id"
        " AND s.entry_id = CASE WHEN e.topic LIKE '~%' THEN e.id"
        "                      ELSE e.id || '@' || e.scope END"
        " WHERE e.user_source=? AND e.user_id=? ORDER BY s.seq",
        (user_source, user_id)):
        src_by_entry.setdefault((s["e_dim"], s["e_topic"], s["e_scope"]), []).append(s)

    # 文件集合：taxonomy 维度文件（去重保序，空也产出）+ archive/pending/_index
    files: dict[str, list[str]] = {"archive.md": [], "pending.md": [], "_index.md": []}
    file_titles: dict[str, list[str]] = {}
    for d in taxonomy.dimensions.values():
        files.setdefault(d.file, [])
        file_titles.setdefault(d.file, []).append(d.name)

    index_rows: list[str] = []
    for r in rows:
        sources = src_by_entry.get((r["dimension"], r["topic"], r["scope"]), [])
        if r["status"] == "archived":
            fname = "archive.md"
        elif r["status"] in ("pending_review", "orphan_topic"):
            fname = "pending.md"
        else:  # 未知维度兜底 shell.md，保证导出视图不丢行（库是真相源）
            fname = (taxonomy.dimensions[r["dimension"]].file
                     if r["dimension"] in taxonomy.dimensions else "shell.md")
        files.setdefault(fname, [])
        files[fname].append(_section(r, sources))
        files.setdefault("_index.md", [])
        index_rows.append(_index_row(r, fname))

    root = out_root / user_source / user_id
    root.mkdir(parents=True, exist_ok=True)
    for fname, sections in files.items():
        if fname == "_index.md":
            content = _index(user_source, user_id, index_rows)
        else:
            fallback = {"archive.md": "归档", "pending.md": "观察区与复核队列"}.get(
                fname, fname.removesuffix(".md"))
            title = " / ".join(file_titles.get(fname, [fallback]))
            content = (_header(fname, title, user_source, user_id)
                       + "\n".join(sections) + ("\n" if sections else ""))
        (root / fname).write_text(content, encoding="utf-8")
    return root


def _header(fname, title, us, uid) -> str:
    return (f"# {fname} — {title}（{us}/{uid}）\n"
            "<!-- 由画像管线自动维护，手工编辑会在下次导出被覆盖；纠错走复核队列 -->\n\n")


def _section(row, sources) -> str:
    data = {}
    for k in _SECTION_KEYS:
        if k == "sources":
            data[k] = [{"origin_type": s["origin_type"], "origin_ref": s["origin_ref"],
                        "quote": s["quote"], "kind": s["kind"]} for s in sources]
        else:
            data[k] = row[k]
    y = yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                       default_flow_style=False).rstrip("\n")
    title = row["summary"] or row["topic"]
    return f"## [{row['id']}] {title}\n\n---\n{y}\n---\n\n{row['body']}\n"


def _index_row(row, fname) -> str:
    cells = (row["id"], row["dimension"], row["topic"], row["scope"], row["status"],
             f"{row['confidence']:g}", fname, row["summary"])
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _index(us, uid, rows) -> str:
    head = _header("_index.md", "摘要", us, uid)
    table = ("| id | dimension | topic | scope | status | confidence | file | summary |\n"
             "|---|---|---|---|---|---|---|---|\n")
    return head + table + "\n".join(rows) + ("\n" if rows else "")
