#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ ! -x .venv/bin/python ]; then
  echo "First run: installing Walter. This takes a few minutes..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
fi
exec .venv/bin/python -m heisenbot ui
