#!/usr/bin/env bash
# Start the LIMS Print Hub. Works on macOS / Linux AND on Windows via Git Bash
# (the real Windows deployment normally uses run.bat). Uses uv to provision the venv.
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

uv venv .venv
uv pip install -r requirements.txt --python .venv
echo
# The interpreter lives under Scripts/ on Windows, bin/ on macOS/Linux.
if [ -x .venv/Scripts/python.exe ]; then
  .venv/Scripts/python.exe app.py
else
  .venv/bin/python app.py
fi
