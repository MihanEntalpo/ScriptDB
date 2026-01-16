#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $(basename "$0")" >&2
  exit 2
}

[[ $# -eq 0 ]] || usage

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh CLI is required." >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Error: working tree has uncommitted changes." >&2
  exit 1
fi

VERSION=$(python - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(data["project"]["version"])
PY
)

if [[ -z "$VERSION" ]]; then
  echo "Error: could not determine version from pyproject.toml." >&2
  exit 1
fi

TAG="v$VERSION"

if git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
  if gh release view "$TAG" >/dev/null 2>&1; then
    echo "Error: tag $TAG already exists and release is already published." >&2
    exit 1
  fi

  gh release create "$TAG" --title "$TAG" --generate-notes
  exit 0
fi

git tag -a "$TAG" -m "Release $TAG"
git push origin "$TAG"

gh release create "$TAG" --title "$TAG" --generate-notes
