"""架构护栏：条目生命周期规则只在 core.lifecycle 声明。

这些测试扫描源码，防止同一业务不变量重新散落成多份 SQL 片段。
新增例外时请先问：它是否真的拥有这条规则，还是应该调用 core.lifecycle。
"""
from pathlib import Path
import re

SRC = Path(__file__).resolve().parents[2] / "src" / "pismor"

# 硬编码的墓碑谓词副本：只允许 core/lifecycle.py 用 {alias} 生成。
ALIVE_LITERAL = re.compile(r"t\.user_source=entries\.user_source")

# 直接读 tombstones 的文件白名单：规则声明 + 墓碑写/报表所有权。
TOMBSTONE_READERS = {
    "core/lifecycle.py",       # 规则本身
    "serving/memory.py",       # soft delete / restore / purge 的写路径
    "serving/audit.py",        # 一次取全量墓碑集合
    "admin/queries/users.py",  # 墓碑列表与详情
    "admin/queries/system.py", # tombstones 计数
    "admin/queries/overview.py",  # tombstones 计数
}

# 直接改 entries.status 的文件白名单：每处都必须接 core.lifecycle 的校验。
STATUS_WRITERS = {
    "pipeline/writer.py",
    "pipeline/aggregator.py",
    "pipeline/runner.py",
    "serving/review.py",
    "serving/audit.py",
    "admin/entry_service.py",
}

# 同一个 UPDATE 语句内出现 status=；窗口只够覆盖跨行拼接的列清单。
_STATUS_UPDATE = re.compile(r"UPDATE\s+entries\s+SET[\s\S]{0,300}?\bstatus\s*=", re.I)


def _sources():
    for path in SRC.rglob("*.py"):
        yield path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8")


def _hits(pattern: re.Pattern) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for rel, text in _sources():
        lines = [n for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
        if lines:
            found[rel] = lines
    return found


def test_tombstone_predicate_is_not_copied():
    hits = _hits(ALIVE_LITERAL)
    assert hits == {}, f"用 core.lifecycle.alive_sql(alias) 代替手写谓词: {hits}"


def test_tombstone_reads_are_confined():
    offenders = sorted(set(_hits(re.compile(r"FROM\s+tombstones", re.I))) - TOMBSTONE_READERS)
    assert offenders == [], f"tombstones 读取越界，改走 core.lifecycle: {offenders}"


def test_status_updates_are_confined():
    offenders = sorted(set(_hits(_STATUS_UPDATE)) - STATUS_WRITERS)
    assert offenders == [], f"entries.status 写入越界，接 core.lifecycle: {offenders}"


def test_status_writers_import_the_rule():
    missing = []
    for rel, text in _sources():
        if rel not in STATUS_WRITERS:
            continue
        if "check_transition" not in text:
            missing.append(rel)
    assert missing == [], f"白名单文件没有接状态机校验: {missing}"


# 直接对 entries.conflict_with 做 JSON 解析/序列化的文件白名单：只有值类型自己。
CONFLICT_MARK_OWNER = "core/conflict_marks.py"

_JSON_ON_CONFLICT = re.compile(r"json\.(?:loads|dumps)\s*\([^\n]*conflict_with", re.I)

# 各模块曾各自复制的标记辅助函数名；回潮即失败。
_OLD_MARK_HELPERS = re.compile(r"def\s+_(?:load|parse|replace)_marks\s*\(")


def test_conflict_marks_json_is_confined():
    offenders = sorted(set(_hits(_JSON_ON_CONFLICT)) - {CONFLICT_MARK_OWNER})
    assert offenders == [], f"conflict_with 的 JSON 读写越界，改走 core.conflict_marks: {offenders}"


def test_no_conflict_mark_helper_copies():
    hits = _hits(_OLD_MARK_HELPERS)
    assert hits == {}, f"标记集合的解析/改写只能有 core.conflict_marks 一份: {hits}"
