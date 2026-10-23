#!/bin/bash
# Run all e2e scenarios (own uvicorn per scenario; never touches ~/aha_data). Extra args pass through, e.g. --only 3,5
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python e2e/phone_sim.py "$@"
