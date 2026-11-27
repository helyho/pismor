#!/usr/bin/env bash
# 域6 韧性收尾（T24/T25）：并发装配、损坏库拒启（必须最后跑并自恢复）

# T24 并发装配：xargs -P8 并发 GET /memory ×16，全部 200 且每个响应可被 jq 解析
t23() {
  # 并发目标：t17u（域4 已不再预建记忆，按空包用户并发；并发正确性口径不变）
  local out n bad base i
  base="http://127.0.0.1:${PORT}/memory/bb/t17u"
  # 8 路并发 × 16 次：xargs 汇总状态码到 stdout（run_case 在命令替换子 shell 中，不能用 job control）
  out=$(seq 1 16 | xargs -P8 -I{} curl -s --max-time 15 -o /dev/null -w '%{http_code}\n' "$base")
  n=$(printf '%s\n' "$out" | grep -c '^[0-9]')
  if [[ "$n" != "16" ]]; then
    case_fail "并发响应数 $n != 16（输出: $(printf '%s' "$out" | paste -sd, -)）"
  fi
  bad=$(printf '%s\n' "$out" | grep -vx '200' | head -3 | paste -sd, -)
  if [[ -n "$bad" ]]; then
    case_fail "并发响应非全 200: $bad"
  fi
  # body 可解析：并发结束后串行取 4 次，全部必须能被 jq 解析
  for i in 1 2 3 4; do
    if ! curl -s --max-time 15 "$base" | jq -e . >/dev/null 2>&1; then
      case_fail "响应 body 无法被 jq 解析"
    fi
  done
  case_pass
}

# T25 损坏库拒启（收尾用例，不走 run_case——需操纵服务生命周期）
# 停服务 → 写坏 index.db → 重启 → 期望非 0 退出拒启 → 还原备份重启，healthz 恢复 200
# --keep-data 下跳过（该用例必然写坏库）
t24_corrupt_db() {
  local id="T25" name="损坏库拒启+自恢复" ok=1 detail="" bak rc=0 refused=0
  if [[ "${KEEP_DATA:-0}" == "1" ]]; then
    SKIP=$((SKIP + 1))
    RESULTS+=("[SKIP] $id $name — --keep-data 下跳过（会写坏库）")
    printf '[SKIP] %s %s — --keep-data 下跳过\n' "$id" "$name"
    return 0
  fi
  bak=$(mktemp "${TMPDIR:-/tmp}/pismor-index.XXXXXX")
  if ! cp data/index.db "$bak" 2>/dev/null; then
    ok=0
    detail="无法备份 data/index.db"
  fi
  echo "  (T25: 接下来 serve.log 中的 'file is not a database' traceback 是预期产物)"
  kill "$SVC_PID" 2>/dev/null
  wait "$SVC_PID" 2>/dev/null
  echo garbage >data/index.db

  PYTHONPATH=src .venv/bin/python scripts/serve.py \
    --settings config/settings.example.yaml --host 127.0.0.1 --port "$PORT" \
    >>tests/blackbox/serve.log 2>&1 &
  SVC_PID=$!
  local _i
  for _i in $(seq 1 30); do
    if ! kill -0 "$SVC_PID" 2>/dev/null; then
      wait "$SVC_PID" 2>/dev/null
      rc=$?
      refused=1
      break
    fi
    sleep 1
  done
  if [[ "$refused" == "0" ]]; then
    # 30s 仍存活：若 healthz 健康 = 静默接受坏库；否则（未就绪）也视为拒启兜底
    if curl -s --max-time 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"ok": *true'; then
      ok=0
      detail="损坏库被静默接受（健康启动）"
    fi
  elif [[ "$rc" == "0" ]]; then
    ok=0
    detail="坏库启动进程退出码 0（应非 0）"
  fi

  # 恢复：还原备份（--keep-data 友好）并重启，确认 healthz 恢复
  kill "$SVC_PID" 2>/dev/null
  wait "$SVC_PID" 2>/dev/null
  rm -f data/index.db
  if [[ -s "$bak" ]]; then cp "$bak" data/index.db; fi
  rm -f "$bak"
  PYTHONPATH=src .venv/bin/python scripts/serve.py \
    --settings config/settings.example.yaml --host 127.0.0.1 --port "$PORT" \
    >>tests/blackbox/serve.log 2>&1 &
  SVC_PID=$!
  local recovered=0
  for _i in $(seq 1 30); do
    if curl -s --max-time 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"ok": *true'; then
      recovered=1
      break
    fi
    sleep 1
  done
  [[ "$recovered" == "1" ]] || { ok=0; detail="${detail:+$detail; }还原后服务未就绪"; }

  if [[ "$ok" == "1" ]]; then
    PASS=$((PASS + 1))
    RESULTS+=("[PASS] $id $name")
    printf '[PASS] %s %s\n' "$id" "$name"
  else
    FAIL=$((FAIL + 1))
    RESULTS+=("[FAIL] $id $name")
    printf '[FAIL] %s %s\n    %s\n' "$id" "$name" "$detail"
  fi
  return 0
}

run_domain_resilience() {
  run_case T24 "并发装配" -- t23
  t24_corrupt_db
}
