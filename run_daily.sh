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

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

log "=== daily cycle start (mode=$MODE) ==="

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
