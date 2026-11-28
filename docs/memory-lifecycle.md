# pismor 记忆约束与生命周期全览

> 本文以当前代码为唯一真相源，覆盖：硬性约束、入库、召回、状态变迁、降级、归档、复核、观察区与晋级、日批、归并任务、删除权与审计。
> 对应分支 `main`，最后核对：2026-11-27。
> 设计动机与取舍见 `docs/superpowers/specs/2026-11-23-user-profile-memory-design.md`；对外接口速览见 `docs/Introduce.md`。

## 0. 十句话速读

1. **SQLite 是唯一真相源**：条目、证据、审计、任务、已见事件全部落库；YAML 只是可热更新的配置词表。
2. **taboo 恒第一**：排序、必带、预算、冲突裁决、衰减豁免、词表冻结六个环节都单独硬编码，不依赖配置。
3. **一条记忆 = 四元组 + 证据链**：`(user_source, user_id, dimension, topic)` 唯一；`entries.id = sources.entry_id = dimension_topic`。
4. **无 quote 不入库**：写入器与审计双重把关；quote 是用户原话，不是模型摘要。
5. **词表即边界**：维度/主题/作用域必须来自 taxonomy；词表外的话题只能进观察区或人工复核，不能静默落库。
6. **显式才直通**：`confidence ≥ gate.auto_min` 且 `kind == explicit` 才是 auto；`inferred` 置信压顶 0.8；低于 `review_min` 直接丢。
7. **演进靠状态机**：`active ⇄ degraded → archived`，`pending_review/orphan_topic` 是待裁决入口；非法迁移单条隔离。
8. **冲突 taboo 恒胜**：preference 与 taboo 同主题必然降级 `conflict_lost`（×0.8），并留互标与澄清待办。
9. **降级不等于删除**：90 天无强化且低置信 → degraded；degraded 再满 180 天 → archived；archived 不再参与召回。
10. **一切每日自动推进**：日批单事务完成观察区晋级、衰减、归档、复核过期、澄清过期，提交后才写回词表并备份。

---

## 1. 数据模型与键约定

### 1.1 持久化表

| 表 | 作用 | 关键约束 |
|---|---|---|
| `entries` | 记忆条目主体 | 五元组主键；`status` 五态；`id` 全局定位 |
| `sources` | 证据链（用户原话） | `quote NOT NULL`；`entry_id` 指向 `entries.id` |
| `embeddings` | sqlite-vec 向量 | `entry_id` 主键；维度由配置固定 |
| `review_history` | 复核/审计/失败流水 | 记录 `decision`、`actor`、`entry_version` |
| `pending_clarifications` | 待用户澄清的矛盾对 | 主键含 `conflicting_ids`；行级 `ttl_days` |
| `events_seen` | 入站幂等 | `(source, event_id)` 主键 |
| `tombstones` | 删除权 | 用户级永久标记；`purged_at` 表示已彻底清除 |
| `merge_tasks` | taboo 超载归并任务 | 部分唯一索引：每用户最多一条 `open` |
| `admin_audit` | 管理端操作审计 | 只追加 |
| `job_runs` | 日批运行记录 | `stats_json`、`error` |
| `mem_fts` | FTS5 词面召回 | `terms = fts_terms(summary)`，`rowid = entries.rowid` |

### 1.2 状态集合

`status` 只有五种（DB CHECK 强制）：

`pending_review`、`orphan_topic`、`active`、`degraded`、`archived`。

> 状态机里出现 `deleted` 只表示“删除”动作的目标态；实际删除是物理 `DELETE`，不会留下 `deleted` 行。

### 1.3 键与命名

- 规范条目：`variant='normal'`、`variant_seq=0`，`topic` 就是词表主题；`id` 是稳定行键（`keys.new_id()`），不再由 dimension/topic 派生。
- 冲突变体：`variant='conflict'`、`variant_seq` 递增，`topic` 仍是基础主题；状态限 `active/degraded/archived`。
- 历史轨迹行：`variant='trace'`、`variant_seq` 递增，必须 `archived`，`degraded_reason='superseded'`。
- 待复核快照：`variant='snapshot'`、`variant_seq` 递增，仍为 `pending_review`。
- `sources.entry_id` 指向稳定的 `entries.id`；改键/合并不再迁移 id，同 topic 的全部 variant 级联改 topic。

---

## 2. 阈值速查

| 配置/常量 | 默认 | 作用 |
|---|---|---|
| `gate.auto_min` | 0.85 | explicit 直通门槛；同时是管线复活 degraded 的门槛 |
| `gate.review_min` | 0.60 | 入复核队列下限，低于即丢 |
| `gate.inferred_cap` | 0.80 | inferred 候选置信压顶 |
| `pipeline.known_ids_limit` | 200 | 注入抽取器的已知 id 上限 |
| `pipeline.reinforce_top_k` | 20 | 无变化轮次 BM25 强化条数 |
| `pipeline.trigger_long_message` | 300 | 长消息触发抽取（严格大于） |
| `pipeline.max_body_chars` | 200 | body 截断长度 |
| `aggregator.degrade_days` | 90 | 无强化降级窗口（所有非 taboo 维度共用） |
| `aggregator.degrade_confidence` | 0.70 | 降级置信上限（严格小于） |
| 归档窗口 | 180 天 | 硬编码：degraded 且 `updated_at` 超期即归档 |
| 复核过期 | 14 天 | 硬编码：pending_review 从 `created_at` 起算 |
| 澄清 TTL | 7 天 | 行级 `ttl_days`，按 `issued_at` 起算 |
| 观察区窗口/阈值 | 30 天 / 3 次 | 近 30 天复现 ≥3 才晋级 |
| `assembler.taboo_merge_threshold` | 40 | taboo 超载阈值（严格大于） |
| `assembler.taboo_budget_share` | 0.6 | 超载时 taboo 段预算占比 |
| `assembler.related_top_k` | 20 | 相关集召回条数 |
| `assembler.half_life_days` | 30 | 时间衰减半衰期 |
| `assembler.clarify_ttl_days` | 7 | 澄清问题默认 TTL |
| `embedding.min_similarity` | 0.5 | 向量召回 cosine 下限 |
| `intake.max_content_chars` | 8192 | text 内容长度上限 |
| `backup.keep` | 30 | 备份保留份数 |
| `serve.daily_interval_seconds` | 86400 | 日批周期 |

配置校验只要求 `gate.auto_min > gate.review_min`（两者都在 0~1），没有“auto_min 不得低于 0.6”的额外下限。

---

## 3. 硬性约束

### 3.1 taboo 恒第一（最重要）

“taboo 优先”不是一条 SQL，而是九个独立落地点。任何新功能都必须重复检查这九处：

| # | 位置 | 行为 |
|---|---|---|
| 1 | `writer.apply_actions` | 同批动作按 `candidate.dimension == 'taboo'` 稳定排序，taboo 先落库；同批新增 preference 时能查到已存在的 taboo |
| 2 | `writer._add` | 同 user/topic 已有存活 taboo → 新行直接 `degraded/conflict_lost`，置信 ×0.8，并给 taboo 补 `conflict_with` |
| 3 | `writer._conflict` | 新候选是 taboo，或目标主题已有存活 taboo → taboo 侧保持/落为 `active`，对方降级；旧行是 taboo 时只补标记，不降状态、不降置信 |
| 4 | `resolver.resolve` | preference 候选存在同 topic 存活 taboo → 强制 `CONFLICT`，不给 LLM 选择空间 |
| 5 | `assembler._must_have` | taboo 与 identity 无条件进入必带集，不受 `query`、`dimensions` 白名单影响 |
| 6 | `assembler._dimension_rank` | `taboo` 硬编码 rank 0，即使 taxonomy 把别的维度 priority 调得更小也压不过 |
| 7 | `assembler._build` | 排序第一键是维度 rank，所以预算耗尽时先保 taboo；超载时 taboo 段单独吃 60% 预算（跳过而非整段中断） |
| 8 | `aggregator._decay` | SQL 明确 `dimension != 'taboo'`，taboo 永不因时间衰减 |
| 9 | `aggregator._activate_orphans` / `_queue_audit` | 观察区里的 taboo 不自动删除、不自动建桶；新主题若声明为 taboo 只入人工队列，禁止运行期扩充 |

补充：`runner._settle_pair` 中，澄清回答也不会把 taboo 行降级；`audit` 另有 `taboo_no_overlap` 与 `taboo_negation` 两条专门规则。

### 3.2 词表与主题边界

- 维度、主题、作用域必须来自 `config/taxonomy.yaml`；词表外维度统一进观察区（`orphan`）。
- 词表外主题：非 taboo → 观察区；taboo → 强制复核（`force_review`），不会以新主题身份直接入库。
- `disabled_topics` 只停用新写入：抽取直接丢弃；存量条目仍可读、可召回、可审计。
- 主题别名 `topic_aliases` 只在 canonical 目标仍合法时改写，避免别名把条目带出词表。
- taboo 的新主题永远不能由日批自动写入；`meta.auto_activate` 只对非 taboo 生效。
- 被引用的主题不能硬删，只能停用（配置校验强制）。

### 3.3 证据与安全

- **无 quote 不写**：`_add`、`_update`、`_conflict`、`_insert_entry`、`_write_pending` 均拒绝空 quote。
- **指令注入过滤**：`INSTRUCTION_RE` 同时扫 `body + quote + summary`，命中即丢；审计再扫一次 `body`。
- **敏感主题**：只认 `dimension.topic_overrides[topic].sensitive`（不认维度级 `sensitive`）；置信压到 ≤0.6，正文/原话命中 `\d+号|\d+栋|确诊|处方` 直接丢弃。
- **敏感新主题**：观察区晋级时若目标主题敏感，只入人工队列，不自动建桶。
- **inferred 压顶**：`kind == inferred` 先压到 ≤0.8，因此永远无法 auto 直通。

### 3.4 主键、状态与一致性

- 四元组唯一；同一主题不允许两条规范行。
- `id` 与 `sources.entry_id` 必须同步；改键/合并必须迁移证据链。
- 状态必须属于五态；迁移必须走合法边（见 §5），非法迁移抛 `IllegalTransition`，由写入器逐条隔离。
- `~` 前缀行必须 `archived`；`+` 前缀行只能 `active/degraded`；`!` 前缀行是 `pending_review` 快照。
- `degraded` 必须有 `degraded_reason`；`archived` 必须有 `archived_at`；`orphan_topic` 必须有 `topic_new`。
- 审计只读，标记动作单独执行；不允许审计顺手改正文。

### 3.5 幂等、并发与删除权

- 入站以 `(source, event_id)` 幂等；缺 `event_id` 时用规范化内容哈希兜底。
- 复核使用乐观锁：`id + updated_at == entry_version`，先到者生效，后到返回 `noop`。
- 审计标记同样用 `updated_at` CAS，且只处理 `active` 行。
- tombstone 是永久口径：写入管线跳过、装配返回空包、`POST /events` 返回 410；软删除可恢复，硬删除保留 tombstone 并记 `purged_at`。

---

## 4. 入库链路

```
POST /events
  → intake（校验 + 幂等）
  → normalize（对话/文档）
  → 触发分级（仅 dialogue）
  → extractor（LLM 抽取 + 候选过滤）
  → gate（auto / review / dropped）
  → resolver（ADD / UPDATE / CONFLICT / NOOP / ORPHAN）
  → writer（单事务落库 + 证据 + 向量）
```

### 4.1 接入与幂等（`serving/api.py` + `pipeline/intake.py`）

1. tombstone 用户直接 `410 {"detail": "memory deleted"}`，不建事件。
2. 入站结构经 pydantic 校验；`text.content` 上限 `intake.max_content_chars`。
3. `force_extract` 只认严格 `True`；`event_id` 缺失时由 `resolve_event_id` 生成并回写。
4. 先 `INSERT events_seen(source, event_id, ts)`；唯一冲突或影响行数为 0 → 回滚并返回“重复”，HTTP 200 `{deduplicated: true}`。
5. 成功提交后投递后台任务并立即返回 202；后台失败会撤销 `events_seen`，允许同一事件重放。

### 4.2 归一化（`pipeline/normalize.py`）

| 输入 | 模式 | 抽取文本 | 用户域证据 |
|---|---|---|---|
| `conversation` | `dialogue` | 全部消息按 `role: text` 拼上下文 | 仅 `role == user` 且非空的消息 |
| `text` | `document` | 整段 `content` | 整段 `content` |

`last_user_text` 取最后一条非空用户消息，用于澄清回流判定。

### 4.3 触发分级（`pipeline/trigger.py`）

只有 dialogue 走触发分级；document 直通抽取。顺序严格如下：

1. **explicit 关键词**：18 个中英文词（记住 / 以后 / 别再 / 不要 / 默认 / 总是 / 永远 / remember / from now on / always / never / stop / don't / do not / prefer / keep in mind / going forward 等）。
2. **长消息**：任一用户消息长度 `> trigger_long_message`（300）。
3. **force_extract**：显式强制。
4. **first_query**：该用户尚无 `active/degraded` 条目。
5. 否则 **不调 LLM**，只做词面强化：`mem_fts` BM25 取 top 20，`reinforcements += 1`、刷新 `last_reinforced_at`。

> 强化会刷新 `last_reinforced_at`，因此能阻止 90 天 stale 降级；但不会改变状态，也不会复活 degraded。

### 4.4 抽取与候选过滤（`pipeline/extractor.py`）

抽取提示只注入：维度 key/name、启用的 topics、敏感 topics、guidance（压到 200 字）、已知 id 列表。**不注入条目正文**，避免模型锚定旧内容。

候选过滤链（顺序有意义）：

1. 维度不在词表 → `orphan`；`topic_new` 缺失时尝试 `{dimension}/{topic}`，仍为空则丢弃。
2. 合法维度：先做 `topic_aliases` 归一。
3. `disabled_topics` → 丢弃。
4. topic 不在词表：
   - taboo → `force_review`（`topic_new` 清空；topic 为空时用 `unclassified`）；
   - 非 taboo → 转观察区（`orphan` + `topic_new`）。
5. `INSTRUCTION_RE` 命中 body/quote/summary → 丢弃。
6. 敏感主题：置信 `min(conf, 0.6)`；正文或原话命中敏感细节正则 → 丢弃。
7. 截断：`body` 200 字、`summary` 20 字；空 quote 置空（后续写入器会拒绝）。

### 4.5 置信门槛（`pipeline/gate.py`）

| 条件 | 去向 |
|---|---|
| `force_review == True` | 直接进复核队列 |
| `kind == inferred` | 先压到 ≤ `inferred_cap` |
| `conf ≥ auto_min` 且 `kind == explicit` | auto，直接进写入器 |
| `conf ≥ review_min` | 进 `pending_review` 队列 |
| 其他 | 丢弃 |

注意：`orphan` 候选也要先过这道门槛；低于 `review_min` 的观察区候选同样会被丢掉。

### 4.6 动作解析（`pipeline/resolver.py`）

- `orphan` 候选：直接输出 `ORPHAN`，不调 LLM。
- 无同键条目：
  1. 先做近邻互斥检测：只检查 `taboo ↔ preference`、`active`、排除 `~/+/!`、LIMIT 5；
  2. LLM 判定互斥 → `CONFLICT`，否则 `ADD`。
- 同键存在：
  - 旧行 `pending_review` → 直接 `UPDATE`（不再问 LLM，避免队列自我阻塞）；
  - 否则 LLM 在 `UPDATE / CONFLICT / NOOP` 中三选一；
  - preference 候选遇到同 topic 存活 taboo → 无条件覆盖为 `CONFLICT`（双保险）。

### 4.7 写入（`pipeline/writer.py`）

- 向量在事务外预计算（批量一次，失败逐条重试），条目、证据、向量在同一事务边界内提交。
- 单条失败（`IntegrityError` / `IllegalTransition`）会让整批回滚，然后 orchestrator 逐条重放隔离；失败条写 `review_history(decision='apply_failed')`。
- `ADD`：无 quote 拒绝；taboo 冲突则降级落库；否则 `active`。
- `UPDATE`：旧值转 `variant='trace'` 的 archived 轨迹行，原行覆盖为新值；`pending_review` 先转 `variant='snapshot'` 腾出 normal 槽，再插新行。
- `CONFLICT`：新值放 `variant='conflict'` 的副本行；双方降级或 taboo 单侧取胜。
- `NOOP`：只强化，不改状态。
- `ORPHAN`：写 `orphan_topic`，同 `topic_new` 复现只累加强化次数。

---

## 5. 状态变迁

### 5.1 合法迁移图

| 从 | 到 | 触发 |
|---|---|---|
| `active` | `degraded` | 冲突落败、日批 stale |
| `active` | `archived` | 冲突对手被裁决后归档等 |
| `degraded` | `active` | 新证据达到复活门槛、复核 confirm、澄清裁决 |
| `degraded` | `archived` | 日批 180 天归档 |
| `pending_review` | `active` | 复核 confirm |
| `pending_review` | `deleted` | 复核 remove（物理删除） |
| `orphan_topic` | `active` | 观察区晋级、复核 confirm |
| `orphan_topic` | `deleted` | 观察区废弃（物理删除） |

`INSERT` 可以选择任意初始状态；`NOOP` 不改变状态，因此不校验迁移。

### 5.2 演进写法：原行覆盖 + 轨迹行

主键不含 `status`，所以 `UPDATE` 的语义是：

1. 先 `_check_transition(old.status, 'archived')` 拦住不可演进的旧态（例如 archived 不能再更新）。
2. 把旧行完整复制成 `variant='trace'`、`variant_seq` 递增的 `archived` 轨迹行，`degraded_reason = 'superseded'`，证据链迁移到轨迹行。
3. 原行原地覆盖新 `body/summary/confidence`，状态回到 `active`，清空 `degraded_reason/conflict_with/topic_new`，刷新 `updated_at` 与 `last_reinforced_at`。
4. 追加一条新 `sources`（kind 取候选的 `explicit/inferred/clarification`）。

效果：当前视图只有一份新值，历史值永远可追。

### 5.3 待复核快照

`UPDATE` 命中 `pending_review` 时：

1. 旧行 topic 改为 `!{topic}`（必要时追加序号），仍保持 `pending_review`；
2. 同步 `id`、`sources.entry_id`、以及其他行 `conflict_with` 中的旧 id；
3. 规范键插入新 `active` 行。

这样复核队列里仍能看到旧候选，但不会阻塞新事实落库。

### 5.4 degraded → active 的复活门槛

- 管线自动写入：`confidence >= revive_min_confidence`，而 `revive_min_confidence = settings.gate.auto_min`（默认 0.85）。
- 未达标：只做 `_noop`（强化 +1、刷新时间），不覆盖正文、不改状态、不产生轨迹行，并写 `review_history(decision='revive_rejected')`。
- 人工复核 confirm、澄清裁决：不受门槛限制（`_apply_clarification` 显式传 0.0）。

> 设计与行为一致：调高 `auto_min` 会同时收紧“显式直通”和“自动复活”；调低则两者一起放宽。

### 5.5 冲突裁决

- 普通矛盾：双方 `degraded / conflict_lost`，置信 ×0.8，并互相写 `conflict_with`。
- 一方是 taboo：taboo 保持 `active`，对方降级；taboo 行只补 `conflict_with`。
- 新候选是 taboo：新行 `active`，旧行降级。
- 冲突行统一用 `variant='conflict'`，与 normal 行同 topic 并存，避免覆盖规范键。
- `resolve_conflicts` 只做精确匹配：按完整 `id` 定位；`revive=True` 时只恢复 `degraded_reason == 'conflict_lost'` 的对方。

### 5.6 合并条目

`writer.merge_entries(src → dst)`：

- 证据链迁移到 dst；
- 其他行 `conflict_with` 中的 src 标记重定向到 dst；
- dst 置信取两者最大，强化次数相加；
- 物理删除 src 行与 src 向量；
- 返回 `{'merged': bool}`，事务由调用方负责。

用于复核 confirm 时把条目归一到一个已存在的规范主题。

---

## 6. 冲突与澄清闭环

### 6.1 何时产生澄清

装配时扫描 `active/degraded` 且 `conflict_with` 非空的条目，但**只认双向对称标记**：A 指向 B，且 B 也指向 A。单向标记不会打扰用户。

- 对称对按双方置信排序，生成固定问句：
  `你之前提过"{s_a}"，也提过"{s_b}"——日常对话里以哪种为准？`
- 写入 `pending_clarifications`，主键 `(user_source, user_id, conflicting_ids)`。
- 同一对未过期则复用原问题；经由期则 UPSERT 刷新 `issued_at/ttl_days`。
- TTL 默认 7 天；管理预览可以只生成不落库。

### 6.2 用户回答的回流

1. dialogue 入库前先查该用户最新未过期的 pending；
2. LLM 二元判断“这句话是否在回答该问题”；命中才进入澄清链路，**不删除 pending**；
3. `extract_clarification` 抽取候选：`kind` 统一为 `clarification`，置信至少 0.85，禁止 orphan；
4. 逐个候选：无同键 → `ADD`；同键 `active/degraded/pending_review` → `UPDATE`；`archived` 跳过（不可复活）；
5. 写入使用 `revive_min_confidence=0.0`：这是用户本人裁决，不再设门槛；
6. 写入成功后才 `_settle_pair` 并 `consume` pending；全部失败则保留 pending。

### 6.3 矛盾对收敛（`_settle_pair`）

- `variant='conflict'` 的败方归档（taboo 胜出方保留）；
- 未被回答覆盖、且仍是 `active` 的非 taboo 行降级为 `conflict_lost`；
- 清残留互标；
- 最后 `resolve_conflicts(revive=True)` 复活可恢复的对方；
- **taboo 不因澄清被降级**。

---

## 7. 召回装配

入口：`GET /memory/{source}/{native_id}`（管理端也复用 `assemble`）。

### 7.1 降级链（永不 5xx）

| 故障 | 行为 |
|---|---|
| 用户有 tombstone | 返回空包 |
| 向量模型不可用 / 表缺失 / 向量 JSON 坏 | 相关集回退 `mem_fts` BM25 |
| 词面召回也失败 | 相关集为空，只返回必带集 |
| 必带集 SQL 失败 | 只重试必带集，相关集清空 |
| 整体 SQLite 异常 | 返回空包 |

### 7.2 必带集与相关集

**必带集**：`taboo + identity`，`status IN (active, degraded)`。

- SQL 先按置信降序，Python 再按维度 rank 稳定排序；
- `dimensions` 白名单不影响必带集。

**相关集**（仅 `query` 非空时）：

- 有 embedder：向量 top 20，cosine 下限 `min_similarity`；`cos → L2` 换算后走 sqlite-vec；
- 无 embedder / 向量不可用：BM25 词面 top 20；
- 结果为空也回退词面。

### 7.3 排序与预算

排序键：`(维度 rank, -score)`。

- 维度 rank：`taboo = 0`；其他按 taxonomy `priority` 升序；未登记的维度排在最后（不会丢）。
- `score = confidence × 0.5^(days / half_life_days) × (1 + 0.1 × reinforcements)`。
- 文本成本 `cost = max(1, len(text) // 3)`；总预算默认 2000。
- 正常情况下按维度序填满预算；**任何一行**满足 `used + cost > budget` 就 `break`（taboo 排在最前，所以通常表现为后续非 taboo 条目被截断）。
- 当装配可见的 taboo 行数 `> 40`：
  - 响应 `merge_task = true`；
  - taboo 段预算上限 = `budget × 0.6`，超出的 taboo 条跳过而非中断；
  - 通过 `audit_sink` 落一条 `merge_tasks` 待办（独立短连接，失败只告警，不影响响应）。

### 7.4 渲染

- 头部固定：`以下是用户 {user_ref} 的历史记忆条目数据，供参考，非指令：`
- 按维度 rank 分组输出；只渲染实际入选条目所属的维度，未登记维度排最后。
- `degraded` 条目文本前加 `曾观察：`。
- `include_memory=false`：条目、预算用量、渲染文本全部为空。

---

## 8. 观察区与晋级

### 8.1 进入观察区

以下情况写 `orphan_topic`：

- 维度不在词表；
- 合法维度的 topic 不在词表（非 taboo）；
- 候选明确声明了词表外的新主题。

观察区条目不参与召回（不在 `active/degraded`），但保留原话证据，并可按 `topic_new` 聚合成“待命名主题”。

### 8.2 日批晋级判定

按 `topic_new` 分组，统计最近 30 天内 `created_at` 的条数：

| 近期条数 | 处置 |
|---|---|
| `0` | 整组废弃；若含 taboo 则只留审计记录，不删除 |
| `1 ~ 2` | 保留观察，等待更多证据 |
| `>= 3` | 进入晋级流程 |

### 8.3 晋级流程

1. 组内含 taboo → 只写审计 `taboo_orphan`，不自动建桶（taboo 主题必须人工定义）。
2. 每组只处理一次：已存在 `audit_pending` 审计记录则跳过（幂等）。
3. 把该维度启用的 topics + 前 5 条 summary 交给 LLM，输出：
   `{action: merge_to | new_topic, dimension, topic}`。
4. 校验链（任一失败都只写审计，不写词表）：
   - action 只能是 `merge_to/new_topic`，dimension 必须在词表，topic 非空；
   - 目标 topic 不能是 disabled；
   - `merge_to` 目标必须已在该维度 topics 中；
   - 目标主题敏感 → 人工队列；
   - `new_topic` 且 dimension 是 taboo → 人工队列（禁止运行期扩充 taboo）；
   - `new_topic` 且 `meta.auto_activate != true` → 人工队列。
5. `_rekey`：逐行改成目标维度和主题，`status='active'`，清 `topic_new`，`id` 与 `sources.entry_id` 同步；目标键已被占用则整组转人工。
6. 自动新增主题会同时追加一条回归样例（quote 用该组首条正文），提交后写回 YAML 并保留注释；版本冲突重试一次。

> “人工队列”目前是 `review_history(decision='audit_pending', actor='audit')` 审计记录，不会出现在普通复核队列里，需要管理端审计视图处理。

---

## 9. 复核

### 9.1 队列口径

```
status = 'pending_review'
OR (topic LIKE '+%' AND status IN ('active','degraded') AND conflict_with IS NOT NULL)
```

排除 tombstone 用户。每项返回 `entry_version = updated_at`，供乐观锁使用。

### 9.2 决策

| 决策 | 行为 |
|---|---|
| `confirm` | 转正 / 归一主题 / 合并 / 快照归档 / 复活冲突对方 |
| `remove` | 物理删除条目、证据、向量 |
| `fix` | 只改正文与摘要 |

`confirm` 细节：

1. 目标状态：`orphan` 维度 → `orphan_topic`；其他 → `active`。
2. 若指定了新 topic：
   - topic 不在词表 → 返回 `invalid_topic`，不改数据；
   - 目标 topic 已有存活行 → `merge_entries` 合并过去，返回 `merged_into`；
   - 目标不存在 → 改键、迁移证据、清冲突标记，返回 `rekeyed_to`。
3. 若当前是 `variant='snapshot'` 快照：
   - 规范槽已被占用 → 快照转 `variant='trace'` 并归档（`superseded_by_evolution`）；
   - 否则同一条 UPDATE 改回 `variant='normal'`, `variant_seq=0`。
4. 最终 CAS 更新状态、正文/摘要（仅在提供时覆盖）、清 `degraded_reason/conflict_with`；
5. `resolve_conflicts(revive=True, clear_pending=True)` 复活因冲突落败的对方并清待澄清；
6. 写 `review_history`。

### 9.3 重要边界

- 所有写操作都带 `entry_version` CAS；版本不符返回 `noop`，先到者生效。
- `fix` 不改变状态、不清冲突、不做快照转换——它只修文字。
- 超过 14 天的 `pending_review` 会被日批物理删除，并写 `decision='expired'`。
- `variant='conflict'` 的争议行如果未被确认，会一直留在队列；确认/移除后按上述规则收敛。

---

## 10. 日批

入口：`scripts/serve.py` 的 `daily_once`；周期默认 86400 秒。

### 10.1 单轮顺序

整轮在**一个事务**内完成：

1. `_activate_orphans`：观察区废弃 / 保留 / 晋级；
2. `_open_merge_tasks`：taboo 超载用户开归并任务；
3. `_decay`：低置信且长期无强化 → degraded；
4. `_archive`：degraded 超 180 天 → archived；
5. `_drop_expired_reviews`：pending_review 超 14 天 → 物理删除；
6. `clarifier.expire`：澄清 TTL 过期清理。

事务提交后才把新主题/回归样例写回 taxonomy YAML；写回失败会回滚内存中的词表变更。异常时回滚并抛给 job 记录。

### 10.2 衰减（stale）

```sql
status='active'
AND dimension != 'taboo'
AND confidence < 0.7
AND julianday(last_reinforced_at) < julianday(now) - 90
→ status='degraded', degraded_reason='stale'
```

- 任何非 taboo 维度都用 90 天，没有按 context 90 / 其他 180 区分。
- 未变化轮次的 BM25 强化会刷新 `last_reinforced_at`，因此真实“沉默”才会计入 90 天。
- 衰减不刷新 `updated_at`。

### 10.3 归档

```sql
status='degraded' AND julianday(updated_at) < julianday(now) - 180
→ status='archived', archived_at=now
```

因为衰减不刷 `updated_at`，180 天实际从最后一次正文写入起算。归档后不参与召回，也不能被普通 `UPDATE` 复活。

### 10.4 复核过期

`pending_review` 从 `created_at` 起算超过 14 天：物理删除条目与证据，逐条写 `review_history(decision='expired', actor='audit')`。

### 10.5 归并任务

按用户统计所有 `active/degraded` 的 taboo 条数：

- 超过 40 → `INSERT OR IGNORE` 一条 `merge_tasks`（`status='open'`），并刷新已有 open 行的计数、候选空闲主题、更新时间；
- 不修改任何条目，只创建待办；
- `proposal_json` 包含 `taboo_count`、`free_topics`、`allowed_topics`，供管理端参考；
- 每用户最多一条 open 任务（部分唯一索引）。

### 10.6 审计与标记

日批在聚合后运行 8 条只读审计规则，然后只把 `active` 违规行标记为 `pending_review`：

- 规则：`taboo_no_overlap`、`taboo_negation`、`conflict_with_json`、`quote_required`、`inferred_cap`、`unique_key`、`status_consistency`、`instruction_pattern`。
- 标记使用 `updated_at` CAS；写 `review_history(decision='flagged', note=规则名)`。
- 归档行、观察区行、已删除用户不在标记范围。

### 10.7 备份与运行记录

- 每轮结束 `VACUUM INTO` 备份数据库到 `backup.root/db`，保留最近 `backup.keep` 份；
- 词表写回前会备份到 `backup.root/config`；
- `job_runs` 记录开始/结束、是否成功、统计 JSON 与错误；
- 服务启动时执行 `PRAGMA integrity_check`，非 `ok` 拒绝启动。

---

## 11. taboo 超载与归并任务

触发有两条独立路径：

1. **装配时**：本次可见 taboo 条数 > 40 → 响应 `merge_task=true`，并通过 `DbMergeTaskSink` 落任务；
2. **日批时**：该用户全量 taboo 条数 > 40 → 确保存在一条 open 任务。

字段：

| 字段 | 含义 |
|---|---|
| `taboo_count` | 当前 taboo 条数 |
| `proposal_json` | taboo 计数、可自由使用/允许的主题 |
| `status` | `open / done / dismissed` |

管理端：

- `GET /admin/api/merge-tasks` 列出任务；
- `POST /admin/api/merge-tasks/{id}/resolve` 只改任务状态；
- 真正的主题归一仍走复核 `confirm`（可带 `topic`），归并后写 `admin_audit`。

---

## 12. 删除权、审计与管理端

### 12.1 删除权

- `DELETE /memory/{source}/{native_id}` 默认**软删除**：只写 tombstone，返回 `mode='soft'`；可 restore。
- 硬删除：清空条目、证据、澄清、归并任务（`merge_tasks`）与向量；`admin_audit` 审计与 `events_seen` 幂等记录刻意保留。保留 tombstone 并写 `purged_at`；之后 restore 返回 `purged`。
- tombstone 生效后：写入管线跳过且不调 LLM；装配返回空包；事件接口 410。
- 备份中的旧数据最多滞留 30 天（由保留份数控制）。
- 条目级物理删除：管理端 `GET /admin/api/entries/delete-preview` 预览目标与同维度同主题的全部 conflict 副本，`DELETE /admin/api/entries` 执行；公开 `DELETE /memory/{source}/{user_id}/entries/{entry_id}` 默认同样级联，目标不存在返回 204。
- 条目级删除不可恢复：清理 entries、sources、embeddings、review_history、pending_clarifications 引用、其他行 `conflict_with` 标记与 `superseded_by` 指针，并刷新 taboo 归并任务；操作写 `admin_audit`（公开 API 的 actor 取 `x-requested-by`）。
- 复核台 `remove` 复用同一清理逻辑，但不级联 conflict，且保留 `updated_at` 乐观锁。

### 12.2 审计

8 条规则的判定口径：

| 规则 | 口径 |
|---|---|
| `taboo_no_overlap` | taboo 与 preference 近似同主题；已裁决（preference `conflict_lost` 或互标）则排除 |
| `taboo_negation` | taboo 的每条 quote 必须含否定词（不/别/禁/勿/拒绝/无需/避免/没有） |
| `conflict_with_json` | 标记必须是合法 JSON 数组；坏 JSON 只报违规，不中断审计 |
| `quote_required` | 非 `~` 行必须至少有一条证据 |
| `inferred_cap` | inferred 证据的条目置信不得 > 0.8 |
| `unique_key` | 六元组 (用户, 维度, 主题, variant, variant_seq) 不得重复；normal 每键最多一行 |
| `status_consistency` | 8 条静态断言（原因、归档时间、topic_new、前缀与状态的对应） |
| `instruction_pattern` | 正文命中指令注入模式 |

审计只读；`flag_violations` 是唯一的标记动作。

### 12.3 管理端

- 当前没有真实认证，`LOCAL_ADMIN` 拥有全部 12 项 capability。
- 配置热更新：下一请求 / 下一轮日批生效；`db_path`、`taxonomy_path`、`embedding.dimensions` 只读。
- 词表校验：维度/主题/priority 唯一；`disabled_topics` 必须属于 topics；topic_overrides 的 key 必须存在；回归样例 quote 非空。
- priority 只影响召回排序；taboo 仍硬钉第一。

---

## 13. 已知实现差异与风险

这些不是“文档没写”，而是当前代码与设计文档/直觉不完全一致，使用或改造时需注意：

1. **衰减窗口未按维度区分**：设计写 context 90 天、其他 180 天；实现是所有非 taboo 统一 90 天降级、180 天归档。
2. **`fix` 不转正**：复核 `fix` 只改文字，不改变状态、不清冲突；需要转正必须用 `confirm`。
3. **管理端编辑绕过状态机**：后台条目编辑只校验状态名、置信范围与主题冲突，不校验 `_LEGAL_TRANSITIONS`；`degraded` 缺原因只是 warning。可能把 `archived` 直接改回 `active`，且日审是静态检查，未必能发现。
4. **quote 约束在应用层**：`sources.quote` 是 `NOT NULL`，但 `entries` 本身没有“必须有证据”的数据库约束；绕过写入器直接写库可产生无证据条目，只能靠审计事后发现。
5. **观察区人工队列不是复核队列**：`audit_pending` 只是审计流水，普通 `/review/queue` 不展示；需要管理端审计视图消费。
6. **归并阈值有两套口径**：装配看“本次可见 taboo 数”，日批看“全量 taboo 数”，同一用户可能先由其中一条触发。
7. **taboo 也可能被预算挤掉**：taboo 排在最前，但仍是逐条计费；单条超长或多个 taboo 总量超过预算/60% 上限时，尾部 taboo 不会渲染。不是“无条件全量注入”。
8. **`deleted` 只是迁移标签**：DB 不存该状态，删除即物理删除。
9. **embedding 默认值**：实例配置 `enabled: true`；设计文档 §20 曾写默认 false，以实际配置为准。

---

## 14. 代码索引

| 主题 | 文件 | 关键符号 |
|---|---|---|
| 接入/幂等 | `src/pismor/pipeline/intake.py` | `intake_event` |
| 归一化 | `src/pismor/pipeline/normalize.py` | `normalize` |
| 触发 | `src/pismor/pipeline/trigger.py` | `should_extract`、`build_explicit_re` |
| 抽取/过滤 | `src/pismor/pipeline/extractor.py` | `extract`、`extract_clarification`、`_to_candidate` |
| 门槛 | `src/pismor/pipeline/gate.py` | `gate` |
| 动作解析 | `src/pismor/pipeline/resolver.py` | `resolve` |
| 写入/状态机 | `src/pismor/pipeline/writer.py` | `apply_actions`、`_add`、`_update`、`_conflict`、`_orphan`、`merge_entries`、`resolve_conflicts` |
| 编排 | `src/pismor/pipeline/runner.py` | `run_pipeline`、`_run`、`_apply`、`_apply_clarification`、`_settle_pair` |
| 澄清 | `src/pismor/pipeline/clarifier.py` | `issue_many`、`match_incoming`、`consume`、`expire` |
| 日批 | `src/pismor/pipeline/aggregator.py` | `run_daily`、`_collect_orphan_plans`、`_apply_orphan_plans`、`_decay`、`_archive`、`_open_merge_tasks` |
| 装配 | `src/pismor/serving/assembler.py` | `assemble`、`_must_have`、`_related`、`_build`、`_clarifications` |
| 复核 | `src/pismor/serving/review.py` | `apply_review_decision` |
| 归并任务 | `src/pismor/serving/merge_task.py` | `DbMergeTaskSink`、`upsert_open`、`resolve` |
| 审计 | `src/pismor/serving/audit.py` | `run_audit`、`flag_violations` |
| 删除 | `src/pismor/serving/memory.py` | `soft_delete_memory`、`restore_memory`、`hard_delete_memory` |
| 条目删除 | `src/pismor/serving/purge.py` | `preview_entry_delete`、`purge_entry` |
| 备份 | `src/pismor/serving/backup.py` | `backup`（`VACUUM INTO` + 滚动保留） |
| API | `src/pismor/serving/api.py` | `/events`、`/memory`、`/review`、`/healthz` |
| 词表 | `src/pismor/core/taxonomy.py` | `load_taxonomy`、`priority_order` |
| 存储 | `src/pismor/core/db.py` | `SCHEMA`、`init_schema`、`get_conn` |
| 配置 | `src/pismor/core/config.py` | `Settings` 及各子配置 |
| 运维 | `scripts/serve.py` | `daily_once`、`_daily_loop` |
| 管理端 | `src/pismor/admin/` | 配置、条目、复核、归并任务路由 |
