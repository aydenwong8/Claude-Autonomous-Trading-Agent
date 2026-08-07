#!/bin/bash
#
# Daily post-close run: review the portfolio, trade if warranted, refresh the dashboard.
# Invoked by launchd (com.aydenwong.tradingagent.plist) on weekdays at 16:30 ET.
#
# launchd gives us a bare environment and cwd=/, so everything here is absolute
# and we cd into the project first — agent.py's load_dotenv() reads ./.env.

set -uo pipefail

# Every timestamp this project shows a human is market time; launchd's bare
# environment has no TZ, so pin it rather than inherit whatever the host has.
export TZ="America/New_York"

PROJECT_DIR="/Users/aydenwong/Projects/Claude-Autonomous-Trading-Agent"
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
MODE="paper"

# If the machine was asleep at 16:30, launchd fires the job on wake — and Wi-Fi
# is not up yet, so every host fails DNS and the whole cycle dies in four
# seconds (this happened 2026-08-06). Wait for the network before starting
# rather than burning the day's cycle on a resolver that is seconds from ready.
NET_WAIT_SECS=900
NET_POLL_SECS=15

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

log "=== daily cycle start (mode=$MODE) ==="

if [ "$MODE" = "live" ]; then
    BROKER_HOST="api.alpaca.markets"
else
    BROKER_HOST="paper-api.alpaca.markets"
fi

# Every host the cycle needs. Checked with the same resolver and TLS stack the
# clients use, so "ready" here means ready for them too.
net_ready() {
    "$PYTHON" -c '
import socket, sys
for host in sys.argv[1:]:
    try:
        socket.create_connection((host, 443), timeout=5).close()
    except OSError:
        sys.exit(1)
' "$BROKER_HOST" data.alpaca.markets api.anthropic.com 2>/dev/null
}

# Record a run that never got as far as agent.py. Without this the DB cannot
# tell a network-stalled day from a day the agent chose not to trade.
record_no_network() {
    RUN_ERROR="$1" "$PYTHON" -c '
import os
from dotenv import load_dotenv
load_dotenv()
import agent, config
conn = agent.init_db(config.DEFAULT_DB_PATH)
agent.log_run(conn, "failed", os.environ["RUN_ERROR"], 0)
conn.close()
' || log "could not write run_log row for the skipped run"
}

if ! net_ready; then
    log "network not reachable — waiting up to $((NET_WAIT_SECS / 60))m for it"
    DEADLINE=$(( $(date +%s) + NET_WAIT_SECS ))
    while true; do
        if [ "$(date +%s)" -ge "$DEADLINE" ]; then
            log "network still unreachable after $((NET_WAIT_SECS / 60))m — no cycle today"
            record_no_network "run_daily.sh: no network after ${NET_WAIT_SECS}s waiting for $BROKER_HOST, data.alpaca.markets, api.anthropic.com — cycle never started"
            log "=== done (agent=1 dashboard=skipped) ==="
            exit 1
        fi
        sleep "$NET_POLL_SECS"
        if net_ready; then
            log "network up — proceeding"
            break
        fi
    done
fi

# Skip market holidays. launchd's Weekday filter handles weekends, but not
# Thanksgiving — without this we'd log a bogus cycle against stale prices.
if ! "$PYTHON" -c "
import sys
from datetime import date
from dotenv import load_dotenv
load_dotenv()
import agent
try:
    cal = agent._trading_client('$MODE').get_calendar()
    today = date.today().isoformat()
    sys.exit(0 if any(str(d.date) == today for d in cal) else 1)
except SystemExit:
    raise
except Exception as e:
    print(f'calendar check failed ({e}) — proceeding anyway', file=sys.stderr)
    sys.exit(0)
"; then
    log "market closed today — skipping cycle"
    exit 0
fi

log "--- decision cycle ---"
"$PYTHON" agent.py --mode "$MODE"
AGENT_RC=$?
if [ $AGENT_RC -ne 0 ]; then
    log "agent.py FAILED (exit $AGENT_RC)"
fi

# Refresh the dashboard regardless — equity moved even on a no-trade day.
log "--- dashboard ---"
"$PYTHON" dashboard.py --mode "$MODE"
DASH_RC=$?
if [ $DASH_RC -ne 0 ]; then
    log "dashboard.py FAILED (exit $DASH_RC)"
fi

log "=== done (agent=$AGENT_RC dashboard=$DASH_RC) ==="
exit $(( AGENT_RC || DASH_RC ))
