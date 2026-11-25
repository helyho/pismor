#!/usr/bin/env bash
# pismor 黑盒测试 helper —— 由 run.sh source，勿直接执行。
# 约定：用例函数 return 0=PASS，1=FAIL，2=SKIP（SKIP_REASON 写原因）。
# 依赖 run.sh 预置并 export 的变量：PORT、TIMEOUT；工作目录恒为仓库根。

BLACKBOX_DB="./data/index.db"

# http METHOD PATH [JSON_BODY] → 设置全局 $STATUS / $BODY
http() {
  local method="$1" path="$2" body="${3-}" tmp
  tmp=$(mktemp)
  local args=(-s -X "$method" -o "$tmp" -w '%{http_code}' "http://127.0.0.1:${PORT}${path}")
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
    [[ -n "$(sqlite3 "$BLACKBOX_DB" "$sql" 2>/dev/null)" ]] && return 0
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

# promote_active <user> <dimension>：条目停在 pending_review 时走 verdict approve
# 使其生效（产品正常路径：待复核条目不进包）。已是 active/degraded 直接返回 0。
promote_active() {
  local uid="$1" dim="$2" st eid ver scope
  st=$(db "SELECT status FROM entries WHERE user_id='${uid}' AND dimension='${dim}' ORDER BY rowid DESC LIMIT 1")
  [[ "$st" == "active" || "$st" == "degraded" ]] && return 0
  eid=$(db "SELECT id FROM entries WHERE user_id='${uid}' AND dimension='${dim}' ORDER BY rowid DESC LIMIT 1")
  [[ -n "$eid" ]] || { printf '    promote_active: 未找到 %s/%s 条目\n' "$uid" "$dim"; return 1; }
  ver=$(db "SELECT updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${eid}' ORDER BY rowid DESC LIMIT 1")
  scope=$(db "SELECT scope FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${eid}' ORDER BY rowid DESC LIMIT 1")
  http POST "/review/bb/${uid}/${scope}/${eid}/verdict" \
    "$(jq -n --arg verdict approve --arg v "$ver" '{verdict:$verdict, entry_version:$v}')"
  expect_status 200 || return 1
  wait_db "SELECT 1 FROM entries WHERE user_id='${uid}' AND dimension='${dim}' AND status IN ('active','degraded')" 20
}

case_pass() { return 0; }
case_fail() { # case_fail <说明>
  printf '    %s\n' "$*"
  return 1
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
