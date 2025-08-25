#!/usr/bin/env bash
set -euo pipefail

VENV="./venv"
if [ ! -d "$VENV" ]; then
    python -m venv "$VENV"
fi
source "$VENV/bin/activate"

python -m pip install --upgrade build twine

rm -rf dist/
python -m build
python -m twine check dist/*
python -m twine upload dist/*

