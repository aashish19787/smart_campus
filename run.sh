#!/usr/bin/env bash
# One-command start: creates a virtualenv, installs dependencies and runs the server.
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt waitress
python run.py
