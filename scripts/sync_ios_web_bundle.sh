#!/usr/bin/env bash
# Syncs the production MindWeave web/ frontend into ios/MindWeave/WebBundle/
# while stripping test suites, screenshots, and local dev metadata.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_DIR="${REPO_ROOT}/web"
DEST_DIR="${REPO_ROOT}/ios/MindWeave/WebBundle"

if [[ ! -d "${SRC_DIR}" || ! -f "${SRC_DIR}/index.html" ]]; then
  echo "error: expected web frontend at ${SRC_DIR}/index.html" >&2
  exit 1
fi

mkdir -p "${DEST_DIR}"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude 'tests/' \
    --exclude 'tests' \
    --exclude 'screenshots/' \
    --exclude 'screenshots' \
    --exclude '*.test.mjs' \
    --exclude '*.test.js' \
    --exclude '*.spec.mjs' \
    --exclude '*.spec.js' \
    --exclude 'node_modules/' \
    --exclude 'package.json' \
    --exclude 'package-lock.json' \
    --exclude '.gitignore' \
    --exclude '.DS_Store' \
    "${SRC_DIR}/" "${DEST_DIR}/"
else
  # Portable fallback when rsync is unavailable in minimal Linux containers.
  rm -rf "${DEST_DIR}"
  mkdir -p "${DEST_DIR}"
  python3 - "${SRC_DIR}" "${DEST_DIR}" <<'PY'
import fnmatch
import os
import shutil
import sys

src_root, dest_root = sys.argv[1], sys.argv[2]
EXCLUDED_DIRS = {"tests", "screenshots", "node_modules"}
EXCLUDED_FILES = {"package.json", "package-lock.json", ".gitignore", ".DS_Store"}
EXCLUDED_GLOBS = ("*.test.mjs", "*.test.js", "*.spec.mjs", "*.spec.js")

for dirpath, dirnames, filenames in os.walk(src_root):
    dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
    rel = os.path.relpath(dirpath, src_root)
    target_dir = dest_root if rel == "." else os.path.join(dest_root, rel)
    os.makedirs(target_dir, exist_ok=True)
    for name in filenames:
        if name in EXCLUDED_FILES or any(fnmatch.fnmatch(name, pat) for pat in EXCLUDED_GLOBS):
            continue
        shutil.copy2(os.path.join(dirpath, name), os.path.join(target_dir, name))
PY
fi

# Sanity-check essential bundle structure and ensure no test artifacts leaked.
for required in "index.html" "scripts/main.js" "styles/base.css" "assets"; do
  if [[ ! -e "${DEST_DIR}/${required}" ]]; then
    echo "error: missing expected bundled artifact: ${DEST_DIR}/${required}" >&2
    exit 1
  fi
done

if [[ -d "${DEST_DIR}/tests" || -d "${DEST_DIR}/screenshots" ]]; then
  echo "error: test or screenshot directories leaked into ${DEST_DIR}" >&2
  exit 1
fi

FILE_COUNT="$(find "${DEST_DIR}" -type f | wc -l | tr -d ' ')"
BUNDLE_SIZE="$(du -sh "${DEST_DIR}" | awk '{print $1}')"
echo "Synced ${FILE_COUNT} files (${BUNDLE_SIZE}) from web/ -> ios/MindWeave/WebBundle/"
