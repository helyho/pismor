#!/usr/bin/env bash
# pismor 黑盒测试入口
# 用法: bash tests/blackbox/run.sh [--keep-data] [--port N] [--timeout S] [--only events,assemble,vector,...]
# 真实启动 scripts/serve.py（配置原样用 config/settings.example.yaml，不生成副本），
# curl 打 HTTP，断言 = 响应 JSON(jq) + SQLite 落库(sqlite3 查 ./data/index.db)。
# LLM_API_KEY 缺失时依赖 LLM 的用例标 [SKIP]，确定性用例照常跑。
set -u
cd "$(dirname "$0")/../.." # 强制仓库根（配置里 db 是相对 CWD 路径）

PORT=18300
TIMEOUT=90
KEEP_DATA=0
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-data) KEEP_DATA=1; shift ;;
    --port) PORT="${2:?--port 需要端口号}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout 需要秒数}"; shift 2 ;;
    --only) ONLY="${2:?--only 需要域名列表}"; shift 2 ;;
    *) echo "未知参数: $1（用法: run.sh [--keep-data] [--port N] [--timeout S] [--only events,assemble,vector,lifecycle,privacy,injection,resilience]）" >&2; exit 2 ;;
  esac
done

for t in curl jq sqlite3 lsof; do
  command -v "$t" >/dev/null 2>&1 || { echo "缺少依赖: $t"; exit 2; }
done
[[ -x .venv/bin/python ]] || { echo "缺少 .venv/bin/python（请先创建 venv）"; exit 2; }
[[ -f scripts/serve.py ]] || { echo "找不到 scripts/serve.py（需在仓库根运行）"; exit 2; }
# 预检：测试独占 ./data（清库 + T25 故意写坏库），检测到运行中的服务实例则拒绝
if pgrep -f "scripts/serve.py" >/dev/null 2>&1; then
  echo "检测到运行中的 pismor 服务实例：" >&2
  pgrep -fl "scripts/serve.py" >&2
  echo "测试会清空 ./data 并在 T25 故意写坏 index.db，请先停止服务再跑测试。" >&2
  exit 2
fi
# 端口预检：避免就绪探测打到外来服务，随后把事件 POST 给它
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用：" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  exit 2
fi

# 默认清库保证幂等（data/ 已 gitignore）
if ((KEEP_DATA == 0)); then
  rm -rf data
fi

SETTINGS=config/settings.example.yaml
MODE=real
[[ -z "${LLM_API_KEY:-}" ]] && MODE=degraded
echo "== pismor 黑盒测试 mode=$MODE port=$PORT =="

# 服务启动：src 布局，venv 未 editable 安装，需 PYTHONPATH=src
PYTHONPATH=src .venv/bin/python scripts/serve.py \
  --settings "$SETTINGS" --host 127.0.0.1 --port "$PORT" \
  >tests/blackbox/serve.log 2>&1 &
SVC_PID=$!
cleanup() {
  [[ -n "${SVC_PID:-}" ]] && kill "$SVC_PID" 2>/dev/null
  [[ -n "${SVC_PID:-}" ]] && wait "$SVC_PID" 2>/dev/null
}
trap cleanup EXIT

# 就绪探测：轮询 /healthz 至 .ok==true 且 .db=="ok"，30s 超时（进程死/端口被抢都不算就绪）
ready=0
for _ in $(seq 1 30); do
  if kill -0 "$SVC_PID" 2>/dev/null \
    && curl -s --max-time 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | jq -e '.ok==true and .db=="ok"' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if ((ready == 0)); then
  echo "服务 30s 未就绪，serve.log 尾部："
  tail -30 tests/blackbox/serve.log
  exit 1
fi
echo "服务就绪 pid=$SVC_PID"

export PORT TIMEOUT KEEP_DATA BB_SETTINGS="$SETTINGS"
# shellcheck source=lib.sh
source tests/blackbox/lib.sh

# 自检：case_fail 必须让用例失败（防止断言被静默吞掉的回归）
if ( _selfcheck() { false; case_fail "selftest"; case_pass; }; _selfcheck ) >/dev/null 2>&1; then
  echo "自检失败：case_fail 未传播失败，断言会被静默吞掉" >&2
  exit 2
fi

PASS=0
FAIL=0
SKIP=0
RESULTS=()

# 逐域执行（文件存在才 source；--only 过滤）
for d in events assemble vector lifecycle privacy injection resilience; do
  if [[ -n "$ONLY" && ",$ONLY," != *",$d,"* ]]; then continue; fi
  f="tests/blackbox/cases_${d}.sh"
  [[ -f "$f" ]] || continue
  # shellcheck disable=SC1090
  source "$f"
  "run_domain_${d}"
done

echo
echo "== 汇总 =="
printf '%s\n' ${RESULTS[@]+"${RESULTS[@]}"}
echo "passed $PASS/$((PASS + FAIL)) (skipped $SKIP)"
((FAIL == 0)) || exit 1
exit 0
