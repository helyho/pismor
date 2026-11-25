#!/usr/bin/env bash
# 域1 事件接入（T1-T7）：POST /events 合法性、幂等、畸形、LLM 入库路径

# T1 healthz+schema：无 LLM 依赖
t1() {
  http GET /healthz
  expect_status 200 || return 1
  expect_json_true '.ok' || return 1
  expect_json '.db' 'ok' || return 1
  local t
  for t in entries sources events_seen tombstones review_history pending_clarifications; do
    expect_db "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='$t'" "1" || return 1
  done
  case_pass
}

run_domain_events() {
  run_case T1 "healthz+schema" -- t1
}
