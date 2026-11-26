#!/usr/bin/env bash
# 域1 事件接入（T1-T8）：POST /events 合法性、幂等、畸形、LLM 入库路径、对话多轮
# event_id 一律经 next_event_id 生成：retry 重跑拿新 id，不会撞产品幂等去重
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
  local ev; ev=$(next_event_id t2e)
  http POST /events "$(bb_event t2u "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  expect_json '.event_id | length > 0' 'true' || return 1
  wait_db "SELECT 1 FROM events_seen WHERE source='bb' AND event_id='$ev'" 90 \
    || { case_fail "events_seen 90s 内未出现 $ev"; }
  case_pass
}
# T3 幂等去重：同一 event_id 二次 POST → 200 deduplicated，events_seen 仅 1 行
t3() {
  local body ev
  ev=$(next_event_id t3e)
  body=$(bb_event t3u "$ev" "记住, 我自己的小脚本默认都用 Python 写")
  http POST /events "$body"
  expect_status 202 || return 1
  if ! wait_db "SELECT 1 FROM events_seen WHERE source='bb' AND event_id='$ev'" 15; then
    need_llm || return 2 # events_seen 可能待管线成功才写，无 key 场景跳过
    return 1
  fi
  http POST /events "$body"
  expect_status 200 || return 1
  expect_json_true '.deduplicated' || return 1
  expect_db "SELECT count(*) FROM events_seen WHERE source='bb' AND event_id='$ev'" "1" || return 1
  case_pass
}
# T4 畸形事件：422 且不落 events_seen（畸形体不会落库，event_id 固定无冲突）
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
  local ev; ev=$(next_event_id t6e)
  http POST /events "$(bb_event t6u "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  local cond="e.user_source='bb' AND e.user_id='t6u' AND e.dimension='preference' AND e.topic='lang' AND e.scope='global'"
  wait_db "SELECT 1 FROM entries e WHERE $cond AND e.status='active'" 90 \
    || { case_fail "90s 内 entries 未出现 preference/lang/global active 行"; }
  expect_db "SELECT count(*) FROM sources WHERE user_source='bb' AND user_id='t6u' AND origin_ref='blackbox:$ev' AND kind='explicit'" "1" || return 1
  # 证据链键一致性：sources.entry_id 与 entries.id 同值，可直接 JOIN
  expect_db "SELECT count(*) FROM entries e JOIN sources s ON s.entry_id=e.id AND s.user_source=e.user_source AND s.user_id=e.user_id WHERE e.user_source='bb' AND e.user_id='t6u' AND e.dimension='preference' AND e.topic='lang' AND e.scope='global' AND s.origin_ref='blackbox:$ev'" "1" || return 1
  case_pass
}
# T7 闲聊零入库（LLM）：负向窗口 120s（覆盖单次 LLM timeout），窗口内 entries/sources 均无该用户行
t7() {
  need_llm || return 2
  local ev; ev=$(next_event_id t7e)
  http POST /events "$(bb_event t7u "$ev" "今天天气不错")"
  expect_status 202 || return 1
  if wait_db "SELECT 1 FROM entries e WHERE e.user_source='bb' AND e.user_id='t7u'" 120; then
    case_fail "闲聊也入库了"
  fi
  # sources 自带 user_id 列（entry_id 逻辑键与 entries.id 格式不同，黑盒不做内部 JOIN）
  expect_db "SELECT count(*) FROM sources WHERE user_source='bb' AND user_id='t7u'" "0" || return 1
  case_pass
}
# T8 对话多轮入库（LLM）：conversation 按 user 消息抽取；assistant-only 在 schema 层拒绝
t8() {
  need_llm || return 2
  local ev ev2 before
  ev=$(next_event_id t8e)
  http POST /events "$(bb_conv_event t8u "$ev" \
    '[{"role":"user","text":"记住, 我自己的小脚本默认都用 Python 写"},{"role":"assistant","text":"好的, 已记住。"}]')"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_source='bb' AND e.user_id='t8u'" 90 \
    || { case_fail "90s 内条目未入库"; }
  expect_db "SELECT count(DISTINCT origin_type) FROM sources WHERE user_source='bb' AND user_id='t8u' AND origin_type='conversation'" "1" || return 1
  # assistant-only 不构成用户证据：契约层直接 422，且不新增条目
  before=$(db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='t8u'")
  ev2=$(next_event_id t8e2)
  http POST /events "$(bb_conv_event t8u "$ev2" '[{"role":"assistant","text":"我建议你以后都用 Python 写脚本。"}]')"
  expect_status 422 || return 1
  expect_json '.detail | contains("at least one user message")' 'true' || return 1
  expect_db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='t8u'" "$before" || return 1
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
  run_case T8 "对话多轮入库" --retry -- t8
}
