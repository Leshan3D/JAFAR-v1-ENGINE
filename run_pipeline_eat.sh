#!/usr/bin/env bash
# JAFAR Intelligence Pipeline — full run, timestamped in Local EAT.
# Usage: run_pipeline_eat.sh [--no-brief]
set -uo pipefail

REPO="/home/jafar/TradingPipeline"
LOG="$REPO/pipeline_runs_eat.log"
export TZ="UTC"
export PATH="/home/jafar/.local/share/mise/installs/python/latest/bin:$PATH"

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
