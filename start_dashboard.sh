#!/usr/bin/env bash
# Launch the JAFAR dashboard server and open it in your browser.
set -euo pipefail
cd "$(dirname "$0")"

# Kill anything already bound to :5000 so we start clean
if lsof -ti tcp:5000 >/dev/null 2>&1; then
  lsof -ti tcp:5000 | xargs -r kill
  sleep 1
fi

nohup python3 dashboard.py > dashboard.log 2>&1 &
echo "Dashboard starting (pid $!) -> http://localhost:5000"

# Wait until it answers
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null http://localhost:5000/; then
    echo "Dashboard is up."
    break
  fi
  sleep 1
done

exec brave --new-window "http://localhost:5000"