#!/bin/bash
# Count core agent lines (excluding channels/, cli/, providers/ adapters)
cd "$(dirname "$0")" || exit 1

PKG="packages/gateway/yeoman_gateway"

echo "yeoman core agent line count"
echo "================================"
echo ""

for dir in agent agent/tools bus core cron heartbeat session; do
  count=$(find "$PKG/$dir" -maxdepth 1 -name "*.py" -exec cat {} + 2>/dev/null | wc -l)
  printf "  %-16s %5s lines\n" "$dir/" "$count"
done

root=$(cat "$PKG/__init__.py" "$PKG/__main__.py" 2>/dev/null | wc -l)
printf "  %-16s %5s lines\n" "(root)" "$root"

echo ""
total=$(find "$PKG" -name "*.py" ! -path "*/channels/*" ! -path "*/cli/*" ! -path "*/providers/*" | xargs cat | wc -l)
echo "  Core total:     $total lines"
echo ""
echo "  (excludes: channels/, cli/, providers/)"
echo "  (config/ and utils/ live in packages/shared/yeoman_shared)"
