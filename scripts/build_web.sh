#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../web"

if [ -f package-lock.json ]; then
  npm ci --no-audit --no-fund
else
  npm install --no-audit --no-fund
fi

npm run build
echo "web/dist 已生成：$(pwd)/dist"
