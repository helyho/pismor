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

# T2 合法事件接入（LLM）：202 + events_seen 落库
t2() {
  need_llm || return 2
  http POST /events "$(bb_event t2u t2e "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  expect_json '.event_id | length > 0' 'true' || return 1
  wait_db "SELECT 1 FROM events_seen WHERE source='bb' AND event_id='t2e'" \
    || { case_fail "events_seen 90s 内未出现 t2e"; }
  case_pass
}

# T3 幂等去重：同 event_id 二次 POST → 200 deduplicated，events_seen 仅 1 行
t3() {
  local body
  body=$(bb_event t3u t3e "记住, 我自己的小脚本默认都用 Python 写")
  http POST /events "$body"
  expect_status 202 || return 1
  if ! wait_db "SELECT 1 FROM events_seen WHERE source='bb' AND event_id='t3e'" 15; then
    need_llm || return 2 # events_seen 可能待管线成功才写，无 key 场景跳过
    return 1
  fi
  http POST /events "$body"
  expect_status 200 || return 1
  expect_json_true '.deduplicated' || return 1
  expect_db "SELECT count(*) FROM events_seen WHERE source='bb' AND event_id='t3e'" "1" || return 1
  case_pass
}

# T4 畸形事件：422 且不落 events_seen
t4() {
  http POST /events '{"source":"bb","user_ref":"t4u","event_id":"t4a","content":"hello"}'
  expect_status 422 || return 1
  http POST /events '{"source":"bb","user_ref":"t4u","event_id":"t4b","event_type":"text"}'
  expect_status 422 || return 1
  expect_db "SELECT count(*) FROM events_seen WHERE event_id IN ('t4a','t4b')" "0" || return 1
  case_pass
}

# T5 content 超限（>8192）：422 且不落库
t5() {
  local big
  big=$(head -c 9000 /dev/zero | tr '\0' 'a')
  http POST /events "$(bb_event t5u t5e "$big")"
  expect_status 422 || return 1
  expect_db "SELECT count(*) FROM events_seen WHERE event_id='t5e'" "0" || return 1
  case_pass
}

# T6 explicit 直通入库（LLM）：taxonomy 回归样例 1
t6() {
  need_llm || return 2
  http POST /events "$(bb_event t6u t6e "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  local cond="e.user_source='bb' AND e.user_id='t6u' AND e.dimension='preference' AND e.topic='lang' AND e.scope='global'"
  wait_db "SELECT 1 FROM entries e WHERE $cond AND e.status='active'" 90 \
    || { case_fail "90s 内 entries 未出现 preference/lang/global active 行"; }
  expect_db "SELECT count(*) FROM sources s JOIN entries e ON s.entry_id=e.id WHERE $cond AND s.kind='explicit'" "1" || return 1
  expect_db "SELECT count(*) FROM sources s JOIN entries e ON s.entry_id=e.id WHERE $cond AND s.origin_ref='blackbox:t6e'" "1" || return 1
  case_pass
}

# T7 闲聊零入库（LLM）：90s 窗口内 entries/sources 均无该用户行
t7() {
  need_llm || return 2
  http POST /events "$(bb_event t7u t7e "今天天气不错")"
  expect_status 202 || return 1
  if wait_db "SELECT 1 FROM entries e WHERE e.user_source='bb' AND e.user_id='t7u'" 90; then
    case_fail "闲聊也入库了"
  fi
  expect_db "SELECT count(*) FROM sources s JOIN entries e ON s.entry_id=e.id WHERE e.user_source='bb' AND e.user_id='t7u'" "0" || return 1
  case_pass
}

run_domain_events() {
  run_case T1 "healthz+schema" -- t1
  run_case T2 "合法事件接入" --retry -- t2
  run_case T3 "幂等去重" -- t3
  run_case T4 "畸形事件422" -- t4
  run_case T5 "content超限422" -- t5
  run_case T6 "explicit直通入库" --retry -- t6
  run_case T7 "闲聊零入库" --retry -- t7
}
