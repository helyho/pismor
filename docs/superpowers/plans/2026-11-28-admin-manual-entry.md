# Web 管理后台手动新增记忆 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 管理员在 Web 后台表单手动补录记忆，新建即 active 立即参与注入，安全规则/写入语义/向量化与自动管线完全一致。

**Architecture:** 三层——L1 新增 `pipeline/safety.py` 收敛全部安全正则与规则（extractor/audit 改 import，行为不变）；L2 `admin/entry_service.py` 拆校验原语（编辑路径复用，签名不变）并组装 `check_entry_create`/`create_entry`；L3 写入原样复用 `writer.apply_actions`（ADD/CONFLICT），回查 entries 取真实结果。LLM 跨维度互斥检查 strict 模式：需要检查但 LLM 不可用 → 503 拒绝、零写入。

**Tech Stack:** Python ≥3.12、FastAPI、pydantic v2、SQLite（sqlite-vec）、pytest（asyncio auto）；前端 Vue3 + Element Plus + TypeScript（vue-tsc）。

**Spec:** `docs/superpowers/specs/2026-11-28-admin-manual-entry-design.md`

## Global Constraints

- 不改 `run_pipeline`、`/events`、`writer` 现有语义、`gate`、audit 规则本体；管线行为零变化（现有测试全绿是每个里程碑的验收线）。
- 安全规则/正则单一权威 = `pipeline/safety.py`；extractor/audit/llm.prompts 不得保留副本定义。
- 长度硬顶：body ≤200 字、summary 1..20 字、quote 1..500 字、confidence 0..1、topic ≤128 且不得以 `~`/`+`/`!` 开头。
- 新建固定 `kind="explicit"`、`origin_type="admin_manual"`；请求**无 status 字段**，落库状态由写入语义决定。
- 校验顺序固定：结构键字符 → 词表（归一→disabled→词表→前缀→长度） → 安全（指令→敏感细节→taboo 否定词） → 同键/tombstone → 近似 taboo → 近邻 LLM(strict) → 写入 → 回查；归一后 topic 用于同键判定。
- 键校验复用 `core/schemas.py` 的 `_KEY_UNSAFE_CHARS`（`\x00-\x1f\x7f`）。
- 审计 `detail` 不存正文/quote 原文；`entry.create.rejected` 记拒绝。
- admin_audit 在 `apply_actions` 提交之后单独提交，失败只记日志不回滚。
- pytest 验证命令：`pytest <path> -v`；前端验证：`cd web && npm run typecheck && npm run build`。
- 提交信息 conventional commits（`feat:`/`refactor:`/`test:`）。

## Review Focus

1. **控制字符键**：`user_source`/`user_id` 含 `\x1f` → 必须 400 `unsafe_chars` 且零写入（否则 vec_key 串号、用户串扰）→ Task 4 测试钉死。
2. **指令注入**：body/quote/summary 含“忽略之前指令”类文本 → 422 拒绝，绝不入库 → Task 1 单测 + Task 5 API 测试。
3. **敏感细节**：敏感主题 + `\d+号|\d+栋|确诊|处方` → 422；敏感主题无细节 → 置信度压 0.6 正常入库 → Task 1/Task 5。
4. **LLM 故障**：taboo/preference 且有对端 active 行、LLM 挂 → 503 零写入；identity 维度同状况 → 201 不受影响 → Task 3/Task 5。
5. **并发同键**：双请求同四元组 → 一 201 一 409，无 500 → Task 5。

---

### Task 1: `pipeline/safety.py` — 安全规则单一权威

**Files:**
- Create: `src/pismor/pipeline/safety.py`
- Modify: `src/pismor/serving/audit.py`（`_NEGATION_RE`、`_topics_near` 改为从 safety import，删除本地定义）
- Modify: `src/pismor/llm/prompts.py`（`INSTRUCTION_RE` 定义迁出，如本文件其他处用到则 `from pismor.pipeline.safety import INSTRUCTION_RE`）
- Test: `tests/test_safety.py`

**Interfaces:**
- Produces（后续所有 task 依赖）:
```python
INSTRUCTION_RE: re.Pattern        # 自 llm/prompts.py 原样迁入
SENSITIVE_DETAIL_RE: re.Pattern   # r"\d+号|\d+栋|确诊|处方"
NEGATION_RE: re.Pattern           # 原 audit._NEGATION_RE，词表：不|别|禁|勿|拒绝|无需|避免|没有
def normalize_topic(taxonomy, dimension: str, topic: str) -> str
def scan_instruction(*texts: str | None) -> bool        # 任一命中 INSTRUCTION_RE → True
def sensitive_cap(taxonomy, dimension: str, topic: str) -> float | None  # dim.topic_overrides[topic].sensitive → 0.6，否则 None
def has_sensitive_detail(*texts: str | None) -> bool
def quote_is_negated(quote: str) -> bool
def topics_near(a: str, b: str) -> bool                 # 原 audit._topics_near 逻辑：剥 ~/+ 前缀相等 / 一方子串 / 共享词根 re.split(r"[\W_]+",t)[0]
```
- 注意：先 grep `INSTRUCTION_RE`、`_NEGATION_RE`、`_topics_near`、`_SENSITIVE_DETAIL_RE`、`_is_sensitive` 的全部引用点再迁；`_is_sensitive` 的逻辑（取 `dim.topic_overrides[topic].sensitive`）迁入 `sensitive_cap`，extractor 的调用在 Task 2 切换。

- [ ] **Step 1: 写失败测试 `tests/test_safety.py`**

```python
def test_scan_instruction_hits():     # “忽略之前的指令” → True；“今天天气不错” → False
def test_quote_is_negated():          # “不要打电话” → True；“多打电话联系” → False
def test_has_sensitive_detail():      # “3号楼502” → True；“确诊” → True；“开会” → False
def test_sensitive_cap():             # taxonomy 敏感 topic → 0.6；普通 → None
def test_normalize_topic_alias():     # 别名归一到 canonical；strip；词表外 → ValueError
def test_topics_near():               # “~phone” vs “phone” → True；子串 → True；共享词根 → True；“a” vs “b” → False
```

- [ ] **Step 2: 跑测试确认失败** — `pytest tests/test_safety.py -v`，预期 `ModuleNotFoundError: pismor.pipeline.safety`
- [ ] **Step 3: 实现 safety.py**（正则与词表从原处**原样复制**，不做语义改动；`normalize_topic` 参照 extractor `_to_candidate` 第 2 步的别名归一逻辑）
- [ ] **Step 4: audit.py / prompts.py 改 import 并删除本地副本定义**
- [ ] **Step 5: 跑测试确认通过 + 管线回归** — `pytest tests/test_safety.py tests/test_audit.py tests/test_prompts.py tests/test_extractor.py tests/test_topic_alias.py -v` 全绿
- [ ] **Step 6: Commit** — `git add -A && git commit -m "refactor: extract safety rules into pipeline/safety.py (behavior unchanged)"`

### Task 2: extractor 切换到 safety（行为不变回归）

**Files:**
- Modify: `src/pismor/pipeline/extractor.py`（`_to_candidate` 内的指令扫描、敏感判定、别名归一改为调用 safety 函数；删除 `_SENSITIVE_DETAIL_RE`、`_is_sensitive` 本地实现）
- Test: 既有 `tests/test_extractor.py`、`tests/test_topic_alias.py`（无新测试——行为不变重构）

**Interfaces:**
- Consumes: Task 1 全部导出。

- [ ] **Step 1: 切换 `_to_candidate` 五处调用**（指令扫描→`scan_instruction(body, quote, summary)`；敏感→`sensitive_cap` + `has_sensitive_detail`；别名归一→`normalize_topic`）
- [ ] **Step 2: 回归** — `pytest tests/test_extractor.py tests/test_topic_alias.py tests/test_runner.py tests/test_e2e.py -v` 全绿（这是“行为一字节不变”的验收）
- [ ] **Step 3: 确认 extractor 无残留正则副本** — `grep -n "INSTRUCTION_RE\|_SENSITIVE_DETAIL_RE\|_is_sensitive" src/pismor/pipeline/extractor.py` 仅剩 import 行
- [ ] **Step 4: Commit** — `git commit -am "refactor: extractor uses pipeline/safety.py"`

### Task 3: resolver — `neighbor_candidates` + `strict` 模式

**Files:**
- Modify: `src/pismor/pipeline/resolver.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Produces:
```python
class LlmUnavailable(RuntimeError): ...
def neighbor_candidates(conn, user_source: str, user_id: str, dimension: str) -> list[dict]
    # 原 _neighbor_conflict 内候选查询抽出：_NEIGHBOR_PAIRS 对端维度、status='active'、
    # topic NOT LIKE '~%' AND NOT LIKE '+%' AND NOT LIKE '!%'、LIMIT _NEIGHBOR_LIMIT(5)
def neighbor_conflict(conn, user_source, user_id, candidate, llm, *, strict: bool = False) -> tuple[str, str] | None
    # 候选行为空 → None（不调 LLM）；候选行非空且 llm is None：
    #   strict=False → None（现状）；strict=True → raise LlmUnavailable
    # LLM 调用抛任何异常：strict=False 维持现状语义；strict=True → raise LlmUnavailable
```
- 注意：`_neighbor_conflict` 现有短路 `if not dims or not llm or not c.get("quote"): return None` 中 `not llm` 的静默跳过是管线容错——**管线调用点（runner/resolver 内部）不传 strict，行为不变**；strict 只由 Task 5 的 create_entry 使用。

- [ ] **Step 1: 写失败测试**

```python
def test_neighbor_candidates_queries_peer_dim(conn, taxonomy): ...   # 造 taboo active 行，查 preference → 命中；archived/带前缀 → 不命中
def test_strict_raises_when_llm_missing(conn): ...                   # 有候选行 + llm=None + strict=True → LlmUnavailable
def test_non_strict_returns_none_when_llm_missing(conn): ...         # 现状不变
def test_strict_raises_on_llm_error(conn): ...                       # 抛错 fake LLM + strict → LlmUnavailable
def test_no_candidates_skips_llm_even_strict(conn): ...              # 无候选行 → None，LLM 零调用
```

- [ ] **Step 2: 跑失败** — `pytest tests/test_resolver.py -v` → `LlmUnavailable`/`neighbor_candidates` 未定义
- [ ] **Step 3: 实现**（候选查询从 `_neighbor_conflict` 原样抽出；`_neighbor_conflict` 改为内部调 `neighbor_candidates`）
- [ ] **Step 4: 跑过 + 管线回归** — `pytest tests/test_resolver.py tests/test_runner.py tests/test_e2e.py -v` 全绿
- [ ] **Step 5: Commit** — `git commit -am "feat: resolver neighbor_candidates + strict LlmUnavailable mode"`

### Task 4: entry_service 校验原语 + `check_entry_create`

**Files:**
- Modify: `src/pismor/admin/entry_service.py`（追加原语与 `check_entry_create`；`check_entry_edit` 改为组装原语，**签名与 `list[str]` 返回不变**）
- Test: `tests/admin/test_entry_create_checks.py`（新）+ 既有 `tests/admin/test_entry_edit.py`（回归，不改）

**Interfaces:**
- Consumes: Task 1 safety 全部导出；`core/schemas.py._KEY_UNSAFE_CHARS`；`serving/audit.py` 的 tombstone 查询模式。
- Produces:
```python
@dataclass
class FieldError: field: str; code: str; message: str

@dataclass
class CheckResult:
    ok: bool
    errors: list[FieldError]
    warnings: list[str]
    conflicts: list[dict]           # {id, dimension, topic, status, summary}
    normalized_topic: str

def check_keys(user_source: str, user_id: str) -> list[FieldError]
    # 空/超128/含 _KEY_UNSAFE_CHARS → code="unsafe_chars"/"missing"/"too_long"
def check_taxonomy(taxonomy, dimension: str, topic: str, *, allow_foreign: bool) -> tuple[str, list[FieldError], list[str]]
    # 顺序：维度存在→normalize_topic→disabled_topics→dim.topics→前缀 ~+!→topic≤128
    # allow_foreign=False：词表外/disabled/前缀 → error；True → 仅 warning（编辑兼容）
    # 返回 (normalized_topic, errors, warnings)
def check_content(summary, body, quote, confidence) -> list[FieldError]
    # summary strip 后 1..20；body strip 后 1..200；quote strip 后 1..500；confidence 0..1
def check_safety(taxonomy, dimension, topic, summary, body, quote) -> tuple[list[FieldError], list[str], float]
    # scan_instruction(body,quote,summary) → error code="instruction_detected" (field="body")
    # has_sensitive_detail(body,quote) → error code="sensitive_detail"
    # dimension=="taboo" 且 not quote_is_negated(quote) → error code="quote_not_negated"
    # 敏感 → warnings 提示压顶；返回 (errors, warnings, cap)  cap=sensitive_cap(...) or 1.0
def check_tombstone(conn, user_source, user_id) -> list[FieldError]   # code="user_deleted"
def find_same_key(conn, user_source, user_id, dimension, topic) -> dict | None
    # 查所有状态 → {id, status, summary, confidence, updated_at}；无 → None
def find_near_taboo(conn, user_source, user_id, dimension, topic) -> dict | None
    # taboo 存活行（active/degraded、topic NOT LIKE '~%'），safety.topics_near 匹配，排除 topic 相同，取第一条
def check_entry_create(conn, taxonomy, *, user_source, user_id, dimension, topic, summary, body, quote, confidence) -> CheckResult
    # 按 Global Constraints 校验顺序组装；conflicts 暂只含 find_same_key 结果
```
- 注意：`check_entry_create` 不调 LLM、不查近似 taboo/近邻（那些在 create_entry 的写入前段做）；`check_entry_edit` 重构后既有测试零改动全绿是硬验收。

- [ ] **Step 1: 写失败测试**（每个原语 2-3 断言 + `check_entry_create` 组装序：`\x1f` 键、超长、词表外硬拒、disabled 硬拒、`~topic` 前缀拒、指令 422 类 code、敏感细节、taboo 无否定词、同键 conflicts、归一后同键判定）
- [ ] **Step 2: 跑失败** — `pytest tests/admin/test_entry_create_checks.py -v`
- [ ] **Step 3: 实现原语 + `check_entry_create`；重构 `check_entry_edit` 为组装**
- [ ] **Step 4: 新测试过 + 编辑回归** — `pytest tests/admin/test_entry_create_checks.py tests/admin/test_entry_edit.py -v` 全绿
- [ ] **Step 5: Commit** — `git commit -am "feat: entry_service validation primitives + check_entry_create"`

### Task 5: writer 向量探测 + `create_entry` + 三个路由

**Files:**
- Modify: `src/pismor/core/writer.py`（或 writer 实际所在路径，`grep -n "_has_embeddings_table" src/pismor/` 定位）追加：
```python
def entry_has_vector(conn, entry_key: str) -> bool   # 表结构细节封在 writer 内
```
- Modify: `src/pismor/admin/entry_service.py` 追加 `create_entry` 与三个异常
- Modify: `src/pismor/admin/schemas.py`（`EntryCreateIn`/`EntryCreateOut`/`EntryValidateOut`）
- Modify: `src/pismor/admin/routers/entries.py`（三个端点）
- Test: `tests/admin/test_entry_create_api.py`（新）

**Interfaces:**
- Consumes: Task 1-4 全部；`writer.apply_actions`、`writer.entry_has_vector`；resolver strict；deps 现有 `get_principal`/`require(*caps)` 模式。
- Produces:
```python
class EntryExistsError(Exception): existing: dict        # → 409
class UserDeletedError(Exception): ...                   # → 409
class SafetyRejectedError(Exception): errors: list[FieldError]  # → 422

def create_entry(conn, taxonomy, *, user_source, user_id, dimension, topic,
                 summary, body, quote, confidence, origin_ref, embedder, llm, now) -> dict
# 返回 {ok, id, dimension, topic(实际值，可能带+), status, confidence, warnings, conflicts}
# 流程：check_entry_create → SafetyRejected(errors 中 code ∈ {instruction_detected,
#   sensitive_detail, quote_not_negated})；find_same_key 命中 → EntryExistsError（archived
#   时 message 注明“走编辑恢复”）；find_near_taboo(dim≠taboo)；候选行存在时
#   neighbor_conflict(strict=True) → LlmUnavailable 上抛；
#   action = CONFLICT(neighbor) | CONFLICT(("taboo", near["topic"])) | ADD
#   candidate.confidence = min(confidence, safety.sensitive_cap(taxonomy, dim, normalized_topic) or 1.0)
#   apply_actions(conn, us, uid, [action], embedder, "admin_manual", origin_ref, now)
#   applied 为空 → SafetyRejectedError([FieldError("body","write_rejected",...)])
#   fetch_final: SELECT id, dimension, topic, status, confidence FROM entries
#     WHERE user_source=? AND user_id=? AND dimension=? AND topic IN (?, '+topic')
#   entry_has_vector(conn, id) 为 False 且向量表启用 → warnings.append("记忆已保存但向量化失败，语义召回可能找不到它；可编辑正文触发重新向量化")
#   record_admin_audit(conn, principal, "entry.create", id, {键+status+confidence+origin_type+origin_ref})
#   异常路径 entry.create.rejected（reason）同样审计
```
- 路由（`admin/routers/entries.py`，prefix=`/admin/api/entries`）：
  - `POST /validate-create`（`require("memory.write")`）→ `EntryValidateOut`；llm 为 None 且候选行存在 → warnings 预警“提交时需要语义互斥检查，该服务当前不可用，提交将被拒绝”
  - `POST ""`（`require("memory.write")`）→ 201 `EntryCreateOut`；映射：FieldError → 400、EntryExists/UserDeleted → 409、SafetyRejected → 422、`LlmUnavailable` → 503 `{"ok":false,"code":"llm_unavailable","message":"语义互斥检查服务暂时不可用，记忆未保存，请稍后重试"}`
  - `GET /topics`（`require("memory.read")`）→ `{dimension: [topic...]}` 排除 disabled
  - origin_ref 缺省 `f"admin:{principal.subject}:{int(now.timestamp())}"`；embedder/llm 从 create_app 注入处取（对照 `serving/api.py` 注入方式与 deps 既有模式）

- [ ] **Step 1: 写失败 API 测试**（复用 `tests/admin/test_entry_edit.py` 的 `_make_app`/`FakeLLM`/`FakeEmbedder` 模式；场景清单，每场景一个测试：）
  - 201 正常（entries+sources 各一行、`run_audit` 对该用户零 violation、audit 有 entry.create）
  - 201 + 向量化：`init_schema(vec_dimensions=4)` + FakeEmbedder → `entry_has_vector` True；FakeEmbedder 抛错 → 201 + warning 含“向量化失败”
  - 201 taboo 恒胜：先造同 topic taboo active，创建 preference → status="degraded"、confidence=×0.8、warnings 说明
  - 201 近似 taboo 转 CONFLICT：taboo 行 topic="phone"，创建 "contact_phone" → 落库 topic="+contact_phone"（接口返回该实际值）、双方 conflict_with 互标
  - 201 跨维度近邻：ScriptedLLM/FakeLLM 编排 RESOLVE 判 CONFLICT → target 对端、双方 degraded
  - 503：taboo/preference + 有候选行 + llm=None（create_app(llm=None)）→ 503、entries/sources/embeddings **零新增**、audit 有 `entry.create.rejected`
  - 503 不波及：identity 维度 + llm=None → 201
  - 409 同键（active 与 archived 两种，后者 message 含“编辑”）；409 tombstone
  - 422×3：指令注入 / 敏感细节 / taboo 无否定词；422 write_rejected（FakeEmbedder 正常但 apply_actions 返回空：quote 为空字符串触发 `_insert_entry` None）
  - 400 字段级（超长/空 summary/`\x1f` 键/confidence 越界）
  - 403 无 memory.write（构造缺 capability 的 principal）
  - validate-create：与创建同 payload → ok/normalized_topic/errors/conflicts；llm=None 且 taboo 场景 → 预警 warning；**不产生任何写入**
  - GET /topics → 排除 disabled_topics
  - 并发：两个线程同时 POST 同四元组 → 恰好一 201 一 409
- [ ] **Step 2: 跑失败** — `pytest tests/admin/test_entry_create_api.py -v`
- [ ] **Step 3: 实现 `entry_has_vector`、`create_entry`、schemas、三端点**
- [ ] **Step 4: 全量回归** — `pytest tests/admin/ -v` + `pytest tests/ -v` 全绿
- [ ] **Step 5: Commit** — `git commit -am "feat: admin API create memory entry (POST /entries, validate-create, topics)"`

### Task 6: 前端类型与 API client

**Files:**
- Modify: `web/src/api/types.ts`
- Modify: `web/src/api/client.ts`（若方法放别处，对照 `EntryEditDialog.vue` 现有调用方式）

**Interfaces:**
- Produces:
```ts
interface EntryCreatePayload { userSource; userId; dimension; topic; summary; body; quote; confidence?; originRef? }
interface EntryCreateResult  { ok; id; dimension; topic; status; confidence; warnings: string[]; conflicts: ConflictRow[] }
interface EntryValidateResult { ok; normalizedTopic; errors: FieldError[]; warnings: string[]; conflicts: ConflictRow[] }
type TopicMap = Record<string, string[]>
// ConflictRow 复用 types.ts 现有冲突行类型（如无则新增，字段 id/dimension/topic/status/summary）
// client: validateCreateEntry(payload): Promise<EntryValidateResult>
//         createEntry(payload): Promise<EntryCreateResult>
//         listEntryTopics(): Promise<TopicMap>
// 错误沿用现有 ApiError（detail 携带后端 errors/conflicts/code）
```
- [ ] **Step 1: 加类型与方法**（409/503 时 ApiError detail 含 `existing`/`code`，类型里体现）
- [ ] **Step 2: 验证** — `cd web && npm run typecheck` 0 错误
- [ ] **Step 3: Commit** — `git commit -am "feat(web): entry create api types & client methods"`

### Task 7: `ConflictPanel.vue` 抽取

**Files:**
- Create: `web/src/components/ConflictPanel.vue`
- Modify: `web/src/components/EntryEditDialog.vue`（改用 ConflictPanel，删除内联冲突表格/el-alert/formatError）

**Interfaces:**
- Produces: `<ConflictPanel :errors="..." :warnings="..." :conflicts="..."/>`，props 接受后端原样结构（errors 可为 `string[]` 或 `{field,code,message}[]`，内部归一渲染）；`formatError` 从组件内导出或移入 api/utils，两处共用。

- [ ] **Step 1: 创建 ConflictPanel**（迁移 EntryEditDialog 现有 conflict-box 三列表格 + el-alert + formatError，样式不变）
- [ ] **Step 2: EntryEditDialog 接入**（视觉与行为零变化）
- [ ] **Step 3: 验证** — `cd web && npm run typecheck && npm run build` 0 错误
- [ ] **Step 4: Commit** — `git commit -am "refactor(web): extract ConflictPanel shared component"`

### Task 8: `EntryCreateDialog.vue` + 两个入口

**Files:**
- Create: `web/src/components/EntryCreateDialog.vue`
- Modify: `web/src/views/EntriesView.vue`（顶部“新建记忆”按钮）
- Modify: `web/src/views/UserDetailView.vue`（按钮，预填 userSource/userId 并 disabled）

**Interfaces:**
- Consumes: Task 6 client 三方法、Task 7 ConflictPanel。
- 行为规格（对照 spec“前端”节）：维度切换清空 topic；20/200/500 字计数；“用正文填充依据”；高级折叠（confidence 默认 1.0、origin_ref）；保存防双击；成功→关抽屉+刷新列表+高亮新行；409 保留表单+existing 摘要+“去编辑这条”跳转；503 保留表单+可重试提示（`code === "llm_unavailable"`）；422 红色提示；表单注明“保存后立即参与注入”“依据应为用户原话”。

- [ ] **Step 1: 实现 EntryCreateDialog**（结构对照 `EntryEditDialog.vue` 的 drawer+字段布局）
- [ ] **Step 2: 两个入口接入**（EntriesView 成功后调用现有刷新逻辑；UserDetailView 预填）
- [ ] **Step 3: 验证** — `cd web && npm run typecheck && npm run build` 0 错误
- [ ] **Step 4: 手动验收** — 起 dev server 走一遍：创建普通条目→列表高亮；同键→409 提示+跳编辑；停掉 LLM 依赖造 taboo 场景→503 提示保留表单
- [ ] **Step 5: Commit** — `git commit -am "feat(web): manual memory entry creation dialog with entries/user entries"`

---

## 里程碑与验收线

| 里程碑 | Task | 验收 |
|---|---|---|
| M1 安全规则抽取（无新功能） | 1-2 | `pytest tests/ -v` 全绿；extractor/audit 无正则副本 |
| M2 后端功能 | 3-5 | `pytest tests/admin/test_entry_create_api.py` 全场景过；全量回归绿；run_audit 零违规 |
| M3 前端 | 6-8 | `npm run typecheck && npm run build` 0 错误；手动验收清单过 |
