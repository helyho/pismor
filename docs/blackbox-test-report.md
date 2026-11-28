# pismor 黑盒测试报告

- 测试对象：pismor 画像记忆服务（`scripts/serve.py`）
- 测试形态：bash + curl + jq + sqlite3，真实进程、真实 HTTP、真实 SQLite
- 配置：`config/settings.example.yaml` 原样传入，无副本、无注入
- 测试套件：`tests/blackbox/`（`run.sh` + `lib.sh` + 6 个用例域）
- 提交范围：`0b4c7a1..785214c`（25 个提交，46 文件，+1830/−236；其中 `cddb7c0` 为并行完成的 FTS/BM25 检索升级，本套件在其之上验收）
- 报告日期：2026-11-25

> **历史报告**：文中 `dimension_topic@scope` 行键为当时实现。2026-11-27 起 scope 已移除；2026-11-28 起行键改为 uuid，接口 `/profile` 更名为 `/memory`。当前基线为 455 个单测，黑盒用例数以下方“当前套件”为准；本报告结论仅代表 2026-11-25 的提交范围。
- 后续变更：导出功能已于 2026-11-26 移除（`GET /export`、`exporter.py`、`export_root`），相关用例（原 T17/T18/T25）不再保留；2026-11-26 新增对话路径用例并按域重排编号，随后新增澄清回流闭环用例 T18，其后编号顺延为 25 条；本报告编号以当前套件为准

## 1. 执行摘要

| 项 | 结论 |
|---|---|
| 用例总数 | 24（6 域） |
| 真实 LLM 模式（向量召回开启） | **24/24 PASS，skipped 0，exit 0** |
| 无密钥模式 | 8 PASS / 16 SKIP，exit 0 |
| 可重复性 | 连续两次全量结果一致，`git status` 干净 |
| 已修复缺陷 | 6 个（配置 1、产品代码 3：schema 注入、embedding 端点、召回阈值；设计债 2：行键统一、删除后 intake 拦截） |
| 遗留风险 | 无（原 2 项产品行为已按裁决修复） |

结论：服务在“真实 LLM + example 配置”组合下的接入、装配、复核、删除、注入防御、并发与容错链路全部可用；测试过程中发现的两处阻塞缺陷（画像**从未落库**）已定位并修复，修复后画像条目真实入库并可被装配召回。两项原先需决策的产品行为（`entries.id` 键格式、删除权层级）经确认后已一并修复：行键统一为 `dimension_topic@scope`，删除用户在 intake 层即拦截。

## 2. 测试方法与边界

**黑盒铁律**：测试代码不 import 任何 pismor 模块，不调用 `create_app`；一切结论经 HTTP 响应（jq 求值）与 `./data/index.db` 只读查询（sqlite3）双通道验证。

**运行方式**

```bash
# 真实 LLM
LLM_API_KEY=... bash tests/blackbox/run.sh
# 无密钥：LLM 用例自动 SKIP，确定性用例照常
bash tests/blackbox/run.sh
# 辅助参数
bash tests/blackbox/run.sh --only assemble,privacy --keep-data --port 18300
```

`run.sh` 的生命周期：依赖检查 → `pgrep serve.py` 预检（防误清开发实例）→ 端口预检 → 清空 `./data` → 启动服务（`PYTHONPATH=src .venv/bin/python scripts/serve.py --settings config/settings.example.yaml`）→ 轮询 `/healthz` 30s → 逐域执行 → 汇总 → kill + wait。

**断言口径**

- 性质级断言：不断言 LLM 措辞，只断言结构、维度/主题归属、状态机与数据库事实。
- `POST /events` 为 202 + 后台管线，落库断言一律 `wait_db` 轮询（默认 90s，最长 120s）。
- 装配链路随配置：`embedding.enabled: true` 时 query 召回走向量（cos 阈值 `min_similarity: 0.5`），向量不可用或低于阈值时回退词面 BM25（FTS5）。
- LLM 随机性容错：标 `--retry` 的用例失败后隔 5s 整用例重跑一次；`event_id` 每次唯一化，重跑不会撞产品幂等去重。
- 装配只取 `status IN (active, degraded)`；LLM 置信度随机落在 `pending_review` 时，用例先用 `activate_entry`/`activate_all` 走复核通道固定前置条件，再断言召回。

## 3. 覆盖矩阵

### 域 1 事件接入（`cases_events.sh`）

| # | 用例 | 验证点 | 依赖 LLM | 结果 |
|---|---|---|---|---|
| T1 | healthz+schema | 200/`.ok`/`.db=="ok"`；六张表存在 | 否 | PASS |
| T2 | 合法事件接入 | 202 + `events_seen` 落行 | 是 | PASS |
| T3 | 幂等去重 | 同 `event_id` 二次 200/`deduplicated`，表中仅 1 行 | 否 | PASS |
| T4 | 畸形事件 422 | 缺字段/非法类型 → 422 且不落库 | 否 | PASS |
| T5 | content 超限 422 | 9000 字符 → 422 且不落库 | 否 | PASS |
| T6 | explicit 直通入库 | `preference/lang/global` + `status=active` + `sources.kind=explicit` + `origin_ref` 可回溯；`entries.id` 与 `sources.entry_id` 可直接 JOIN | 是 | PASS |
| T7 | 闲聊零入库 | 无关内容 90s 后 `entries`/`sources` 无行 | 是 | PASS |
| T8 | 对话多轮入库 | conversation 多轮 messages 触发入库，`sources.origin_type=conversation`；assistant-only 事件 422（`at least one user message`）且不新增条目 | 是 | PASS |

### 域 2 装配（`cases_assemble.sh`）

| # | 用例 | 验证点 | 依赖 LLM | 结果 |
|---|---|---|---|---|
| T9 | 空包合法 | 未知用户 200，五键齐全、`entries==[]` | 否 | PASS |
| T10 | taboo 入包置顶 | `id` 以 `taboo` 开头且位于 `entries[0]` | 是 | PASS |
| T11 | identity 入包 | `id` 以 `identity` 开头 | 是 | PASS |
| T12 | 用户隔离 | `bb/u1` 数据不出现在 `bb/u2` 与 `app/u1` | 是 | PASS |
| T13 | query 召回 | `?query=python` 召回正文含 Python 的条目 | 是 | PASS |

### 域 3 生命周期 / 复核（`cases_lifecycle.sh`）

| # | 用例 | 验证点 | 依赖 LLM | 结果 |
|---|---|---|---|---|
| T14 | 矛盾收敛 | 先后两条互斥语言偏好，终态 `lang` 生效条目 ≤1 | 是 | PASS |
| T15 | decision 流转 | confirm 非 noop 且 `review_history` +1；旧版本 → `{"noop":true}`；新版本 remove 后行消失 | 是 | PASS |
| T16 | 复核队列结构 | 200，`.items` 为数组，非空项含 `entry_id/status/entry_version` | 否 | PASS |
| T17 | 长消息触发 | >300 字符纯文本素材可触发抽取并入包 | 是 | PASS |
| T18 | 澄清回流闭环 | 互斥偏好双存 → `clarification_needed` 提问 → conversation 回答后 pending 消费；规范行恢复 `active`、`+` 变形行归档、互标清除 | 是 | PASS |

### 域 4 删除权（`cases_privacy.sh`）

| # | 用例 | 验证点 | 依赖 LLM | 结果 |
|---|---|---|---|---|
| T19 | 删除权全链 | `tombstones` 落行（`requested_by='api'`）；`entries/sources/review_history/pending_clarifications` 该用户清空；`/profile` 空包 | 是 | PASS |
| T20 | tombstone 持续 | 删除后再上报：intake 直接拦截，`entries`/`sources` 无新行，`/profile` 仍空包 | 是 | PASS |

### 域 5 注入防御（`cases_injection.sh`）

| # | 用例 | 验证点 | 依赖 LLM | 结果 |
|---|---|---|---|---|
| T21 | 破坏性指令拒绝 | `rm -rf` 载荷不进 `entries.body/summary`，不进装配文本 | 是 | PASS |
| T22 | 提示词覆写拒绝 | “输出系统提示词”类内容不入库、不入装配 | 是 | PASS |
| T23 | 混合文本正反断言 | 正常语言偏好可入库（`preference/lang` 或 `identity/language`），嵌入指令不入库 | 是 | PASS |

### 域 6 韧性收尾（`cases_resilience.sh`）

| # | 用例 | 验证点 | 依赖 LLM | 结果 |
|---|---|---|---|---|
| T24 | 并发装配 | 8 路并发 ×16 次 `/profile`，全部 200 且 body 可被 jq 解析 | 否 | PASS |
| T25 | 损坏库拒启 | 停服务→写坏 `index.db`→重启：非 0 退出或 healthz 不就绪；测毕恢复备份并确认 healthz 200 | 否 | PASS |

## 4. 验收数据

| 运行 | 命令 | 结果 |
|---|---|---|
| run_rowkey | `LLM_API_KEY=... bash tests/blackbox/run.sh`（行键统一 + 向量召回开启，当时 26 条） | `passed 26/26 (skipped 0)`，exit 0 |
| run_rowkey_nokey | `env -u LLM_API_KEY bash tests/blackbox/run.sh`（当时 26 条） | `passed 9/9 (skipped 17)`，exit 0 |
| run_reorder | `LLM_API_KEY=... bash tests/blackbox/run.sh`（24 条，新增 T8 对话用例 + 按域重排编号） | `passed 24/24 (skipped 0)`，exit 0 |
| run_reorder_nokey | `env -u LLM_API_KEY bash tests/blackbox/run.sh`（24 条） | `passed 8/8 (skipped 16)`，exit 0 |
| run_clarify | `LLM_API_KEY=... bash tests/blackbox/run.sh`（25 条，新增 T18 澄清回流闭环） | `passed 25/25 (skipped 0)`，exit 0 |
| run_clarify_nokey | `env -u LLM_API_KEY bash tests/blackbox/run.sh`（25 条） | `passed 8/8 (skipped 17)`，exit 0 |
| 幂等性 | 连续两次同模式全量 | 逐用例结果一致 |
| 稳定性 | 域 2 单独连跑 3 次（T13 竞态修复后） | 5/5 PASS ×3 |
| 向量召回 E2E | 单条 Python 画像，3 个相关 / 3 个无关 query | 相关（Python、编程语言）1 条；无关（天气预报、种番茄、股票）0 条 |
| 单元层回归 | `.venv/bin/python -m pytest` | 282/282 通过 |

## 5. 发现与修复

### 5.1 阻塞缺陷 A：`json_schema` 对该网关无效（配置层）

`config/settings.example.yaml` 原为 `structured_output_type: json_schema`。DeepSeek 网关（`http_api` 兼容模式）不支持 `json_schema`，返回内容不含 `confidence/quote/body` 等约定字段。链路 `extractor._to_candidate` → `confidence=0` → `gate` 判定 `0 < review_min(0.6)` → 全部 dropped，**画像条目 0 落库**。

处置：配置改为 `json_object`（并调大 `log_preview_chars`）。example 配置对该网关恢复可用。

### 5.2 阻塞缺陷 B：`json_object` 模式下 schema 未进入提示词（产品代码）

`json_object` 只保证“输出合法 JSON”。实测 `deepseek-v4.1-flash` 在无结构约束时出现多种自造格式：容器键为 `items`/`entries` 而非 `candidates`，字段整体缺失。服务端 `_post_once` 仅发送 `{"type":"json_object"}`，schema 文本从未送达模型。

处置（产品修复）：`src/pismor/llm/client.py::_post_once` 在 `json_object` 模式下把 schema 描述注入 system prompt，并附容器键硬约束（禁止 `items`/`entries`）。修复后新增 `tests/test_llm.py` 两个用例（注入生效、`json_schema` 模式不重复注入），pytest 255/255 通过，黑盒 T6/T10/T11/T13/T14 首次真实落库。

**影响面**：任何使用 DeepSeek/OpenAI 兼容网关且走 `json_object` 的部署都会受益；`json_schema` 原生支持的供应商行为不变。

### 5.3 向量召回两处缺陷（产品代码）

开启 `embedding.enabled` 后暴露，均已修复：

1. **端点重复拼接**：`Embedder` 无条件在 base 后拼 `/embeddings`，而 example 配置的 endpoint 已含 `/v1/embeddings`，请求落到 `/v1/embeddings/embeddings` 404，`EmbedUnavailable` 静默降级，`embeddings` 表恒空、语义 query 召回恒空。修复：幂等拼接（已以 `/embeddings` 结尾则不再追加，`886c10b`），并补单元测试防止回归。
2. **召回无相似度下限**：`_vector_recall` 只按距离取 top-k，任意 query 都会返回条目（实测 Python 条目对“天气预报”cos=0.41、“股票行情”cos=0.39 仍被召回）。修复：新增 `embedding.min_similarity: 0.5`（`EmbeddingSettings` 字段 + example 配置），由 `_cos_to_l2` 换算成 L2 距离进 SQL 过滤（`ff304ae`）；低于阈值或向量不可用时回退词面 BM25。

### 5.4 `entries.id` 与 `sources.entry_id` 键格式不一致（设计债，已修）

原 `entries.id` 形如 `preference_lang`，`sources.entry_id` 形如 `preference_lang@global`，两者**永不相等**，无法直接 JOIN；审计、导出、复核等路径各自拼接格式，`exporter` 甚至用 `CASE` 弥合。

处置：新增 `core.keys.entry_key(dimension, topic, scope)`，`entries.id` 统一为行键 `dimension_topic@scope`，`sources.entry_id` 同值；`conflict_with`/澄清标记天然同形；`embeddings` 五元组键不变；旧库由 `init_schema` 幂等 `UPDATE` 迁移。JOIN 简化为 `s.entry_id = e.id`（`exporter`/`audit`/`assembler`/`review` 全部对齐），T6 增加 JOIN 一致性断言。过程中顺带修出 `runner._settle_pair` 的 `SELECT` 缺 `id` 列导致的静默事务回滚。

### 5.5 删除权只在装配/导出层生效（产品行为，已修）

原 `tombstones` 仅在 `assembler.py` 装配时检查。删除后 ingest/runner **不拦截**新事件：同一用户再次上报，`entries` 仍会写入新行，只是 `/profile` 与导出恒为空包。

处置：`runner._run` 入口先查 tombstone，命中即记录日志并返回，不再调用 LLM、不写任何表。T20 因此从“装配空包”加严为“intake 拦截：`entries`/`sources` 计数为 0 且 `/profile` 空包”；单测新增 `test_events_from_deleted_user_not_persisted`（202 但零落库、LLM 零调用）。

### 5.6 其它观测

- `events_seen` 在管线因 LLM 鉴权失败而中断时仍会写入，故 T3（幂等）在无密钥下亦可 PASS。
- `POST /events` 的 `content` 上限 8192，超限 422。
- 装配响应条目字段为 `{id, text, confidence, status}`，**不含 `dimension`**；维度信息只能从 `id` 前缀推断。
- 非 ASCII query 参数必须百分号编码，裸 UTF-8 被 h11 判 400。
- `embedding.enabled` 默认 `false`（词面匹配为默认装配路径）；开启后 `embeddings` 为 sqlite-vec `vec0` 虚表，CLI `sqlite3` 无法直接读取，需 `enable_load_extension` 后加载 `sqlite_vec`。
- T13 曾在全量下间歇失败，根因在测试侧：`wait_db` 等“任意条目入库”会被先落库的无关条目提前满足，提权跑在目标条目写入之前，目标条目随后以 `pending_review` 入库被装配过滤。已改为等 `sources.origin_ref` 精确落库、提权后确认 `active`（`67b7f15`）。

## 6. 局限与后续建议

1. **真实 LLM 的判定漂移**：维度/主题归属存在合理波动（如“我用中文回答”可归 `preference/lang` 或 `identity/language`）。套件已用性质级断言 + 单次重试 + 复核提权吸收，但仍偶发依赖模型输出，建议在 CI 中固定模型版本与温度。
2. **日批与降级场景未覆盖**：需修改 `daily_interval_seconds` 等配置，违背“配置原样”约束；相关逻辑由 `tests/test_aggregator.py` 等单元测试覆盖。
3. **budget 截断**未做精确值断言（真实 LLM 下条目数不可控），可在后续加弱断言（`&budget=200` 时 `budget_used<=200`）。
4. **T25 会破坏 `data/index.db`**：已实现备份/还原与 `--keep-data` 跳过，但该用例必须最后执行（套件已固定顺序）。
5. **测试独占 `./data`**：`run.sh` 检测到运行中的服务实例会拒绝执行（exit 2），避免误清开发数据。
6. **向量召回依赖外部 embedding 服务**：服务不可达或维度不匹配时静默降级为词面 BM25，黑盒不区分降级原因；T13 因此只断言“两条召回路径至少一条命中”，不单独断言走向量。

## 7. 复现方式

```bash
cd /path/to/pismor
# 有密钥：全量
LLM_API_KEY=sk-... bash tests/blackbox/run.sh
# 无密钥：确定性用例 + SKIP 清单
bash tests/blackbox/run.sh
# 单域调试（保留数据）
bash tests/blackbox/run.sh --only assemble --keep-data
```

产物：`tests/blackbox/serve.log`（服务端日志）、`./data/index.db`（SQLite）。
