#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade build twine

rm -rf dist/
python -m build
twine check dist/*
twine upload dist/*

