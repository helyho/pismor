#!/usr/bin/env bash
# pismor 黑盒测试入口
# 用法: bash tests/blackbox/run.sh [--keep-data] [--port N] [--timeout S]
# 真实启动 scripts/serve.py（配置原样用 config/settings.example.yaml，不生成副本），
# curl 打 HTTP，断言 = 响应 JSON(jq) + SQLite 落库(sqlite3 查 ./data/index.db)。
# LLM_API_KEY 缺失时依赖 LLM 的用例标 [SKIP]，确定性用例照常跑。
set -u
cd "$(dirname "$0")/../.." # 强制仓库根（配置里 db/export 是相对 CWD 路径）

PORT=18300
TIMEOUT=90
KEEP_DATA=0
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-data) KEEP_DATA=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    *) echo "未知参数: $1（用法: run.sh [--keep-data] [--port N] [--timeout S] [--only events,assemble,...]）" >&2; exit 2 ;;
  esac
done

for t in curl jq sqlite3; do
  command -v "$t" >/dev/null 2>&1 || { echo "缺少依赖: $t"; exit 2; }
done
[[ -x .venv/bin/python ]] || { echo "缺少 .venv/bin/python（请先创建 venv）"; exit 2; }
[[ -f scripts/serve.py ]] || { echo "找不到 scripts/serve.py（需在仓库根运行）"; exit 2; }

# 默认清库保证幂等（data/ exports/ 已 gitignore）
if ((KEEP_DATA == 0)); then
  rm -rf data exports
fi

MODE=real
[[ -z "${LLM_API_KEY:-}" ]] && MODE=degraded
echo "== pismor 黑盒测试 mode=$MODE port=$PORT =="

# 服务启动：src 布局，venv 未 editable 安装，需 PYTHONPATH=src
PYTHONPATH=src .venv/bin/python scripts/serve.py \
  --settings config/settings.example.yaml --host 127.0.0.1 --port "$PORT" \
  >tests/blackbox/serve.log 2>&1 &
SVC_PID=$!
cleanup() {
  [[ -n "${SVC_PID:-}" ]] && kill "$SVC_PID" 2>/dev/null
  [[ -n "${SVC_PID:-}" ]] && wait "$SVC_PID" 2>/dev/null
}
trap cleanup EXIT

# 就绪探测：轮询 /healthz 至 200，30s 超时
ready=0
for _ in $(seq 1 30); do
  if [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)" == "200" ]]; then
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

export PORT TIMEOUT
# shellcheck source=lib.sh
source tests/blackbox/lib.sh
PASS=0
FAIL=0
SKIP=0
RESULTS=()

# 逐域执行（文件存在才 source；--only 过滤）
for d in events assemble lifecycle export injection resilience; do
  if [[ -n "$ONLY" && ",$ONLY," != *",$d,"* ]]; then continue; fi
  f="tests/blackbox/cases_${d}.sh"
  [[ -f "$f" ]] || continue
  # shellcheck disable=SC1090
  source "$f"
  "run_domain_${d}"
done

echo
echo "== 汇总 =="
printf '%s\n' "${RESULTS[@]}"
echo "passed $PASS/$((PASS + FAIL)) (skipped $SKIP)"
((FAIL == 0)) || exit 1
exit 0
