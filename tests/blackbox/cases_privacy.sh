#!/usr/bin/env bash
# 域4 删除权（T18-T19）：删除全链、tombstone 持续
# event_id 一律经 next_event_id 生成：retry 重跑拿新 id，不撞产品幂等去重
# T18 删除权全链（LLM 自建数据）：tombstones 留痕、关联表清空、profile 空包
t18() {
  need_llm || return 2
  local ev; ev=$(next_event_id t19e)
  http POST /events "$(bb_event t19u "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t19u'" 90 \
    || { case_fail "90s 内条目未入库"; }
  local ids
  ids=$(db "SELECT group_concat(id) FROM entries WHERE user_source='bb' AND user_id='t19u'")
  [[ -n "$ids" ]] || { case_fail "未取到条目 id，删除链无数据可验"; }
  http DELETE /profile/bb/t19u
  expect_status 200 || return 1
  expect_json_true '.deleted' || return 1
  expect_db "SELECT count(*) FROM tombstones WHERE user_source='bb' AND user_id='t19u' AND requested_by='api'" "1" || return 1
  expect_db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  expect_db "SELECT count(*) FROM sources WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  expect_db "SELECT count(*) FROM review_history WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  expect_db "SELECT count(*) FROM pending_clarifications WHERE user_source='bb' AND user_id='t19u'" "0" || return 1
  http GET /profile/bb/t19u
  expect_status 200 || return 1
  expect_json '.entries' '[]' || return 1
  case_pass
}
# T19 tombstone 持续（LLM）：删后含触发词事件不得复活画像
t19() {
  need_llm || return 2
  local e1 e2
  e1=$(next_event_id t20e)
  e2=$(next_event_id t20e2)
  http POST /events "$(bb_event t20u "$e1" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t20u'" 90 \
    || { case_fail "90s 内条目未入库"; }
  http DELETE /profile/bb/t20u
  expect_status 200 || return 1
  expect_json_true '.deleted' || return 1
  http POST /events "$(bb_event t20u "$e2" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM events_seen WHERE event_id='$e2'" 30 || { case_fail "删后事件未被接受"; }
  sleep 45 # 后台管线处理时延
  # 产品语义：tombstone 在 intake 拦截——已删除用户的新事件不调 LLM、不写库；
  # 装配同样恒空包
  expect_db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='t20u'" "0" || return 1
  expect_db "SELECT count(*) FROM sources WHERE user_source='bb' AND user_id='t20u'" "0" || return 1
  http GET /profile/bb/t20u
  expect_status 200 || return 1
  expect_json '.entries' '[]' || return 1
  case_pass
}
run_domain_privacy() {
  run_case T18 "删除权全链" -- t18
  run_case T19 "tombstone持续" --retry -- t19
}
