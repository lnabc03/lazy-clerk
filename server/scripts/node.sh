#!/usr/bin/env bash
# mihomo 节点管理（公网版代理逃生通道）：
#   ./node.sh list          列出 hospital 组节点、当前出口与实测延迟
#   ./node.sh use <节点名>  手动指定节点（健康检查仍在跑，节点挂了自动转移）
#   ./node.sh direct        恢复直连优先（默认策略）
# 一般无需手动干预——fallback 组直连被封会自动切最快节点，恢复自动切回。
set -euo pipefail
cd "$(dirname "$0")/.."

API="http://127.0.0.1:9090"
GROUP="hospital"
SECRET="$(grep '^MIHOMO_SECRET=' .env | cut -d= -f2-)"

api() { curl -sf -H "Authorization: Bearer $SECRET" "$@" ; }

case "${1:-list}" in
  list)
    api "$API/proxies/$GROUP" | python3 -c "
import json, sys
g = json.load(sys.stdin)
print('当前出口:', g['now'])
print('候选节点:')
for name in g['all']:
    print('  -', name)
"
    ;;
  use)
    [ -n "${2:-}" ] || { echo "用法: $0 use <节点名>"; exit 1; }
    curl -sf -X PUT -H "Authorization: Bearer $SECRET" \
         -H 'Content-Type: application/json' \
         -d "{\"name\": \"$2\"}" "$API/proxies/$GROUP"
    echo "已切换到: $2"
    ;;
  direct)
    curl -sf -X PUT -H "Authorization: Bearer $SECRET" \
         -H 'Content-Type: application/json' \
         -d '{"name": "DIRECT"}' "$API/proxies/$GROUP"
    echo "已恢复直连优先"
    ;;
  *)
    echo "用法: $0 [list | use <节点名> | direct]"; exit 1
    ;;
esac
