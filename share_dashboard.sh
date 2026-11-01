#!/usr/bin/env bash
# Expose the local dashboard to the internet via a free Cloudflare quick tunnel,
# prints the public URL and opens it in Brave.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "ERROR: cloudflared not found." >&2
  exit 1
fi

# Make sure the dashboard is actually up
if ! curl -sf -o /dev/null http://localhost:5000/; then
  echo "Dashboard not running. Starting it first..."
  bash start_dashboard.sh
fi

# Kill any prior quick tunnel so we get a clean, current URL
pkill -f 'cloudflared tunnel --url' >/dev/null 2>&1 || true
sleep 1

nohup cloudflared tunnel --url http://localhost:5000 > cloudflared.log 2>&1 &
tunnel_pid=$!
echo "Tunnel process started (pid $tunnel_pid) — grabbing URL..."

# Try to extract the public URL from the log
URL=""
for _ in $(seq 1 45); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' cloudflared.log | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "ERROR: couldn't obtain tunnel URL. See cloudflared.log:" >&2
  tail -30 cloudflared.log >&2
  exit 1
fi

# DNS for the fresh tunnel hostname takes a few seconds to propagate;
# wait until the public URL actually answers before declaring success
READY=""
for _ in $(seq 1 45); do
  if curl -sf -o /dev/null --max-time 10 "$URL/"; then
    READY=1
    break
  fi
  sleep 2
done

echo "=================================================================="
echo "  Public link (share with trading partner):"
echo "    $URL"
echo "=================================================================="
echo "  Log: cloudflared.log · kill with: pkill -f 'cloudflared tunnel'"

# Write the URL to a stable file the partner/AI can read next time
echo "$URL" > public_link.txt

if [ -n "$READY" ]; then
  echo "  Tunnel is live (HTTP 200)."
  exec brave --new-window "$URL"
else
  echo "  WARNING: could not confirm tunnel is live yet — opening Brave anyway."
  exec brave --new-window "$URL"
fi