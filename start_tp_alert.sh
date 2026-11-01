#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

for pid in $(pgrep -f "TradingPipeline/tp_alert.py" || true); do
  kill "$pid" 2>/dev/null || true
done
sleep 1

setsid python3 tp_alert.py \
  --symbol  SOLUSDT \
  --entry   11745 \
  --sl      11876 \
  --tp1     11355 \
  --tp2     11183 \
  --margin  100 \
  --leverage 7 \
  --confirm 2 \
  --log     tp_alert.log \
  > tp_alert.out 2>&1 < /dev/null &

echo "tp_alert restarted (pid $!) -> logs: tp_alert.out / tp_alert.log"