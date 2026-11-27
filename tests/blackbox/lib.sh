#!/usr/bin/env bash
# pismor 黑盒测试 helper —— 由 run.sh source，勿直接执行。
# 约定：用例函数 return 0=PASS，1=FAIL，2=SKIP（SKIP_REASON 写原因）。
# 依赖 run.sh 预置并 export 的变量：PORT、TIMEOUT；工作目录恒为仓库根。

BLACKBOX_DB="./data/index.db"

# http METHOD PATH [JSON_BODY] → 设置全局 $STATUS / $BODY
# --max-time 兜底：服务卡死时不让整个套件无限挂起
http() {
  local method="$1" path="$2" body="${3-}" tmp
  tmp=$(mktemp)
  local args=(-s --max-time 150 --connect-timeout 5 -X "$method" -o "$tmp" -w '%{http_code}' "http://127.0.0.1:${PORT}${path}")
  [[ -n "$body" ]] && args+=(-H 'Content-Type: application/json' -d "$body")
  STATUS=$(curl "${args[@]}")
  BODY=$(cat "$tmp")
  rm -f "$tmp"
}

expect_status() { # expect_status <code>
  if [[ "$STATUS" == "$1" ]]; then return 0; fi
  printf '    expect_status %s: got %s, body: %s\n' "$1" "$STATUS" "$(printf '%s' "$BODY" | head -c 200)"
  return 1
}

expect_json() { # expect_json <jq_expr> <expected>
  local got
  got=$(printf '%s' "$BODY" | jq -r "$1" 2>&1)
  if [[ "$got" == "$2" ]]; then return 0; fi
  printf '    expect_json %s: got [%s] want [%s]\n' "$1" "$got" "$2"
  return 1
}

expect_json_true() { expect_json "$1" "true"; }

db() { sqlite3 "$BLACKBOX_DB" "$1" 2>&1; }

expect_db() { # expect_db <SQL> <expected>
  local got
  got=$(db "$1")
  if [[ "$got" == "$2" ]]; then return 0; fi
  printf '    expect_db [%s]: got [%s] want [%s]\n' "$1" "$got" "$2"
  return 1
}

# wait_db <SQL> [timeout=TIMEOUT]：轮询直到 SQL 结果非空（返回 0）；超时返回 1。
# "该出现而未出现"的负向断言（如闲聊零入库）直接 ! wait_db 使用。
wait_db() {
  local sql="$1" timeout="${2:-$TIMEOUT}" waited=0
  while :; do
    # 注意 count(*) 在 0 行时输出 "0" 也非空：把 "0" 视为未就绪，防调用方写错条件
    local out
    out=$(sqlite3 "$BLACKBOX_DB" "$sql" 2>/dev/null)
    [[ -n "$out" && "$out" != "0" ]] && return 0
    sleep 1
    waited=$((waited + 1))
    ((waited >= timeout)) && return 1
  done
}

# need_llm：LLM_API_KEY 未设置 → 输出原因并返回 2（SKIP）
need_llm() {
  if [[ -n "${LLM_API_KEY:-}" ]]; then return 0; fi
  printf 'LLM_API_KEY 未设置\n'
  return 2
}

# next_event_id <base>：进程内唯一 event_id（retry 重跑拿新 id，不撞产品幂等去重）
# 用 $RANDOM（每次展开重新求值、跨子 shell 无状态）——子 shell 自增计数不回传，不能用
next_event_id() {
  printf '%s-%s-%d%d' "$1" "$$" "$RANDOM" "$RANDOM"
}

# bb_event <user> <event_id> <content>：构造合法事件体（jq 负责转义）
bb_event() {
  jq -n --arg u "$1" --arg e "$2" --arg c "$3" --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{source:"bb", user_ref:$u, event_id:$e, event_type:"text", content:$c, ts:$t, origin_ref:("blackbox:" + $e)}'
}

# bb_conv_event <user> <event_id> <messages-json>：conversation 事件（消息字段为 text）
bb_conv_event() {
  jq -n --arg u "$1" --arg e "$2" --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson m "$3" \
    '{source:"bb", user_ref:$u, event_id:$e, event_type:"conversation", session_id:("blackbox:" + $e), turn:1, ts:$t, messages:$m}'
}

# activate_entry <user> <dimension>：条目停在 pending_review 时走 decision confirm
# 使其生效（产品正常路径：待复核条目不进包）。已是 active/degraded 直接返回 0。
# user_id 不是唯一键（PK 含 user_source），故查询都带 user_source
activate_entry() {
  local uid="$1" dim="$2" st eid= ver=
  st=$(db "SELECT status FROM entries WHERE user_source='bb' AND user_id='${uid}' AND dimension='${dim}' ORDER BY rowid DESC LIMIT 1")
  [[ "$st" == "active" || "$st" == "degraded" ]] && return 0
  IFS='|' read -r eid ver <<<"$(db "SELECT id || '|' || updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND dimension='${dim}' ORDER BY rowid DESC LIMIT 1")"
  [[ -n "$eid" && -n "$ver" ]] || { printf '    activate_entry: 未找到 %s/%s 条目\n' "$uid" "$dim"; return 1; }
  http POST "/review/bb/${uid}/${eid}/decision" \
    "$(jq -n --arg decision confirm --arg v "$ver" '{decision:$decision, entry_version:$v}')"
  expect_status 200 || return 1
  expect_json '.noop' 'false' || return 1
  wait_db "SELECT 1 FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${eid}' AND status IN ('active','degraded')" 30
}

# activate_all <uid>：把该用户所有非生效条目逐条 confirm。
# 装配只取 status IN (active,degraded)，而 LLM 置信度会随机落在 pending_review；
# 用例断言"能被召回"时用它消除随机性（不掩盖问题，只是把前置条件固定）。
activate_all() {
  local uid="$1" eid ver
  while IFS='|' read -r eid ver; do
    [[ -n "$eid" ]] || continue
    http POST "/review/bb/${uid}/${eid}/decision" \
      "$(jq -n --arg decision confirm --arg v "$ver" '{decision:$decision, entry_version:$v}')"
    if [[ "$STATUS" != 200 ]]; then
      printf '    activate_all: %s 复核返回 %s\n' "$eid" "$STATUS"
      return 1
    fi
  done < <(db "SELECT id || '|' || updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND status NOT IN ('active','degraded')")
  return 0
}

case_pass() { return 0; }

# case_fail：立即终止本用例（run_case 在子 shell 捕获非 0 退出码）。
# 必须 exit 而非 return——用例函数末尾的 case_pass 会把 return 码吞成 0。
case_fail() { # case_fail <说明>
  printf '    %s\n' "$*"
  exit 1
}

# run_case <id> <名称> [--retry] -- <fn>
# --retry：失败后隔 5s 整用例重跑一次（LLM 随机性容错），PASS 则记 PASS
run_case() {
  local id="$1" name="$2"
  shift 2
  local retry=0
  if [[ "${1:-}" == "--retry" ]]; then retry=1; shift; fi
  [[ "${1:-}" == "--" ]] && shift
  local fn="$1"
  local attempt out rc
  for attempt in 0 1; do
    out=$("$fn" 2>&1)
    rc=$?
    ((rc == 0 || rc == 2)) && break
    ((retry == 1 && attempt == 0)) || break
    sleep 5
  done
  case "$rc" in
    0)
      PASS=$((PASS + 1)); RESULTS+=("[PASS] $id $name")
      printf '[PASS] %s %s\n' "$id" "$name"
      ;;
    2)
      SKIP=$((SKIP + 1)); RESULTS+=("[SKIP] $id $name — ${out:-未知原因}")
      printf '[SKIP] %s %s — %s\n' "$id" "$name" "${out:-未知原因}"
      ;;
    *)
      FAIL=$((FAIL + 1)); RESULTS+=("[FAIL] $id $name")
      printf '[FAIL] %s %s\n' "$id" "$name"
      printf '%s\n' "$out" | sed 's/^/    /'
      ;;
  esac
  return 0
}

# skip_case <id> <原因>：不经用例函数直接登记 SKIP
skip_case() {
  SKIP=$((SKIP + 1)); RESULTS+=("[SKIP] $1 — $2")
  printf '[SKIP] %s — %s\n' "$1" "$2"
  return 0
}

# --- 向量分支 helper（T26）--------------------------------------------------
# settings 文件：run.sh 通过 BB_SETTINGS 传给用例；缺省按产品默认路径。
BB_SETTINGS="${BB_SETTINGS:-config/settings.yaml}"

urienc() { printf '%s' "$1" | jq -sRr @uri; }

# need_embed：embedding 未启用、端点不可达、维度与配置不一致 → 2（SKIP）
# 探测同时校验 data[0].embedding 维度，避免"端点通但模型/维度配错"被误判为覆盖。
need_embed() {
  local out rc
  out=$(PYTHONPATH=src .venv/bin/python - <<'PY' 2>&1
import os, sys
from pathlib import Path
import httpx
from pismor.core.config import load_settings
e = load_settings(Path(os.environ.get("BB_SETTINGS", "config/settings.yaml"))).embedding
if not e.enabled:
    print("embedding.enabled=false"); sys.exit(2)
url = e.endpoint.rstrip("/")
url = url if url.endswith("/embeddings") else url + "/embeddings"
headers = {"Content-Type": "application/json"}
key = os.environ.get(e.api_key_env)
if key:
    headers["Authorization"] = f"Bearer {key}"
try:
    r = httpx.post(url, headers=headers, json={"model": e.model, "input": ["probe"]}, timeout=8)
except Exception as exc:
    print(f"embed endpoint unreachable: {type(exc).__name__}: {exc}"); sys.exit(2)
if not (200 <= r.status_code < 300):
    print(f"embed endpoint returned {r.status_code}"); sys.exit(2)
try:
    dim = len(r.json()["data"][0]["embedding"])
except Exception as exc:
    print(f"embed endpoint bad body: {type(exc).__name__}: {exc}"); sys.exit(2)
if dim != e.dimensions:
    print(f"embed dim {dim} != config dimensions {e.dimensions}"); sys.exit(2)
sys.exit(0)
PY
)
  rc=$?
  ((rc == 0)) && return 0
  printf '%s\n' "$out"
  return 2
}

# vec_count <user_id>：该用户 embeddings 行数。
# embeddings 是 vec0 虚拟表，sqlite3 CLI 会报 "no such module: vec0"，
# 必须走 pismor.core.db.get_conn（内部加载 sqlite_vec 扩展）。
# schema v2 起 embeddings.entry_id 就是 entries.id（32 位哈希），直接等值 JOIN。
vec_count() {
  PYTHONPATH=src .venv/bin/python - "${1:?vec_count 需要 user_id}" <<'PY'
import sys
from pathlib import Path
from pismor.core.db import get_conn
conn = get_conn(Path("./data/index.db"))
sql = ("SELECT count(*) FROM embeddings v JOIN entries e ON v.entry_id = e.id"
       " WHERE e.user_source='bb' AND e.user_id=?")
print(conn.execute(sql, (sys.argv[1],)).fetchone()[0])
PY
}

# pick_semantic_query <body> → "<query>\t<cos>"；无可用候选输出空。
# 候选需同时满足：与 body 的 jieba token 交集为空（词面召回必空）、
# cosine ≥ min_similarity + 0.05（留余量，避免正文随机改写后擦线）。
pick_semantic_query() {
  PYTHONPATH=src .venv/bin/python - "${1:?pick_semantic_query 需要 body}" <<'PY'
import math, os, sys
from pathlib import Path
import httpx, jieba
from pismor.core.config import load_settings
body = sys.argv[1]
e = load_settings(Path(os.environ.get("BB_SETTINGS", "config/settings.yaml"))).embedding
cands = ["平时做开发选哪种语言", "编程语言的选择习惯", "开发时倾向的编程语言",
         "写程序时喜欢用的语言", "推荐的编程语言"]
url = e.endpoint.rstrip("/")
url = url if url.endswith("/embeddings") else url + "/embeddings"
headers = {"Content-Type": "application/json"}
key = os.environ.get(e.api_key_env)
if key:
    headers["Authorization"] = f"Bearer {key}"
r = httpx.post(url, headers=headers, json={"model": e.model, "input": [body] + cands},
               timeout=e.timeout_seconds)
r.raise_for_status()
data = r.json()["data"]
if all(isinstance(x.get("index"), int) for x in data):
    data = sorted(data, key=lambda x: x["index"])
vecs = [x["embedding"] for x in data]
def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
body_tokens = set(jieba.cut(body))
best = None
for cand, vec in zip(cands, vecs[1:]):
    similarity = cos(vecs[0], vec)
    if body_tokens & set(jieba.cut(cand)):
        continue  # 词面有交集 → 无法判别是向量还是 BM25
    if similarity < e.min_similarity + 0.05:
        continue
    if best is None or similarity > best[1]:
        best = (cand, similarity)
if best:
    print(f"{best[0]}\t{best[1]:.3f}")
PY
}
