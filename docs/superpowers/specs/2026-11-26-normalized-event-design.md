# 事件规范化层（B 层）设计

- 日期：2026-11-26
- 状态：待批准
- 规模：small change（5 个源码文件 + 1 个新测试文件，不改外部契约）
- 决策来源：会话中选择「方案 1：独立 normalize 模块」

## 1. 背景与意图

`conversation` / `text` 两种事件的差异，目前被 `event_type` 隐式承载在四个位置：

1. `extractor.extract()` 选 prompt 模板（`EXTRACT_CONVERSATION` vs `EXTRACT_TEXT`）；
2. `trigger.should_extract()` 只对 conversation 生效，且只遍历 user 消息；
3. `runner._run()` 只对 conversation 先做澄清回流匹配；
4. `runner._reinforce()` 只对 conversation 取最后一条 user 消息做词面强化。

四种差异本身是真实的（证据归属、触发策略、澄清参与、强化信号），不能取消。问题是它们都通过读原始 `event` dict 里的 `event_type` 和 `messages` 分散判断，下游任意函数都能再读一次。

B 层只做一件事：**在事件进入管线后立即计算出一个管线视图 `NormalizedEvent`，下游只读这个视图，不再读 `event_type` 或 `messages`。** 外部契约、DB、prompt 规则、触发语义全部不变。

## 2. 目标 / 非目标

**目标**

- `trigger`、`extractor`、`runner` 的语义判断收敛到 `mode`（`dialogue` / `document`）与 `user_texts`；
- `event_type` 在管线内只出现于 `normalize()` 内部与 `origin_type` 落库处；
- 把"用户域文本"从"整段 prompt 文本"里显式拆开，避免下游各自重新解析 `messages`。

**非目标**

- 不改 `POST /events` 的外部契约，不动 `ConversationEvent` / `TextEvent`；
- 不合并 `EXTRACT_CONVERSATION` 与 `EXTRACT_TEXT`（"仅 user 为证据" vs "整段属于用户本人"是两条真实规则）；
- 不改 DB schema、导出、审计、`canonical_hash`；
- 不改 intake 的长度校验（8192 仍只作用于 `content`）；
- 不改触发阈值与澄清判定逻辑。

## 3. 设计

### 3.1 `NormalizedEvent`

新增 `src/pismor/pipeline/normalize.py`：

```python
@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    event_type: Literal["conversation", "text"]  # 唯一用途：sources.origin_type 落库
    mode: Literal["dialogue", "document"]        # 策略判断依据
    prompt_text: str                             # 送抽取 LLM 的完整文本
    user_texts: tuple[str, ...]                  # 用户域文本；dialogue 为全部 user 消息
    force_extract: bool                          # 上报方显式要求抽取

    @property
    def last_user_text(self) -> str:
        return self.user_texts[-1] if self.user_texts else ""

    @property
    def is_dialogue(self) -> bool:
        return self.mode == "dialogue"
```

只承载"策略相关"的派生字段。`source` / `user_ref` / `ts` / `event_id` / `origin_ref` 仍由 runner 从原始 dict 读取，不放进视图——避免出现第二份事件真相。

### 3.2 `normalize(event) -> NormalizedEvent`

| 输入 | `mode` | `prompt_text` | `user_texts` | `force_extract` |
|---|---|---|---|---|
| conversation | `dialogue` | `_render_messages(messages)`（含 assistant 上下文） | 全部 `role == "user"` 且非空的 `text`，保持顺序 | `event.get("force_extract") is True` |
| text | `document` | `content` | `(content,)` | `False` |

- `event_type` 用 `event.get("event_type") or "text"` 兜底，与 runner 现有行为一致；
- conversation 的 `user_texts` 非空由 schema 保证（当前未提交改动已要求 messages 至少一条 user）；normalize 仍做一次防御性判断；
- `_render_messages` 从 `extractor.py` 迁到 `normalize.py`，成为唯一实现。

### 3.3 调用点改造

| 位置 | 现状 | 改为 |
|---|---|---|
| `runner._run` | `origin_type = event.get("event_type") or "text"`；`if origin_type == "conversation"` | `ev = normalize(event)`；`origin_type = ev.event_type`；`if ev.is_dialogue` |
| `runner._run` 澄清 | `text = _last_user_text(event)` | `text = ev.last_user_text`，删除 `_last_user_text` |
| `trigger.should_extract` | 入参 `event: dict`，遍历 `event["messages"]` | 入参 `ev: NormalizedEvent`，遍历 `ev.user_texts`；读 `ev.force_extract` |
| `extractor.extract` | 入参 `ev: dict`，按 `event_type` 选模板 | 入参 `ev: NormalizedEvent`，按 `ev.mode` 选模板；`user = ev.prompt_text` |
| `extractor._render_messages` | 本地实现 | 删除，迁至 `normalize.py` |
| `clarifier.match_incoming` | 接收 `text: str` | 不变 |

`intake.py` 保持现状：它负责把 pydantic 校验后的 `event_type` / `force_extract` 回写原始 dict，`normalize()` 依赖这个前提。

### 3.4 不变量

- 管线行为逐字节不变：同一事件经 normalize 后，送 LLM 的文本、触发结论、澄清匹配输入、强化词面与改造前一致；
- `event_type` 的读取点从 4 处降到 2 处（`normalize()`、`origin_type` 落库）；
- 下游函数签名不再接受裸 `dict` 做策略判断，新增分支必须改 `NormalizedEvent` 才能表达。

## 4. 方案取舍

| 方案 | 做法 | 取舍 |
|---|---|---|
| **1（选中）** | 独立 `normalize.py` + dataclass，下游签名改为对象 | 收敛彻底、类型可查；代价是 `test_trigger` / `test_extractor` 的 fixture 要改成 `normalize({...})` |
| 2 | Pydantic 模型上加 `.normalized()`，intake 返回模型 | 少一个文件；但落库/hash 仍需原始 dict，模型与 dict 两条路径并存，易混用 |
| 3 | 保留 dict，塞 `_mode` / `_user_texts` 派生键 | 改动最小；但无类型约束，`event_type` 仍可能被下游读到，只算半个 B 层 |

选 1 的理由：B 层的价值在"下游无法再绕过视图"，只有类型边界能保证这一点。

## 5. 验收

**测试**

- 新增 `tests/test_normalize.py`：
  - conversation：多 user/assistant 混排 → `prompt_text` 保留全部消息、`user_texts` 只含 user 且保序、`force_extract` 透传；
  - text：`mode == "document"`、`prompt_text == content`、`user_texts == (content,)`；
  - 缺省 `event_type` 走 text 兜底；`force_extract` 缺省为 False。
- 改 `tests/test_trigger.py` / `tests/test_extractor.py` 的 fixture 为 `normalize({...})`；
- `tests/test_runner.py` / `test_e2e.py` 行为断言不变，全量 pytest 通过。

**静态检查**

```bash
grep -rn 'get("event_type")\|\["event_type"\]' src/pismor
# 期望只剩 normalize.py 的读取与 intake.py 的回写
```

## 6. 改造顺序

1. 新增 `normalize.py` + `test_normalize.py`（不改任何现有调用）；
2. 改 `extractor.extract` 签名与模板选择，跑 `test_extractor`；
3. 改 `trigger.should_extract` 签名与遍历来源，跑 `test_trigger` / `test_runner`；
4. 改 `runner._run` 接线，删除 `_last_user_text` / `_render_messages`；
5. 全量 pytest + 上面的 grep 检查。

## 7. 基线依赖与执行方式（待确认）

当前 `main` 工作区存在**未提交改动**，与本设计直接相邻：

- `schemas.py`：`feedback` 被 `force_extract` 取代；messages 必须含至少一条 user；
- `intake.py`：把 `event_type` / `force_extract` 归一化回写原始 dict；
- `trigger.py`：`feedback` 触发改为 `force_extract` 触发。

本设计按这些改动**生效后**的语义编写（`force_extract`、user 文本非空）。实现必须在独立 worktree 进行，避免与原会话同时改同一批文件；基线取「`main` HEAD + 上述未提交 diff」。

待确认：是复制当前未提交 diff 作为 worktree 基线，还是等原会话提交后从新 HEAD 拉分支。两种方式对 B 层代码本身没有差别。
