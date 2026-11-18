#!/usr/bin/env bash
# JAFAR Intelligence Pipeline — full run, timestamped in the host timezone.
# Usage: run_pipeline_eat.sh [--no-brief]
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$REPO/pipeline_runs_eat.log"
export TZ="${TZ:-UTC}"
# Pin the interpreter: mise `latest` moved to 3.15.2 (no deps installed).
PY_HOME="${JAFAR_PY_HOME:-$HOME/.local/share/mise/installs/python/3.14.7}"
[[ -x "$PY_HOME/bin/python3" ]] || PY_HOME="$HOME/.local/share/mise/installs/python/latest"
export PATH="$PY_HOME/bin:$PATH"

cd "$REPO" || exit 1

stamp() { date '+%Y-%m-%d %H:%M:%S %Z (%z)'; }

{
  echo "============================================================"
  echo "[$(stamp)] PIPELINE RUN START"
} >>"$LOG"

echo "-- data collection (build_snapshot.py) --"
python3 build_snapshot.py >>"$LOG" 2>&1
rc_snap=$?
echo "[$(stamp)] build_snapshot.py exit=$rc_snap" >>"$LOG"

if [[ "${1:-}" != "--no-brief" ]]; then
  echo "-- HTML brief (generate brief.py) --"
  python3 "generate brief.py" >>"$LOG" 2>&1
  echo "[$(stamp)] generate brief.py exit=$?" >>"$LOG"
fi

echo "-- signal scan (signals.py) --"
python3 signals.py >>"$LOG" 2>&1
echo "[$(stamp)] signals.py exit=$?" >>"$LOG"

echo "[$(stamp)] PIPELINE RUN END" >>"$LOG"
echo "============================================================" >>"$LOG"

exit $rc_snap
