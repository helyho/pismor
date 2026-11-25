"""Task 12 md 导出器测试：文件集、spec §9 小节格式、确定性（逐字节）、
pending/archive 归宿、轨迹行证据键（裸 id 例外）、只读。"""
from pathlib import Path

import pytest

from pismor.serving.exporter import export

NOW = "2026-11-24T00:00:00+08:00"
REPO = Path(__file__).resolve().parent.parent

_ENTRY_COLS = ("user_source,user_id,dimension,topic,scope,id,confidence,status,body,summary,"
               "created_at,updated_at,last_reinforced_at,degraded_reason,topic_new,"
               "conflict_with,reinforcements,archived_at")


def seed(conn, dimension, topic, scope="global", status="active", confidence=0.9,
         body="一句话画像", summary=None, degraded_reason=None, sources=True):
    """直接造行（同 test_audit 模式）：证据键统一为行键 "{id}@{scope}"。"""
    conn.execute(
        f"INSERT INTO entries ({_ENTRY_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("w", "u1", dimension, topic, scope, f"{dimension}_{topic}@{scope}", confidence, status,
         body, summary if summary is not None else body[:10], NOW, NOW, NOW,
         degraded_reason, None, None, 0, NOW if status == "archived" else None))
    if not sources:
        return
    conn.execute(
        "INSERT INTO sources (entry_id,user_source,user_id,origin_type,origin_ref,quote,kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"{dimension}_{topic}@{scope}",
         "w", "u1", "conversation", "s1:1", "用户原话", "explicit"))


@pytest.fixture
def seeded(conn):
    seed(conn, "taboo", "tool", body="表格类结果不用 Excel 公式", summary="表格不用公式")
    seed(conn, "taboo", "tone", status="degraded", degraded_reason="refuted",
         body="回复中不使用 emoji", summary="不用 emoji")
    seed(conn, "preference", "lang", body="回答用中文", summary="中文回答")
    seed(conn, "preference", "format", scope="project:alpha", body="alpha 用表格交付",
         summary="alpha 表格")  # 确定性测试覆盖 projects/ 分支（review Minor-3）
    seed(conn, "preference", "~lang:20261124T000000+08:00:1", status="archived",
         degraded_reason="superseded", body="旧值：回答用英文", summary="旧值")
    return conn


@pytest.fixture
def seeded_orphan(conn):
    seed(conn, "orphan", "xx", status="orphan_topic", body="观察区候选")
    return conn


def test_export_files(tmp_path, conn, seeded, taxonomy):
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    assert (root / "identity.md").exists() and (root / "taboo.md").exists()
    assert (root / "shell.md").exists() and (root / "_index.md").exists()
    text = (root / "taboo.md").read_text(encoding="utf-8")
    assert "## [taboo_" in text and "---" in text  # spec §9 格式


def test_export_deterministic(tmp_path, conn, seeded, taxonomy):
    r1 = export(conn, "w", "u1", tmp_path / "a", taxonomy)
    r2 = export(conn, "w", "u1", tmp_path / "b", taxonomy)
    for f in sorted(r1.rglob("*.md")):
        assert f.read_bytes() == (r2 / f.relative_to(r1)).read_bytes()


def test_pending_in_export(tmp_path, conn, seeded_orphan, taxonomy):
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    assert "orphan" in (root / "pending.md").read_text()


def test_section_layout(tmp_path, conn, seeded, taxonomy):
    """§9 小节：标题行 → --- 围栏 YAML（裁决键序）→ 一句正文。"""
    text = (export(conn, "w", "u1", tmp_path, taxonomy) / "taboo.md").read_text(encoding="utf-8")
    block = text.split("## [taboo_tool@global]", 1)[1]
    assert block.startswith(" 表格不用公式\n\n---\nid: taboo_tool@global\n")
    yaml_part = block.split("---\n", 2)[1]
    keys = [ln.split(":")[0] for ln in yaml_part.splitlines() if ln and not ln.startswith((" ", "-"))]
    assert keys == ["id", "dimension", "scope", "confidence", "status", "created_at",
                    "updated_at", "sources", "reinforcements", "degraded_reason",
                    "superseded_by"]
    assert "quote: 用户原话" in yaml_part and "kind: explicit" in yaml_part
    assert yaml_part.rstrip().endswith("superseded_by: null")
    assert text.rstrip().endswith("表格类结果不用 Excel 公式")


def test_archive_and_trace_sources(tmp_path, conn, seeded, taxonomy):
    """archived（含 UPDATE 轨迹行）只进 archive.md；轨迹行证据按裸 id 键取出。"""
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    arch = (root / "archive.md").read_text(encoding="utf-8")
    assert "## [preference_~lang:" in arch and "旧值：回答用英文" in arch
    assert "quote: 用户原话" in arch  # 裸 id JOIN 分支命中，证据未丢
    for fname in ("taboo.md", "shell.md", "pending.md"):
        assert "archived" not in (root / fname).read_text(encoding="utf-8")


def test_degraded_stays_in_dimension_file(tmp_path, conn, seeded, taxonomy):
    """degraded 是状态不是位置：留在原维度文件（spec §9 约定）。"""
    assert "## [taboo_tone@global]" in (export(conn, "w", "u1", tmp_path, taxonomy) / "taboo.md").read_text(encoding="utf-8")


def test_index_rows(tmp_path, conn, seeded, seeded_orphan, taxonomy):
    """_index.md 一行一条目（含 archive/pending 归宿列）。"""
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    lines = (root / "_index.md").read_text(encoding="utf-8").splitlines()
    rows = [ln for ln in lines if ln.startswith("| ") and "---" not in ln]
    assert len(rows) == 7  # 表头 + 5 条 seeded + 1 条 orphan
    # review Minor-4：整行列值校验（id|dimension|topic|scope|status|conf|file|summary）
    assert ("| preference_format@project:alpha | preference | format | project:alpha | active | 0.9"
            " | projects/alpha.md | alpha 表格 |") in rows
    joined = "\n".join(rows)
    assert "preference_~lang:" in joined and "archive.md" in joined


def test_project_scope_split(tmp_path, conn, seeded, taxonomy):
    """修复轮①：scope=project:{name} 分流 projects/{name}.md，不混入 shell.md。"""
    seed(conn, "preference", "tool", scope="project:alpha", body="alpha 项目用 pnpm",
         summary="alpha 用 pnpm")
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    proj = (root / "projects" / "alpha.md").read_text(encoding="utf-8")
    assert "## [preference_tool@project:alpha]" in proj and "alpha 项目用 pnpm" in proj
    assert "preference_tool@" not in (root / "shell.md").read_text(encoding="utf-8")
    assert "| preference_tool@project:alpha |" in (root / "_index.md").read_text(encoding="utf-8")


def test_taboo_project_scope_stays_in_taboo_md(tmp_path, conn, taxonomy):
    """修复轮②裁决：taboo/identity 恒入必带集文件，project scope 只留 YAML。"""
    seed(conn, "taboo", "tool", scope="project:alpha", body="alpha 项目禁用公式",
         summary="禁公式")
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    assert "## [taboo_tool@project:alpha]" in (root / "taboo.md").read_text(encoding="utf-8")
    assert not (root / "projects" / "alpha.md").exists()


def test_project_name_traversal_rejected(tmp_path, conn, taxonomy):
    """修复轮② Critical：白名单外 project name 拒绝写文件，记 export-warnings.txt。"""
    before = set(p.name for p in tmp_path.iterdir())
    seed(conn, "preference", "tool", scope="project:../../x", body="穿越尝试", summary="穿越")
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    assert set(p.name for p in tmp_path.iterdir()) - before == {"w"}  # 树外零写入
    assert not list(root.rglob("projects/*"))
    for f in root.rglob("*.md"):
        assert "preference_tool@" not in f.read_text(encoding="utf-8")
    assert "preference_tool@project:../../x\tinvalid project scope: project:../../x" in \
        (root / "export-warnings.txt").read_text(encoding="utf-8")


def test_export_readonly(tmp_path, conn, seeded, taxonomy):
    before = conn.total_changes
    export(conn, "w", "u1", tmp_path, taxonomy)
    assert conn.total_changes == before


def test_empty_user_still_writes_fileset(tmp_path, conn, taxonomy):
    """无条目用户也产出全文件集（taxonomy 维度文件空也要建，spec §8.2）。"""
    root = export(conn, "w", "nobody", tmp_path, taxonomy)
    for fname in ("identity.md", "taboo.md", "shell.md", "archive.md", "pending.md", "_index.md"):
        assert (root / fname).exists()


def test_export_clears_stale_files(tmp_path, conn, taxonomy):
    # 导出前清理本用户旧文件：条目消失后旧 md 不得残留（I-11）
    seed(conn, "preference", "tool", scope="project:alpha", body="alpha 用 pnpm", summary="pnpm")
    root = export(conn, "w", "u1", tmp_path, taxonomy)
    assert (root / "projects" / "alpha.md").exists()
    conn.execute("DELETE FROM entries WHERE topic='tool'")
    conn.execute("DELETE FROM sources WHERE entry_id='preference_tool@project:alpha'")
    conn.commit()
    root2 = export(conn, "w", "u1", tmp_path, taxonomy)
    assert not (root2 / "projects" / "alpha.md").exists()
    assert "preference_tool@" not in (root2 / "shell.md").read_text(encoding="utf-8")


def test_export_uses_given_taxonomy(tmp_path, conn, seeded):
    # 导出必须用调用方给的词表（settings.taxonomy_path），不得回落到仓库默认文件
    import yaml
    from pismor.core.taxonomy import load_taxonomy

    raw = yaml.safe_load((REPO / "config" / "taxonomy.yaml").read_text(encoding="utf-8"))
    for d in raw["dimensions"]:
        if d["key"] == "preference":
            d["file"] = "pref.md"
    custom = tmp_path / "custom_taxonomy.yaml"
    custom.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    root = export(conn, "w", "u1", tmp_path / "out", load_taxonomy(custom))
    assert (root / "pref.md").exists()
    assert "回答用中文" in (root / "pref.md").read_text(encoding="utf-8")
    assert "回答用中文" not in (root / "shell.md").read_text(encoding="utf-8")
