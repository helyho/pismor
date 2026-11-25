#!/usr/bin/env bash
# 域6 韧性收尾（T24-T26）：并发装配、非法路径、损坏库拒启（必须最后跑并自恢复）

# T24 并发装配：xargs -P8 并发 GET /profile ×16，全 200 且可被 jq 解析
t24() {
  # 并发目标：t17u（域4 已建画像；若 T17 被跳过则为空包用户，并发正确性口径不变）
  local out
  out=$(seq 1 16 | xargs -P8 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
    "http://127.0.0.1:${PORT}/profile/bb/t17u" | sort | uniq -c | tr -d ' ' | paste -sd, -)
  [[ "$out" == "16,200" ]] || { case_fail "并发响应非全 200: $out"; }
  http GET /profile/bb/t17u
  printf '%s' "$BODY" | jq -e . >/dev/null 2>&1 || { case_fail "响应不可被 jq 解析"; }
  case_pass
}

# T25 非法导出路径：单段穿越应用层 400；多段穿越被路由层拒绝且不泄漏文件内容
t25() {
  http GET "/export/bb/%2e%2e"
  expect_status 400 || return 1
  http GET "/export/bb/..%2F..%2Fetc"
  [[ "$STATUS" == "400" || "$STATUS" == "404" ]] || {
    printf '    穿越 URL 期望 4xx 拒绝, got %s\n' "$STATUS"
    return 1
  }
  printf '%s' "$BODY" | grep -q 'root:' && { case_fail "穿越响应泄漏了系统文件内容"; }
  case_pass
}

# T26 损坏库拒启（收尾用例，不走 run_case——需操纵服务生命周期）
# 停服务 → 写坏 index.db → 重启 → 期望拒启（退出非 0 或 healthz 30s 不就绪）
# → 恢复：清库重启，healthz 恢复 200
t26_corrupt_db() {
  local id="T26" name="损坏库拒启+自恢复" ok=1 detail=""
  kill "$SVC_PID" 2>/dev/null
  wait "$SVC_PID" 2>/dev/null
  echo garbage >data/index.db

  PYTHONPATH=src .venv/bin/python scripts/serve.py \
    --settings config/settings.example.yaml --host 127.0.0.1 --port "$PORT" \
    >>tests/blackbox/serve.log 2>&1 &
  SVC_PID=$!
  local refused=0
  for _ in $(seq 1 30); do
    if ! kill -0 "$SVC_PID" 2>/dev/null; then refused=1; break; fi # 进程已退出
    [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)" == "200" ]] && break
    sleep 1
  done
  # 进程存活且 healthz 200 = 损坏库被静默接受 → FAIL；其余（退出/不就绪）= 拒启
  if [[ "$refused" == "0" ]] \
    && kill -0 "$SVC_PID" 2>/dev/null \
    && [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)" == "200" ]]; then
    ok=0
    detail="损坏库被静默接受（健康启动）"
  fi

  # 恢复：清库重启，确认 healthz 恢复
  kill "$SVC_PID" 2>/dev/null
  wait "$SVC_PID" 2>/dev/null
  rm -rf data
  PYTHONPATH=src .venv/bin/python scripts/serve.py \
    --settings config/settings.example.yaml --host 127.0.0.1 --port "$PORT" \
    >>tests/blackbox/serve.log 2>&1 &
  SVC_PID=$!
  local recovered=0
  for _ in $(seq 1 30); do
    [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)" == "200" ]] && { recovered=1; break; }
    sleep 1
  done
  [[ "$recovered" == "1" ]] || { ok=0; detail="${detail:+$detail; }恢复后服务未就绪"; }

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
  run_case T24 "并发装配" -- t24
  run_case T25 "非法导出路径" -- t25
  t26_corrupt_db
}
