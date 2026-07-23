#!/usr/bin/env bash
# Simulate an agent crash mid-call (TRD §8.2 step 1).
#
# SIGKILL the agent worker and its whole process tree, then block until it's
# actually gone. Blocking matters: it guarantees no stale process overlaps the
# restart, which is exactly the split-brain the recovery demo must avoid.
#
# Usage:  scripts/simulate_crash.sh
# Then restart the worker (`python -m agent.main dev`) and watch it resume the
# call from its persisted state.
set -uo pipefail

kill_tree() {
  local p=$1
  for c in $(pgrep -P "$p" 2>/dev/null); do kill_tree "$c"; done
  kill -9 "$p" 2>/dev/null
}

pids=$(pgrep -f "[a]gent.main")
if [ -z "$pids" ]; then
  echo "no agent worker running — nothing to crash"
  exit 1
fi

echo "SIGKILL agent worker tree: $pids"
for p in $pids; do kill_tree "$p"; done

for _ in $(seq 1 50); do
  if ! pgrep -f "[a]gent.main" >/dev/null; then
    echo "agent process gone — safe to restart"
    exit 0
  fi
  sleep 0.1
done

echo "warning: agent process still present after wait"
exit 1
