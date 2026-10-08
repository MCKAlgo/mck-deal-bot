#!/usr/bin/env bash
# Supervisor: keeps DealBot alive 24/7, auto-restarts on crash.
cd "$(dirname "$0")"
mkdir -p logs data
while true; do
  if ! pgrep -f "python3 bot.py" > /dev/null; then
    echo "$(date '+%F %T') supervisor: starting bot..." >> logs/supervisor.log
    python3 bot.py >> logs/daemon.log 2>&1 &
  fi
  sleep 30
done
