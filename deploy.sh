#!/bin/bash
# Deploy LocalVidGen to tigerclaw (MacBook Air, Tailscale 100.114.171.88) and (re)start its launchd service.
# Usage: ./deploy.sh            # sync + restart
#        ./deploy.sh --logs     # sync + restart + tail server logs
set -euo pipefail

HOST="tigerclaw@100.114.171.88"
REMOTE_DIR="/Users/tigerclaw/.openclaw/workspace/localvidgen"
LABEL="com.tigerclaw.localvidgen"
: "${TIGERCLAW_PASS:?set TIGERCLAW_PASS to the tigerclaw ssh password (never commit it)}"
export SSHPASS="$TIGERCLAW_PASS"
SSH_OPTS=(-o PubkeyAuthentication=no -o PreferredAuthentications=password -o ConnectTimeout=10)
ssh_() { sshpass -e ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

cd "$(dirname "$0")"
ssh_ "mkdir -p $REMOTE_DIR/static $REMOTE_DIR/data $REMOTE_DIR/launchd"
for f in server.py workflows.py whitepc.py config.json deploy.sh; do
  sshpass -e scp "${SSH_OPTS[@]}" -q "$f" "$HOST:$REMOTE_DIR/$f"
done
sshpass -e scp "${SSH_OPTS[@]}" -q static/index.html "$HOST:$REMOTE_DIR/static/index.html"
sshpass -e scp "${SSH_OPTS[@]}" -q launchd/$LABEL.plist "$HOST:$REMOTE_DIR/launchd/$LABEL.plist"

ssh_ "cp $REMOTE_DIR/launchd/$LABEL.plist ~/Library/LaunchAgents/$LABEL.plist \
   && cp $REMOTE_DIR/launchd/$LABEL.plist ~/.openclaw/workspace/launchd/$LABEL.plist \
   && launchctl bootout gui/\$(id -u)/$LABEL 2>/dev/null || true; \
   launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$LABEL.plist \
   && sleep 2 && launchctl print gui/\$(id -u)/$LABEL | grep -E 'state|pid' | head -3 \
   && curl -s -m5 http://localhost:5030/api/status | head -c 300; echo"

if [[ "${1:-}" == "--logs" ]]; then
  ssh_ "tail -n 40 -f $REMOTE_DIR/server.out.log $REMOTE_DIR/server.err.log"
fi
