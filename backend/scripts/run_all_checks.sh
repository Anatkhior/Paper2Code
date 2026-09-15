#!/usr/bin/env bash
# 一键复跑全部里程碑验收。任何一项不过就以非零码退出。
#
# 注意：m3_check 与 m6_check 会跑 `pnpm build`，和 `next dev` 共用 .next 目录，
# 所以跑之前请先停掉前端 dev server（./scripts/dev_services.sh stop 或只停前端）。
set -u
cd "$(dirname "$0")/.." || exit 1

# 服务端口：验收脚本用 8231/8232，和手动调试用的 8123/8000 互不干扰
total=0
failed=0
for milestone in m0 m1 m2 m3 m4 m6 m7; do
  echo "=== ${milestone}_check ==="
  output=$(.venv/bin/python -u -m "scripts.${milestone}_check" 2>&1)
  code=$?
  passed=$(printf '%s\n' "$output" | grep -c '^✅')
  total=$((total + passed))
  if [ $code -eq 0 ]; then
    echo "  ✅ ${passed} 项通过"
  else
    failed=$((failed + 1))
    echo "  ❌ 未通过（退出码 ${code}）"
    printf '%s\n' "$output" | grep '^❌' | head -5
  fi
done

echo
if [ $failed -eq 0 ]; then
  echo "✅ 全部里程碑通过，共 ${total} 项断言"
else
  echo "❌ ${failed} 个里程碑未通过（共 ${total} 项通过）"
fi
exit $failed
