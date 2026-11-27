# Web 管理后台手动新增记忆 — 设计文档

日期：2026-11-28　状态：已批准（用户拍板：后台表单填写方案 + LLM 不可用硬拒 + 词表外主题拒绝）

## 意图与动机

管理员在 Web 后台手动补录一条记忆（表单填写），新建即生效、立即参与注入。价值定位：**冷启动 / POC 演示**（低频操作，日常价值低）。合规叙事上，手动条目通过 `origin_type="admin_manual"` 显式标注来源，不冒充 session 证据。

## 目标 / 非目标 / 反目标

- **目标**：管理员填表创建记忆；全部安全规则、写入语义、向量化与自动管线完全一致；创建后 `run_audit` 零违规。
- **非目标**：不做 dry_run 预览仿真（二期：出现“一周补录>5 条 / 补录覆盖正确记忆 / 客户要求可审可撤”再做，用临时 `:memory:` 库跑真实 `writer.apply_actions`，不做 plan_actions 影子实现）；不做批量导入；不做真实认证（沿用现有 LOCAL_ADMIN）。
- **反目标**：手动路径与管线规则漂移（同一规则两处实现）；绕过证据链硬约束（quote 必填）；静默降级（LLM 检查跳过、向量化失败不告知）。

## 方案取舍（已决策）

| 方案 | 结论 |
|---|---|
| **A 后台表单直填**（quote 为用户原话，管理员录入） | **选定**。最简、可控、新建即 active |
| B1 复用 TextEvent 全自动抽取 | 弃：会进 pending_review/orphan 队列，不可控 |
| B2 粘贴素材+人工划句 | 弃：交互重，低频场景不值 |
| E2 后台同步调管线 + dry_run | 推迟二期 |

待拍板项均已定：词表外主题**拒绝**（新建即生效定位）；LLM 不可用**拒绝入库**（503，不静默跳过）；错误码 400/403/409/422/503 分工；`validate-create` 预览不调 LLM。

## 架构：三层，全部与管线共用

```
请求 → L1 安全规则 (pipeline/safety.py，唯一权威)
     → L2 校验原语 (admin/entry_service.py，与编辑路径共用)
     → L3 写入 (writer.apply_actions 原样复用，ADD / CONFLICT 两种 action)
```

### L1：新增 `pipeline/safety.py`（单一权威）

迁入并收敛以下资产（原处改为 import，行为一字节不变）：

- `INSTRUCTION_RE`（原 `llm/prompts.py`）→ `scan_instruction(*texts)`
- `_SENSITIVE_DETAIL_RE` = `\d+号|\d+栋|确诊|处方`（原 extractor）→ `has_sensitive_detail(*texts)`
- `_NEGATION_RE`：`不|别|禁|勿|拒绝|无需|避免|没有`（原 audit）→ `quote_is_negated(quote)`
- `_is_sensitive`（原 extractor）→ `sensitive_cap(taxonomy, dimension, topic)`（敏感 → 0.6）
- `_topics_near`（原 audit）→ `topics_near(a, b)`（剥 `~`/`+` 前缀相等 / 子串 / 共享词根）
- 新增 `normalize_topic(taxonomy, dimension, topic)`：topic_aliases 归一到 canonical 并 strip，canonical 必须仍在 `dim.topics` 内

### L2：校验原语（`admin/entry_service.py`）

`FieldError{field, code, message}` + `CheckResult{ok, errors, warnings, conflicts, normalized_topic}`。

原语：`check_keys`（非空、≤128、无 `_KEY_UNSAFE_CHARS`，防 vec_key `\x1f` 串号）、`check_taxonomy(..., allow_foreign)`（维度存在→别名归一→disabled→词表→前缀 `~+!`→topic≤128；新建硬拒词表外/disabled，编辑只警告）、`check_content`（summary 1..20、body 1..200、quote 1..500、confidence 0..1）、`check_tombstone`、`find_same_key`（查所有状态，返回 existing 摘要）、`find_near_taboo`（`topics_near` 匹配，**排除同 topic**——同 topic 交给 writer 内部恒胜）。

组装：`check_entry_create`（纯本地、无 LLM）；`check_entry_edit` 改用原语，**对外签名与 `list[str]` 返回不变**。

**校验顺序（依赖序，归一先于同键判定）**：结构键字符 → 词表 → 安全（指令→敏感细节→taboo 否定词） → 同键/tombstone → 近似 taboo → 跨维度互斥（LLM strict） → 写入 → 回查。

### L3：写入（复用 writer）

`create_entry` 调 `writer.apply_actions(conn, us, uid, [action], embedder, "admin_manual", origin_ref, now)`，kind 固定 `"explicit"`（避开 inferred_cap 0.8），origin_ref 形如 `admin:<principal.subject>:<ticket或ts>`。

**必须回查 entries**：`apply_actions` 返回入参 action 原样列表（不含真实 status/confidence）；CONFLICT 落库 topic 带 `+` 前缀（`{**c, "topic": f"+{c['topic']}"}`，不改原 dict）。回查 SQL：`topic IN (?, '+'||topic)`。

向量化：与管线同路径（`_precompute_vectors` → `_write_vectors`）。embeddings 表不存在则整体跳过；embedder 失败该条无向量——回查时检测，warnings 提示“记忆已保存但向量化失败，语义召回可能找不到它；可编辑正文触发重新向量化”（编辑路径 `sync_entry_vector` 是现成补救）。

审计：`entry.create`（detail 存键与状态，**不存正文原文**）；拒绝记 `entry.create.rejected`。apply_actions 自行提交，audit 在其后单独提交，失败只记日志不回滚（已知取舍）。

### 冲突决策表

| 检测到 | action | 落库 topic | status | confidence |
|---|---|---|---|---|
| 无冲突 | ADD | `topic` | active | 原值（敏感则 ≤0.6） |
| 同 topic taboo | ADD | `topic` | degraded | ×0.8（writer 内部恒胜） |
| 近似同主题 taboo | CONFLICT(target=taboo) | `+topic` | degraded | ×0.8 |
| 跨维度近邻互斥 | CONFLICT(target=对端 key 元组) | `+topic` | 双方 degraded | ×0.8 |
| **LLM 不可用** | **拒绝（503），零写入** | — | — | — |

冲突一律按 writer 既有语义落库并降级、互标 `conflict_with`（audit 不会报 taboo_no_overlap），接口返回真实 status 知会管理员。

### LLM 不可用：硬性拒绝（已拍板）

`_neighbor_conflict` 开头 `if not dims or not llm or not c.get("quote"): return None`——llm 为 None 会**静默跳过**，对手动创建即漏检。改造 resolver：

- `neighbor_candidates(conn, us, uid, dimension)`：公开函数，抽出原候选查询（对端维度、`status='active'`、topic 无 `~+/!` 前缀、LIMIT 5）；
- `neighbor_conflict(..., strict=False)`：strict 且需要检查（有候选行）但 llm 缺失/抛错 → `raise LlmUnavailable`；管线调用不传 strict，行为不变；
- 豁免范围：identity/fact/context 不依赖 LLM；taboo/preference 但对端无存活行也不调。超时依赖 llm 客户端自身配置，调用处 `except Exception → LlmUnavailable`。

## 接口契约

### `POST /admin/api/entries/validate-create`（memory.write，无 LLM）

响应 `{ok, normalized_topic, errors:[{field,code,message}], warnings, conflicts}`。若判定“需要检查”且 llm 为 None → warnings 提前告知“提交将被拒绝”。

### `POST /admin/api/entries`（memory.write）

请求 `{user_source, user_id, dimension, topic, summary, body, quote, confidence=1.0, origin_ref?}`（无 status 字段）。

| 码 | 场景 |
|---|---|
| 201 | `{ok, id, dimension, topic(实际值，可能带+), status, confidence, warnings, conflicts}` |
| 400 | 业务校验（结构化 `errors[{field,code,message}]`） |
| 403 | 缺 memory.write |
| 409 | 同键已存在（archived 提示“走编辑恢复”）/ 用户已 tombstone |
| 422 | 安全拒绝：指令注入 / 敏感细节 / taboo quote 无否定词 / 写入被拒 |
| 503 | `{ok:false, code:"llm_unavailable", message:"语义互斥检查服务暂时不可用，记忆未保存，请稍后重试"}` |

### `GET /admin/api/entries/topics`（memory.read）

`{dimension: [topic...]}`，排除 disabled_topics（facets 不含 topics，前端必需）。

## 前端

`EntryCreateDialog.vue`（新组件，不塞进编辑抽屉）：来源下拉（facets.sources）+新值标黄；用户ID 补全；维度下拉切换**清空主题**；主题可搜索下拉+自由输入；摘要 20/正文 200/依据 500 字计数；“用正文填充依据”+“依据应为用户原话”提示；高级折叠（confidence 默认 1.0、origin_ref）；`检查`（validate-create）/`保存`（禁用防双击）；成功关抽屉→刷新→高亮新行；409 保留表单+“去编辑这条”；503 保留表单+可重试；422 红色提示；表单注明“保存后立即参与注入”。

`ConflictPanel.vue`：抽出编辑抽屉的冲突表格（id/status/summary）+ el-alert + formatError，两抽屉共用。入口：`EntriesView` 顶部按钮、`UserDetailView` 按钮（预填用户并 disabled）。

## 约束

- 不动 `run_pipeline`、`/events`、`writer` 现有语义、`gate`、audit 规则本体；管线行为零变化。
- 安全规则/正则单一权威 = `pipeline/safety.py`；extractor/audit 不得保留副本。
- 校验顺序固定（见 L2）；归一后 topic 用于同键判定。
- 每条新记忆必须有 quote（硬约束）；新碇固定 `kind="explicit"`。
- Python ≥3.12 / pytest（asyncio auto）/ FastAPI / pydantic v2 / SQLite（sqlite-vec）；web Vue3 + Element Plus + TS，`vue-tsc` 0 错误。

## 留给实施者的决定

`FieldError` 序列化形态、前端补全交互细节、FakeLLM 在 strict 测试中的编排、并发测试的线程编排方式（threading 或双 TestClient）。
