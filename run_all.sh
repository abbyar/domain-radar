#!/bin/bash
# Run all collectors sequentially. Suitable for invocation via cron.
# Example crontab (hourly scan):
#   0 * * * * cd /path/to/domain-radar && ./run_all.sh >> scan.log 2>&1
set -e
cd "$(dirname "$0")"

if [ -f "venv/bin/python" ]; then
    PYTHON_CMD="venv/bin/python"
elif [ -f "venv/Scripts/python.exe" ]; then
    PYTHON_CMD="venv/Scripts/python.exe"
else
    PYTHON_CMD="python3"
fi

$PYTHON_CMD run_all.py "$@"
