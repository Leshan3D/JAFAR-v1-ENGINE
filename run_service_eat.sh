#!/usr/bin/env bash
# JAFAR Trading Pipeline — engine service wrapper for systemd --user.
# Runs run_service.py (full pipeline hourly + 15-min price refresh + dashboard +
# Telegram alerts/bot), recording start/exit stamps in the host timezone.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$REPO/pipeline_runs_eat.log"
export TZ="${TZ:-UTC}"
# Pin the interpreter. mise's `latest` symlink moved to Python 3.15.2, which
# does NOT have the engine's packages (requests/flask/pandas/yfinance/torch).
# 3.14.7 is the interpreter every dependency is actually installed into.
PY_HOME="${JAFAR_PY_HOME:-$HOME/.local/share/mise/installs/python/3.14.7}"
[[ -x "$PY_HOME/bin/python3" ]] || PY_HOME="$HOME/.local/share/mise/installs/python/latest"
export PATH="$PY_HOME/bin:$PATH"
export PYTHONUNBUFFERED=1

stamp() { date '+%Y-%m-%d %H:%M:%S %Z (%z)'; }

# The repo lives on an external NTFS disk; wait for the mount if boot races us.
for _ in $(seq 1 60); do
  [[ -d "$REPO" ]] && break
  sleep 5
done
if [[ ! -d "$REPO" ]]; then
  echo "[$(stamp)] repo $REPO unavailable — aborting" >>"$LOG"
  exit 1
fi

cd "$REPO" || exit 1

{
  echo "============================================================"
  echo "[$(stamp)] SERVICE START — run_service.py (pipeline + dashboard + alerts)"
} >>"$LOG"

python3 -u run_service.py >>"$REPO/service.log" 2>&1
rc=$?

echo "[$(stamp)] SERVICE EXIT rc=$rc" >>"$LOG"
echo "============================================================" >>"$LOG"
exit $rc
