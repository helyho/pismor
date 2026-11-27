# 晋升时自动补 topic_aliases 设计

- 日期：2026-11-27
- 状态：待批准
- 规模：small change（4 个源码文件 + 1 个新测试文件，不改 DB schema）
- 决策来源：会话中确认两条路径都补；`merge_to` 直接补，限定「只增不改、跳过 shadow、跳过 disabled、挂 decision.dimension、同批 appends 可回滚」

## 1. 背景与意图

`topic_aliases` 目前只人工维护，抽取时 `extractor._to_candidate` 命中即把自由 topic 改写成 canonical。两条把词表外主题转正的路径都只改条目与 `topics`，不写别名：

1. **日批自动转正**（`aggregator`）：orphan 组 30 天内 ≥3 行 → LLM 判定 `merge_to` / `new_topic`。
2. **人工复核归一**（`review.apply_review_decision(topic=...)`）：人指定白名单 topic，合并或 re-key。

结果：条目转正了，但原始自由说法没有沉淀，下次换措辞仍进观察区。本次让两条路径在转正时把「裸自由主题 → canonical」写进同维度 `topic_aliases`，后续抽取直接收敛。

## 2. 目标 / 非目标

**目标**

- 日批 `merge_to` / `new_topic` 成功后自动补别名；
- 人工复核 `confirm + topic` 的 `merge` 或 `rekey` 成功后自动补别名；
- 别名写入与现有 `taxonomy.yaml` round-trip 机制一致：保留注释与键顺序、版本冲突重试、事务回滚不留痕；
- 补别名失败不影响条目转正本身（降级为告警）；
- 运行时 taxonomy 热更新，抽取立即生效。

**非目标**

- 不改 DB schema、不加别名来源账本（`meta.auto_aliases`）；
- 不改管理端条目编辑（`admin/entry_service.py`）——它允许改成词表外 topic，语义是改存量，不是归一晋升；
- 不改 `topic_aliases` 的管理端校验规则；
- 不改 LLM prompt 与归并阈值。

## 3. 关键约束（来自现有实现）

1. `Dimension` 是 `frozen=True`，但 `topics` 是 `list`、`topic_aliases` 是 `dict`，可原地修改；`taxonomy.dimensions` 是普通 dict。
2. 现有 `aggregator._flush_taxonomy` 在**事务提交后**执行 round-trip 写回，失败会冒泡；`_revert_taxonomy` 只在事务异常时撤销内存。
3. 管理端校验规则（`config_service.validate_taxonomy`）：
   - 别名 key 不能与 `topics` 同名（shadow 会覆盖白名单原义）；
   - 别名 value 必须在 `topics` 内；
   - value 指向 `disabled_topics` 只告警（运行时会丢弃）。
4. orphan 行的 `topic_new` 形如 `<原维度或幻觉维度>/<裸主题>`；`entries.dimension` 恒为 `orphan`，原维度只在 `topic_new` 前缀里。
5. `apply_review_decision` 不 commit、不碰文件，由调用方开事务；`serving/api.py` 与 `admin/routers/review.py` 都在 `with conn:` 内直接 return，需改为「事务内取结果 → 事务外写文件 + 热更新」。

## 4. 方案与取舍

**方案 1（选用）：统一 `taxonomy_edit` 模块，事务外落盘。**
新增 `src/pismor/core/taxonomy_edit.py`，提供 `plan_topic_alias` / `flush_taxonomy_edits` / `revert_taxonomy_edits`。日批复用同一批 `appends`；人工复核把待写别名放进返回值内部键，由调用方在 commit 后 flush。

**方案 2：各自内联。** 日批在 `_flush_taxonomy` 加 alias 分支，复核里复制一份写文件代码。改动少但守卫重复、易漂移，弃用。

**方案 3：别名存 DB 表。** 破坏配置单一来源，抽取需合并 yaml + DB，管理端要改，弃用。

## 5. 设计

### 5.1 新模块 `core/taxonomy_edit.py`

```python
def plan_topic_alias(taxonomy, dim_key: str, alias: str,
                     canonical: str) -> dict | None
def flush_taxonomy_edits(taxonomy, appends, *, backup_root=None,
                         keep: int = 30, label: str = "taxonomy-edit") -> Taxonomy | None
def revert_taxonomy_edits(taxonomy, appends) -> None
```

**append 结构**（统一，字段可缺省）：

```python
{
  "dim": "preference",
  "topics": ["新桶"],              # 仅 new_topic 用；空列表表示不动
  "aliases": {"沟通方式": "style"},
  "case": {"quote": ..., "expect": {...}},  # 仅 new_topic 用
}
```

**`plan_topic_alias` 守卫**（任一不过 → 返回 `None`，不抛错、不写）：

1. `alias`、`canonical` 去空白后非空；
2. `dim_key` 在 `taxonomy.dimensions` 内；
3. `alias == canonical` → 跳过（已在白名单，别名无意义）；
4. `alias` 在 `d.topics` 内 → 跳过（shadow，管理端校验也不允许）；
5. `alias` 在 `d.disabled_topics` 内 → 跳过；
6. `canonical` 不在 `d.topics` 内 → 跳过；
7. `canonical` 在 `d.disabled_topics` 内 → 跳过；
8. `d.topic_aliases` 已有 `alias` → 跳过（只增不改；无论已有值是人工还是自动）。

通过后立即 `d.topic_aliases[alias] = canonical`，返回 append 项：

```python
{"dim": dim_key, "aliases": {alias: canonical}}
```

**`flush_taxonomy_edits`**：沿用现有 `_flush_taxonomy` 的两轮版本冲突重试：

```python
for attempt in range(2):
    data, _text, version = read_yaml(taxonomy.path)
    for a in appends:
        for d in data["dimensions"]:
            if d.get("key") != a["dim"]: continue
            # topics 追加（去重）
            # aliases：d.setdefault("topic_aliases", {}).setdefault(alias, canonical)
            # case 追加
    write_config(taxonomy.path, dump_yaml(data), expected_version=version,
                 backup_root=backup_root, keep=keep, label=label)
    return load_taxonomy(taxonomy.path)
```

别名写入同样用 `setdefault`，避免覆盖并发人工写入（只增不改在文件层再兜一次）。

**`revert_taxonomy_edits`**：按 append 逆序撤销内存：`topics` 移除本轮新增项，`topic_aliases` 删除本轮新增的 key（只删「plan 时记录的新增项」，不能误删已有映射）。为实现这点，`plan_topic_alias` 或调用方需要在 append 里记录 `"added_aliases": [...]`；设计上用 append 的 `aliases` 键即可——因为守卫 8 保证进入 append 的 key 必然是新增的。

### 5.2 日批接入（`pipeline/aggregator.py`）

`_apply_orphan_decision`：

- `merge_to` 校验通过后：
  ```python
  bare = _bare_topic(plan["topic_new"], taxonomy)
  append = plan_topic_alias(taxonomy, dim, bare, topic)
  if append: appends.append(append)
  ```
- `new_topic` 在 `_stage_taxonomy` 后调用同样的 `plan_topic_alias`；若桶名等于裸名，守卫 3/4 自动跳过。

`_bare_topic(topic_new, taxonomy)`：仅当去掉第一个 `/` 后的前缀是已知维度 key 时剥前缀，否则整串作为裸名。不依赖 `decision.dimension`，避免前缀与目标维度不一致时误剥。

`_stage_taxonomy` 改为产出统一 append（`topics` + `case`），与别名 append 合并成同一条，或分两条 append 均可；倾向**一条 append 带三类字段**，减少重复 dim 查找。

`run_daily` 的 `_flush_taxonomy` 调用替换为 `flush_taxonomy_edits(...)`；`_revert_taxonomy` 替换为 `revert_taxonomy_edits(...)`。原 `_flush_taxonomy`/`_revert_taxonomy` 删除，避免两份实现。

label：日批继续用 `taxonomy-activate`（含 topics + aliases + case）。

### 5.3 人工复核接入

**`serving/review.py`**

- 在 `merge` 成功与 `rekey` 成功两个分支，计算待写别名：
  ```python
  bare = row["topic"]          # 人工路径 row["topic"] 已是裸 topic
  d = taxonomy.dimensions.get(row["dimension"]) if taxonomy else None
  if d and topic: pending = {"dim": row["dimension"], "alias": bare, "canonical": topic}
  ```
- 返回体加内部键：`"_pending_alias": pending`（无则不带）。不改变对外字段（`updated` / `merged_into` / `rekeyed_to`）。
- 不调用 `plan_topic_alias`、不写文件、不改 taxonomy——本函数仍是无文件副作用的 DB 层。

**`serving/api.py`（`/review/.../decision`）**

```python
with conn:
    result = apply_review_decision(..., taxonomy=taxonomy_store.current(), ...)
pending = result.pop("_pending_alias", None)
if pending:
    _apply_pending_alias(taxonomy_store, pending, settings, s)
return result
```

`_apply_pending_alias`（放在 `serving/api.py` 或复用 `core/taxonomy_edit.py` 的辅助函数）：

1. `append = plan_topic_alias(store.current(), dim, alias, canonical)`；
2. `append` 为空 → 直接返回；
3. `new_taxonomy = flush_taxonomy_edits(store.current(), [append], backup_root=..., keep=..., label="taxonomy-alias")`；
4. `store.replace(new_taxonomy)`；
5. 任一步异常 → `logger.warning`，不改变已提交的复核结果。

**`admin/routers/review.py`（`/decision`）**

同一结构：`with conn:` 内只拿 `result` 与写 `record_admin_audit`，退出后判断 `_pending_alias`；`request.app.state.taxonomy_store` 存在则 `replace`，不存在则只写文件（`write_config` 成功后 `app.state.taxonomy = load_taxonomy(path)` 由 deps 决定，若没有 store 则跳过内存更新并告警）。

### 5.4 失败与并发

- **日批**：沿用现有语义——`flush_taxonomy_edits` 在事务提交后执行，失败冒泡；条目与 topics 已提交，内存 taxonomy 保留本轮追加（现有行为，不扩大改动）。别名与 topics 在同一文件写入中，失败时一起不落盘。
- **人工复核**：commit 已成功，别名写失败只告警。该自由说法未来再次进观察区并晋升时会重试，不会立即补。
- **并发人工保存**：`write_config` 的 `expected_version` + 两轮重试处理；`setdefault` 保证不覆盖并发写入的同一 alias。
- 同一 alias 重复晋升：守卫 8 幂等跳过。

## 6. 测试

新增 `tests/test_topic_alias.py`：

1. `plan_topic_alias` 六个拒绝分支各一例（空、同维不存在、alias==canonical、shadow、disabled alias、canonical 不在 topics）；
2. 只增不改：已有映射不覆盖；
3. `flush_taxonomy_edits` 写回后 `load_taxonomy` 能读到，且原有注释/键顺序不破坏（round-trip 断言）；
4. 版本冲突重试成功一例；
5. 日批 `merge_to` 集成：orphan 组 → 判定 → 别名落 yaml → 再抽取同说法命中 canonical，不再进 orphan；
6. 日批 `new_topic`：桶名等于裸名时不写别名；桶名不同时写别名；
7. `_bare_topic` 前缀剥离：已知维度前缀剥、未知前缀不剥；
8. `run_daily` 事务异常 → `revert_taxonomy_edits` 后内存与 yaml 均无别名；
9. 人工复核 `confirm + topic` 的 rekey 与 merge 两条分支：提交后 yaml 有别名、taxonomy_store 已热更新、响应体不含 `_pending_alias`；
10. 人工复核别名写失败（mock `write_config` 抛错）→ 复核结果仍成功。

## 7. 影响文件

| 文件 | 改动 |
|---|---|
| `src/pismor/core/taxonomy_edit.py` | 新增：`plan_topic_alias` / `flush_taxonomy_edits` / `revert_taxonomy_edits` |
| `src/pismor/pipeline/aggregator.py` | `_stage_taxonomy` 产出统一 append；接入 `plan_topic_alias`；`_flush_taxonomy`/`_revert_taxonomy` 替换；新增 `_bare_topic` |
| `src/pismor/serving/review.py` | merge / rekey 分支返回 `_pending_alias` |
| `src/pismor/serving/api.py` | 事务外 flush + `taxonomy_store.replace` |
| `src/pismor/admin/routers/review.py` | 同上（兼容无 store 的情况） |
| `tests/test_topic_alias.py` | 新增 |

## 8. 待实现时确认

- `flush_taxonomy_edits` 的 `backup_root`/`keep` 来源：日批沿用 `taxonomy_backup_root`、`settings.backup.keep`；serving/api 与 admin 从 `settings` 取同样两项，若无备份配置则传 `None`/默认。
- 管理端 router 若无 `taxonomy_store`，内存热更新降级为「下次请求重新读文件」还是「显式 reload 到 `app.state.taxonomy`」，实现时按 `admin/deps.py` 现状选最小改动。
