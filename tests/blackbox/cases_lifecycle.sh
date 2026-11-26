#!/usr/bin/env bash
# 域3 生命周期/复核（T14-T17）：矛盾收敛、verdict 流转、复核队列、长消息触发
# event_id 一律经 next_event_id 生成：retry 重跑拿新 id，不撞产品幂等去重
# T14 矛盾收敛（LLM）：两条 lang 主题矛盾指令，终态生效条目至多 1 条
t14() {
  need_llm || return 2
  local e1 e2
  e1=$(next_event_id t13a)
  e2=$(next_event_id t13b)
  http POST /events "$(bb_event t13u "$e1" "以后都用 Python 3.12 写脚本")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t13u'" 120 \
    || { case_fail "第一条 120s 未入库"; }
  http POST /events "$(bb_event t13u "$e2" "以后别用 Python 了, 改用 Rust")"
  expect_status 202 || return 1
  # 第二条的管线完成信号用 sources 行（extract 落库），而不是 events_seen（同步写、不代表处理完）
  if ! wait_db "SELECT 1 FROM sources WHERE user_source='bb' AND user_id='t13u' AND origin_ref='blackbox:$e2'" 120; then
    sleep 20 # 第二条可能被判重复/免提问：给收敛留时间后仍按性质断言
  fi
  # 轮询收敛（最多 60s）：active lang 条目至多 1 条
  local waited=0 n=99
  while ((waited < 60)); do
    n=$(db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='t13u' AND topic='lang' AND status='active'")
    [[ "$n" -le 1 ]] && break
    sleep 2
    waited=$((waited + 2))
  done
  [[ "$n" -le 1 ]] || { case_fail "lang 主题 active 条目 ${n} 条，矛盾未收敛"; }
  http GET /profile/bb/t13u
  expect_status 200 || return 1
  case_pass
}
# T15 verdict 流转（LLM 建数据，流转本身确定）：approve→旧版本 noop→discard 行消失
# user 每次 attempt 唯一（retry 重跑不污染 review_history 计数）
t15() {
  need_llm || return 2
  local uid="t14u${RANDOM}" ev; ev=$(next_event_id t14e)
  http POST /events "$(bb_event "$uid" "$ev" "记住, 我自己的小脚本默认都用 Python 写")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_source='bb' AND e.user_id='${uid}' AND e.dimension='preference' AND e.topic='lang'" 120 \
    || { case_fail "120s 内条目未入库"; }
  local entry_id ver ver2 scope
  entry_id=$(db "SELECT id FROM entries WHERE user_source='bb' AND user_id='${uid}' AND topic='lang' ORDER BY rowid DESC LIMIT 1")
  [[ -n "$entry_id" ]] || { case_fail "未取到 entry_id"; }
  scope=$(db "SELECT scope FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}' ORDER BY rowid DESC LIMIT 1")
  ver=$(db "SELECT updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}' AND scope='${scope}' ORDER BY rowid DESC LIMIT 1")
  [[ -n "$scope" && -n "$ver" ]] || { case_fail "未取到 scope/updated_at"; }
  local vpath="/review/bb/${uid}/${scope}/${entry_id}/verdict"
  # updated_at 是秒级：approve 前跨一秒，保证版本推进，"旧版本重发"才有 noop 语义
  sleep 1
  http POST "$vpath" "$(jq -n --arg verdict approve --arg v "$ver" '{verdict:$verdict, entry_version:$v}')"
  expect_status 200 || return 1
  expect_json '.noop' 'false' || return 1
  expect_db "SELECT count(*) FROM review_history WHERE user_source='bb' AND user_id='${uid}' AND entry_id='${entry_id}'" "1" || return 1
  # 旧版本重发 → noop（版本失配保护）
  http POST "$vpath" "$(jq -n --arg verdict discard --arg v "$ver" '{verdict:$verdict, entry_version:$v}')"
  expect_status 200 || return 1
  expect_json_true '.noop' || return 1
  # 新版本 discard → 该条目消失
  ver2=$(db "SELECT updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}' AND scope='${scope}' ORDER BY rowid DESC LIMIT 1")
  [[ -n "$ver2" ]] || { case_fail "discard 前 entry 已消失"; }
  if [[ "$ver2" == "$ver" ]]; then
    sleep 1
    ver2=$(db "SELECT updated_at FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}' AND scope='${scope}' ORDER BY rowid DESC LIMIT 1")
  fi
  [[ "$ver2" != "$ver" ]] || { case_fail "approve 后 updated_at 未推进（版本轮转失效）"; }
  http POST "$vpath" "$(jq -n --arg verdict discard --arg v "$ver2" '{verdict:$verdict, entry_version:$v}')"
  expect_status 200 || return 1
  expect_json '.noop' 'false' || return 1
  expect_db "SELECT count(*) FROM entries WHERE user_source='bb' AND user_id='${uid}' AND id='${entry_id}'" "0" || return 1
  case_pass
}
# T16 复核队列结构：200、items 为数组、非空时键齐全
t16() {
  http GET /review/queue
  expect_status 200 || return 1
  expect_json '.items | type' 'array' || return 1
  local k
  for k in entry_id status entry_version; do
    expect_json "[.items[] | select(has(\"$k\") | not)] | length" '0' || return 1
  done
  case_pass
}
# T17 长消息触发（LLM）：>300 字符无触发词纯文本，管线后入库（性质级）
t17() {
  need_llm || return 2
  local long ev
  long=$(printf '我们团队的工作流是这样的, 需求评审之后进入开发, 开发完成提交代码评审, 评审通过再合入主干. 日常站会十五分钟, 迭代周期两周. 发布流程走灰度, 先百分之五再全量. 监控告警接企微, 线上问题四小时内响应. 文档随代码一起维护, 接口变更同步更新 openapi 描述. %.0s' $(seq 1 5))
  ev=$(next_event_id t16e)
  http POST /events "$(bb_event t16u "$ev" "$long")"
  expect_status 202 || return 1
  wait_db "SELECT 1 FROM entries e WHERE e.user_id='t16u'" 120 \
    || { case_fail "长消息 120s 内未入库"; }
  case_pass
}
run_domain_lifecycle() {
  run_case T14 "矛盾收敛" --retry -- t14
  run_case T15 "verdict流转" --retry -- t15
  run_case T16 "复核队列结构" -- t16
  run_case T17 "长消息触发" --retry -- t17
}
