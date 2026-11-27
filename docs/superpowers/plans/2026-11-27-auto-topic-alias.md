# 晋升时自动补 topic_aliases 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 日批晋升与人工复核归一时，把「裸自由主题 → canonical」自动写入对应维度的 `topic_aliases`，让后续抽取直接收敛。

**Architecture:** 新增 `core/taxonomy_edit.py` 集中守卫与 round-trip 写回；日批复用现有 `appends` + 提交后写文件机制；人工复核在返回值携带内部键 `_pending_alias`，由两个调用方在事务提交后写文件并热更新 `TaxonomyStore`。

**Tech Stack:** Python 3.14、SQLite、PyYAML round-trip、FastAPI、pytest。

**Spec:** `docs/superpowers/specs/2026-11-27-auto-topic-alias-design.md`

## Global Constraints

- 别名只增不改：`topic_aliases` 已有 key 一律跳过，不覆盖人工配置。
- 别名 key 不得与同维度 `topics` 同名（shadow），不得写入 disabled canonical。
- 文件写入必须走 `core/config_files` 的 round-trip（`read_yaml` → `dump_yaml` → `write_config(expected_version)`），保留注释与键顺序，版本冲突重试 2 轮。
- 不改 DB schema、不改 LLM prompt、不改管理端条目编辑。
- 所有新行为先写失败测试，再实现。

## Review Focus

1. **`topic_new` 前缀是未登记维度**（如 `unknown/沟通方式`）：`_bare_topic` 不应剥前缀；整串作为 alias key 时大概率永不命中，但不得因此报错或写入非法 mapping。
2. **裸主题本身含 `/`**：只有首段是已知维度 key 时才剥，不能无条件 `split("/")`。
3. **并发人工保存**：两个请求同时给同一 alias 写不同 canonical 时，文件层 `setdefault` 必须保留先到者，不覆盖。
4. **人工复核写文件失败**：条目转正已提交，别名写失败不得让接口报错或回滚条目。
5. **同一 alias 重复晋升**：守卫幂等，yaml 不出现重复 key、不重复追加备份。

## File Structure

| 文件 | 责任 |
|---|---|
| `src/pismor/core/taxonomy_edit.py`（新增） | `plan_topic_alias`、`flush_taxonomy_edits`、`revert_taxonomy_edits`、`write_pending_alias` |
| `src/pismor/pipeline/aggregator.py` | 日批：统一 append、`_bare_topic`、接入新模块、删除旧 `_flush_taxonomy`/`_revert_taxonomy` |
| `src/pismor/serving/review.py` | 人工复核：merge / rekey 返回 `_pending_alias` |
| `src/pismor/serving/api.py` | 事务外写别名 + `taxonomy_store.replace` |
| `src/pismor/admin/routers/review.py` | 同上，兼容无 store |
| `tests/test_topic_alias.py`（新增） | 守卫、round-trip、revert、review 返回值 |
| `tests/test_aggregator.py` | 追加日批别名用例 |
| `tests/test_api.py`、`tests/admin/test_review.py` | 追加两条调用方用例 |

---

### Task 1: `plan_topic_alias` 守卫

**Files:**
- Create: `src/pismor/core/taxonomy_edit.py`
- Test: `tests/test_topic_alias.py`

**Interfaces:**
- Consumes: `pismor.core.taxonomy.Taxonomy` / `Dimension`（`topics`、`disabled_topics`、`topic_aliases` 均可原地修改）。
- Produces: `plan_topic_alias(taxonomy: Taxonomy, dim_key: str, alias: str, canonical: str) -> dict | None`，成功返回 `{"dim": dim_key, "aliases": {alias: canonical}}` 并已改内存。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_topic_alias.py
import pytest
from pismor.core.taxonomy import load_taxonomy
from pismor.core.taxonomy_edit import plan_topic_alias

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]

@pytest.fixture
def tax():
    return load_taxonomy(REPO / "config" / "taxonomy.yaml")

def test_plan_alias_happy_path(tax):
    dim = tax.dimensions["preference"]
    canon = dim.topics[0]
    assert plan_topic_alias(tax, "preference", "沟通方式", canon) == {
        "dim": "preference", "aliases": {"沟通方式": canon}}
    assert dim.topic_aliases["沟通方式"] == canon

@pytest.mark.parametrize("alias,canon", [
    ("  ", "style"), ("别称", "  "), ("别称", "not_in_topics"),
])
def test_plan_alias_rejects_invalid(tax, alias, canon):
    assert plan_topic_alias(tax, "preference", alias, canon) is None

def test_plan_alias_rejects_same_and_shadow(tax):
    dim = tax.dimensions["preference"]
    assert plan_topic_alias(tax, "preference", dim.topics[0], dim.topics[0]) is None
    assert plan_topic_alias(tax, "preference", dim.topics[0], dim.topics[1]) is None

def test_plan_alias_rejects_disabled(tax):
    dim = tax.dimensions["preference"]
    dim.disabled_topics.append(dim.topics[0])
    assert plan_topic_alias(tax, "preference", "别称", dim.topics[0]) is None
    dim.disabled_topics.clear()
    dim.topics.append("停用别名")
    dim.disabled_topics.append("停用别名")
    assert plan_topic_alias(tax, "preference", "停用别名", dim.topics[1]) is None

def test_plan_alias_never_overwrites(tax):
    dim = tax.dimensions["preference"]
    dim.topic_aliases["别称"] = dim.topics[0]
    assert plan_topic_alias(tax, "preference", "别称", dim.topics[1]) is None

def test_plan_alias_unknown_dimension(tax):
    assert plan_topic_alias(tax, "nope", "别称", "style") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_topic_alias.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'pismor.core.taxonomy_edit'`

- [ ] **Step 3: 实现 `plan_topic_alias`**

按设计文档 §5.1 的 8 条守卫顺序实现；`alias`/`canonical` 先 `.strip()`。通过后 `d.topic_aliases[alias] = canonical` 并返回 append。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_topic_alias.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/pismor/core/taxonomy_edit.py tests/test_topic_alias.py
git commit -m "feat(taxonomy): add topic alias planning with guards"
```

---

### Task 2: `flush_taxonomy_edits` / `revert_taxonomy_edits`

**Files:**
- Modify: `src/pismor/core/taxonomy_edit.py`
- Test: `tests/test_topic_alias.py`

**Interfaces:**
- Consumes: `ConfigVersionConflict`、`read_yaml`、`dump_yaml`、`write_config` from `pismor.core.config_files`；Task 1 的 append 结构。
- Produces:
  - `flush_taxonomy_edits(taxonomy, appends, *, backup_root=None, keep=30, label="taxonomy-edit") -> Taxonomy | None`
  - `revert_taxonomy_edits(taxonomy, appends) -> None`
  - append 支持 `topics: list[str]`、`aliases: dict[str, str]`、`case: dict` 三类字段。

- [ ] **Step 1: 写失败测试**

```python
import shutil
from pismor.core.config_files import ConfigVersionConflict, write_config
from pismor.core.taxonomy_edit import (
    flush_taxonomy_edits, plan_topic_alias, revert_taxonomy_edits)

@pytest.fixture
def tax_writable(tmp_path):
    dst = tmp_path / "taxonomy.yaml"
    shutil.copy(REPO / "config" / "taxonomy.yaml", dst)
    return load_taxonomy(dst)

def test_flush_writes_alias_and_keeps_comments(tax_writable):
    dim = tax_writable.dimensions["preference"]
    append = plan_topic_alias(tax_writable, "preference", "沟通方式", dim.topics[0])
    before = tax_writable.path.read_text(encoding="utf-8")
    new = flush_taxonomy_edits(tax_writable, [append], label="taxonomy-alias")
    text = tax_writable.path.read_text(encoding="utf-8")
    assert "topic_aliases:" in text and "沟通方式" in text
    assert "停用主题" in text  # 注释仍在
    assert before != text
    assert new.dimensions["preference"].topic_aliases["沟通方式"] == dim.topics[0]

def test_flush_retries_version_conflict(tax_writable, monkeypatch):
    dim = tax_writable.dimensions["preference"]
    append = plan_topic_alias(tax_writable, "preference", "别称", dim.topics[0])
    real = write_config
    calls = {"n": 0}
    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConfigVersionConflict("conflict")
        return real(*args, **kwargs)
    monkeypatch.setattr("pismor.core.taxonomy_edit.write_config", flaky)
    assert flush_taxonomy_edits(tax_writable, [append]) is not None
    assert calls["n"] == 2

def test_flush_does_not_overwrite_existing_alias(tax_writable):
    dim = tax_writable.dimensions["preference"]
    canon = dim.topics[0]
    append = plan_topic_alias(tax_writable, "preference", "别称", canon)
    # 模拟另一进程抢先写入同一 alias 的不同 canonical
    data, _text, version = read_yaml(tax_writable.path)
    for d in data["dimensions"]:
        if d["key"] == "preference":
            d.setdefault("topic_aliases", {})["别称"] = dim.topics[1]
    write_config(tax_writable.path, dump_yaml(data), expected_version=version)
    new = flush_taxonomy_edits(tax_writable, [append])
    assert new.dimensions["preference"].topic_aliases["别称"] == dim.topics[1]

def test_revert_removes_only_new_alias(tax_writable):
    dim = tax_writable.dimensions["preference"]
    dim.topic_aliases["既有"] = dim.topics[0]
    append = plan_topic_alias(tax_writable, "preference", "新别名", dim.topics[0])
    revert_taxonomy_edits(tax_writable, [append])
    assert "新别名" not in dim.topic_aliases
    assert dim.topic_aliases["既有"] == dim.topics[0]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_topic_alias.py -v`
Expected: FAIL，`ImportError: cannot import name 'flush_taxonomy_edits'`

- [ ] **Step 3: 实现两个函数**

`flush_taxonomy_edits` 照搬 `aggregator._flush_taxonomy` 的两轮重试结构；修改 `d["topics"]`、`d.setdefault("topic_aliases", {}).setdefault(k, v)`、`data.setdefault("regression_cases", []).append(case)`。无 appends 返回 `None`。`revert_taxonomy_edits` 逆序删除本 append 的 `aliases` key、移除新增 `topics`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_topic_alias.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/pismor/core/taxonomy_edit.py tests/test_topic_alias.py
git commit -m "feat(taxonomy): flush and revert topic alias edits"
```

---

### Task 3: 日批接入

**Files:**
- Modify: `src/pismor/pipeline/aggregator.py`
- Test: `tests/test_aggregator.py`

**Interfaces:**
- Consumes: Task 1/2 的 `plan_topic_alias`、`flush_taxonomy_edits`、`revert_taxonomy_edits`。
- Produces: `_bare_topic(topic_new: str, taxonomy: Taxonomy) -> str`；`_stage_taxonomy` 返回统一 append（含 `topics`/`aliases`/`case`）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_aggregator.py`）

```python
def test_merge_to_writes_alias(conn, taxonomy_writable, fake_llm_merge):
    _seed_orphans(conn, topic_new="preference/回复要简短")
    run_daily(conn, taxonomy_writable, fake_llm_merge, auto_activate=True, now=NOW)
    pref = taxonomy_writable.dimensions["preference"]
    assert pref.topic_aliases["回复要简短"] == "lang"
    raw = yaml.safe_load(taxonomy_writable.path.read_text(encoding="utf-8"))
    pref_raw = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert pref_raw["topic_aliases"]["回复要简短"] == "lang"

def test_new_topic_writes_alias(conn, taxonomy_writable, fake_llm_new_topic):
    _seed_orphans(conn, topic_new="preference/回复要简短")
    run_daily(conn, taxonomy_writable, fake_llm_new_topic, auto_activate=True, now=NOW)
    pref = taxonomy_writable.dimensions["preference"]
    assert pref.topic_aliases["回复要简短"] == "brevity"

def test_merge_to_skips_shadow_alias(conn, taxonomy_writable, fake_llm_merge):
    _seed_orphans(conn, topic_new="preference/lang")
    run_daily(conn, taxonomy_writable, fake_llm_merge, auto_activate=True, now=NOW)
    assert "lang" not in taxonomy_writable.dimensions["preference"].topic_aliases

def test_unknown_prefix_kept_bare(conn, taxonomy_writable, fake_llm_merge):
    _seed_orphans(conn, topic_new="unknown/回复要简短")
    run_daily(conn, taxonomy_writable, fake_llm_merge, auto_activate=True, now=NOW)
    assert "unknown/回复要简短" in taxonomy_writable.dimensions["preference"].topic_aliases

def test_alias_reverted_when_transaction_fails(conn, taxonomy_writable,
                                               fake_llm_new_topic, monkeypatch):
    from pismor.pipeline import aggregator
    _seed_orphans(conn, topic_new="preference/回复要简短")
    conn.commit()
    monkeypatch.setattr(aggregator, "_rekey", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        run_daily(conn, taxonomy_writable, fake_llm_new_topic, auto_activate=True, now=NOW)
    assert "回复要简短" not in taxonomy_writable.dimensions["preference"].topic_aliases
    raw = yaml.safe_load(taxonomy_writable.path.read_text(encoding="utf-8"))
    pref_raw = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert "回复要简短" not in (pref_raw.get("topic_aliases") or {})
```

> `tests/test_aggregator.py` 顶部已有 `import yaml` 与 `_seed_orphans`、`taxonomy_writable`。`fake_llm_new_topic` 的 canonical 是 `brevity`。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_aggregator.py -k "alias" -v`
Expected: FAIL，别名未写入

- [ ] **Step 3: 实现**

1. 新增 `_bare_topic`：取 `topic_new.split("/", 1)`，仅当首段在 `taxonomy.dimensions` 时返回第二段，否则原串。
2. `_stage_taxonomy` 改为返回一条 append：`{"dim": dim, "topics": [topic] if added else [], "aliases": {...}, "case": {...}}`；别名用 `plan_topic_alias(taxonomy, dim, _bare_topic(topic_new, taxonomy), topic)` 计算，`None` 则省略。
3. `_apply_orphan_decision` 的 `merge_to` 成功分支也追加一条 alias append（无 topics/case）。
4. `run_daily` 里 `_flush_taxonomy` → `flush_taxonomy_edits`，`_revert_taxonomy` → `revert_taxonomy_edits`；删除两个旧函数。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_aggregator.py -v`
Expected: 全部 PASS（含原有 30+ 用例）

- [ ] **Step 5: 提交**

```bash
git add src/pismor/pipeline/aggregator.py tests/test_aggregator.py
git commit -m "feat(aggregator): write topic aliases on orphan promotion"
```

---

### Task 4: 复核返回值携带 `_pending_alias`

**Files:**
- Modify: `src/pismor/serving/review.py`
- Test: `tests/test_topic_alias.py`

**Interfaces:**
- Produces: `apply_review_decision(...)` 在 `merged_into` / `rekeyed_to` 返回体上附加 `"_pending_alias": {"dim": str, "alias": str, "canonical": str}`；其他返回不带该键。
- Consumes: 行内 `row["topic"]`（已是裸 topic）、入参 `topic`。

- [ ] **Step 1: 写失败测试**

```python
from pismor.serving.review import apply_review_decision

def _seed_confirmable(conn, topic="手工模型", scope="global", version="2026-01-01T00:00:00+00:00"):
    eid = f"preference_{topic}@{scope}"
    conn.execute(
        "INSERT INTO entries (user_source,user_id,dimension,topic,scope,id,confidence,status,"
        "body,summary,created_at,updated_at,last_reinforced_at,reinforcements)"
        " VALUES ('webchat','u1','preference',?,?,?,0.9,'pending_review','b','s',?,?,?,0)",
        (topic, scope, eid, version, version, version))
    conn.commit()
    return eid

def test_rekey_returns_pending_alias(conn, tax_writable):
    canon = tax_writable.dimensions["preference"].topics[0]
    eid = _seed_confirmable(conn)
    res = apply_review_decision(
        conn, user_source="webchat", user_id="u1", scope="global", entry_id=eid,
        decision="confirm", entry_version="2026-01-01T00:00:00+00:00",
        taxonomy=tax_writable, topic=canon)
    assert res["rekeyed_to"] == canon
    assert res["_pending_alias"] == {"dim": "preference", "alias": "手工模型", "canonical": canon}

def test_merge_returns_pending_alias(conn, tax_writable):
    canon = tax_writable.dimensions["preference"].topics[0]
    eid = _seed_confirmable(conn, topic="手工模型")
    _seed_confirmable(conn, topic=canon)
    # 目标行 status 改为 active，避免同键约束外的情况
    conn.execute("UPDATE entries SET status='active' WHERE id=?", (f"preference_{canon}@global",))
    conn.commit()
    res = apply_review_decision(
        conn, user_source="webchat", user_id="u1", scope="global", entry_id=eid,
        decision="confirm", entry_version="2026-01-01T00:00:00+00:00",
        taxonomy=tax_writable, topic=canon)
    assert res["merged_into"] == canon
    assert res["_pending_alias"]["alias"] == "手工模型"

def test_no_pending_alias_without_topic(conn, tax_writable):
    eid = _seed_confirmable(conn)
    res = apply_review_decision(
        conn, user_source="webchat", user_id="u1", scope="global", entry_id=eid,
        decision="confirm", entry_version="2026-01-01T00:00:00+00:00",
        taxonomy=tax_writable)
    assert "_pending_alias" not in res
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_topic_alias.py -k pending_alias -v`
Expected: FAIL，`KeyError: '_pending_alias'`

- [ ] **Step 3: 实现**

在 merge 成功 return 与 rekey 成功 return 前构造 `pending = {"dim": row["dimension"], "alias": row["topic"], "canonical": topic}`，仅当 `taxonomy` 非空且 `topic` 非空时附加。不调用 `plan_topic_alias`、不写文件。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_topic_alias.py tests/test_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/pismor/serving/review.py tests/test_topic_alias.py
git commit -m "feat(review): return pending topic alias on confirm"
```

---

### Task 5: serving API 写别名并热更新

**Files:**
- Modify: `src/pismor/core/taxonomy_edit.py`（新增 `write_pending_alias`）
- Modify: `src/pismor/serving/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `write_pending_alias(taxonomy, pending, *, backup_root=None, keep=30) -> Taxonomy | None`：`plan_topic_alias` + `flush_taxonomy_edits(label="taxonomy-alias")`，无新增返回 `None`。
- Consumes: Task 4 的 `_pending_alias`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api.py 新增本地 fixture：taxonomy 拷贝到 tmp
@pytest.fixture
def writable_settings(tmp_path):
    dst = tmp_path / "taxonomy.yaml"
    shutil.copy(REPO / "config" / "taxonomy.yaml", dst)
    return Settings(db_path=tmp_path / "api.db", taxonomy_path=dst, llm=..., embedding=...)

@pytest.fixture
def writable_client(writable_settings, fake_llm, embedder_stub):
    return TestClient(create_app(writable_settings, llm=fake_llm, embedder=embedder_stub))

def test_review_decision_writes_alias(writable_client, writable_settings):
    tax = load_taxonomy(writable_settings.taxonomy_path)
    canon = tax.dimensions["preference"].topics[0]
    client = writable_client
    with closing(get_conn(writable_settings.db_path)) as conn:
        eid = _seed_pending(conn, topic="手工模型", version=NOW)
    r = client.post(f"/review/webchat/u1/global/{eid}/decision",
                    json={"decision": "confirm", "entry_version": NOW, "topic": canon})
    assert r.status_code == 200
    assert "_pending_alias" not in r.json()
    raw = yaml.safe_load(writable_settings.taxonomy_path.read_text(encoding="utf-8"))
    pref = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert pref["topic_aliases"]["手工模型"] == canon
    assert client.app.state.taxonomy_store.current().dimensions["preference"].topic_aliases["手工模型"] == canon

def test_alias_write_failure_does_not_fail_review(writable_client, writable_settings, monkeypatch):
    monkeypatch.setattr("pismor.core.taxonomy_edit.write_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    ...
    assert r.status_code == 200 and r.json()["updated"] == 1
```

> `_seed_pending` 复用 `test_api.py` 里 `_seed_entry` 的写法；`import yaml`、`from contextlib import closing` 补到文件顶部。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_api.py -k alias -v`
Expected: FAIL，YAML 无别名／响应含 `_pending_alias`

- [ ] **Step 3: 实现**

1. `core/taxonomy_edit.py` 增加 `write_pending_alias`。
2. `serving/api.py` 的 `review_decision`：`with conn:` 内赋给 `result` 而不是直接 return；退出后 `pending = result.pop("_pending_alias", None)`；有则
   ```python
   try:
       new = write_pending_alias(taxonomy_store.current(), pending,
                                 backup_root=s.backup.root / "config", keep=s.backup.keep)
       if new is not None:
           taxonomy_store.replace(new)
   except Exception:
       logger.warning("topic alias write failed: %s", pending, exc_info=True)
   ```
3. `return result`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/pismor/core/taxonomy_edit.py src/pismor/serving/api.py tests/test_api.py
git commit -m "feat(api): persist pending topic alias after review commit"
```

---

### Task 6: admin 复核路由写别名

**Files:**
- Modify: `src/pismor/admin/routers/review.py`
- Test: `tests/admin/test_review.py`

**Interfaces:**
- Consumes: Task 5 的 `write_pending_alias`；`request.app.state.taxonomy_store`。
- Behavior: store 存在 → `replace`；不存在 → 只写文件并 `logger.warning`。

- [ ] **Step 1: 写失败测试**

```python
# tests/admin/test_review.py
def test_admin_review_writes_alias(tmp_path):
    tax_path = tmp_path / "taxonomy.yaml"
    shutil.copy(REPO / "config" / "taxonomy.yaml", tax_path)
    app, client, db_path = _make_app(tmp_path, taxonomy_path=tax_path)  # 扩展 helper 支持传路径
    canon = load_taxonomy(tax_path).dimensions["preference"].topics[0]
    with closing(get_conn(db_path)) as conn:
        eid = _insert_entry(conn, topic="手工模型", status="pending_review")
    r = client.post("/admin/api/review/decision",
                    json={"user_source": "webchat", "user_id": "u1", "scope": "global",
                          "entry_id": eid, "decision": "confirm",
                          "entry_version": VERSION, "topic": canon})
    assert r.status_code == 200
    raw = yaml.safe_load(tax_path.read_text(encoding="utf-8"))
    pref = next(d for d in raw["dimensions"] if d["key"] == "preference")
    assert pref["topic_aliases"]["手工模型"] == canon
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/admin/test_review.py -k alias -v`
Expected: FAIL，YAML 无别名

- [ ] **Step 3: 实现**

与 Task 5 相同：`with conn:` 内赋值 `result`，退出后 pop + `write_pending_alias` + `store.replace`；异常只告警。`_make_app` 增加可选 `taxonomy_path` 参数（默认仍是真实配置，保持现有用例不变）。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/admin/test_review.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归并提交**

```bash
uv run pytest -q
git add src/pismor/admin/routers/review.py tests/admin/test_review.py
git commit -m "feat(admin): persist topic alias on review decision"
```

---

## 自检记录

- Spec §5.1 守卫 → Task 1；§5.1 flush/revert → Task 2；§5.2 日批 → Task 3；§5.3 人工复核 → Task 4/5/6；§6 测试 → 各任务测试步骤。
- 类型一致性：`plan_topic_alias` 返回 `dict | None`；append 键 `dim`/`topics`/`aliases`/`case`；`_pending_alias` 键名三处一致。
- Review Focus 五条均有对应断言：第 1 条 Task 3 `test_unknown_prefix_kept_bare`；第 2 条同用例；第 3 条 Task 2 `test_flush_does_not_overwrite_existing_alias`；第 4 条 Task 5 写失败用例；第 5 条 Task 1 `test_plan_alias_never_overwrites`。
