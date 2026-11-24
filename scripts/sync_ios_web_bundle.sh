#!/usr/bin/env bash
# Syncs the production MindWeave web/ frontend into ios/MindWeave/WebBundle/
# while stripping test suites, screenshots, QA video reels, and local dev metadata,
# and enforcing V2 bundle integrity (required modules, retired legacy ban, JS syntax,
# <=500-line ceiling, and zero .innerHTML assignment).
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
    --exclude '*.mp4' \
    --exclude '*.webm' \
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
EXCLUDED_GLOBS = ("*.mp4", "*.webm", "*.test.mjs", "*.test.js", "*.spec.mjs", "*.spec.js")

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

# 1. Verify all required V2 bundle files & directories exist.
REQUIRED_V2_ARTIFACTS=(
  "index.html"
  "sw.js"
  "manifest.webmanifest"
  "styles/tokens.css"
  "styles/base.css"
  "styles/journal.css"
  "styles/dock.css"
  "styles/chat.css"
  "styles/sheets.css"
  "scripts/main.js"
  "scripts/native-bridge.js"
  "scripts/voice-fsm.js"
  "scripts/lib/orb.js"
  "scripts/lib/virtual-river.js"
  "scripts/lib/swipe-arbiter.js"
  "scripts/lib/journal-pinch.js"
  "scripts/lib/solar-terms.js"
  "scripts/lib/spring-waapi.js"
  "scripts/lib/ime-guard.js"
  "scripts/views/archive-history-sheet.js"
  "scripts/views/people-sheet.js"
  "scripts/views/peek-date.js"
  "scripts/views/vellum-pocket.js"
  "scripts/views/capture-receipt.js"
  "scripts/views/chat-drawer.js"
  "scripts/views/data-rights-sheet.js"
  "scripts/views/crisis.js"
  "assets"
)

MISSING_COUNT=0
for required in "${REQUIRED_V2_ARTIFACTS[@]}"; do
  if [[ ! -e "${DEST_DIR}/${required}" ]]; then
    echo "error: missing required V2 bundled artifact: ${DEST_DIR}/${required}" >&2
    MISSING_COUNT=$((MISSING_COUNT + 1))
  fi
done
if [[ "${MISSING_COUNT}" -gt 0 ]]; then
  echo "error: ${MISSING_COUNT} required V2 artifact(s) missing from ${DEST_DIR}" >&2
  exit 1
fi

# 2. Enforce that retired V1 legacy files never sneak back into WebBundle.
RETIRED_LEGACY_FILES=(
  "scripts/gestures.js"
  "scripts/views/casual.js"
  "scripts/views/questions.js"
)
for retired in "${RETIRED_LEGACY_FILES[@]}"; do
  if [[ -e "${DEST_DIR}/${retired}" ]]; then
    echo "error: retired legacy file must not exist in ${DEST_DIR}: ${retired}" >&2
    exit 1
  fi
done

# 3. Verify zero test directories, screenshots, or QA video reels leaked.
if [[ -d "${DEST_DIR}/tests" || -d "${DEST_DIR}/screenshots" ]]; then
  echo "error: test or screenshot directories leaked into ${DEST_DIR}" >&2
  exit 1
fi
LEAKED_VIDEOS="$(find "${DEST_DIR}" -type f \( -name '*.mp4' -o -name '*.webm' \))"
if [[ -n "${LEAKED_VIDEOS}" ]]; then
  echo "error: QA video files leaked into ${DEST_DIR}:" >&2
  echo "${LEAKED_VIDEOS}" >&2
  exit 1
fi

# 4. Syntax-check every bundled JS file with Node.js (if available on PATH).
if command -v node >/dev/null 2>&1; then
  while IFS= read -r -d '' js_file; do
    if ! node --check "${js_file}"; then
      echo "error: JS syntax check failed: ${js_file}" >&2
      exit 1
    fi
  done < <(find "${DEST_DIR}/scripts" -type f -name '*.js' -print0)
fi

# 5. Enforce <= 500-line ceiling and zero `.innerHTML =` assignments across all bundled JS files.
MAX_JS_LINES=500
VIOLATIONS=0
while IFS= read -r -d '' js_file; do
  line_count="$(wc -l < "${js_file}" | tr -d ' ')"
  if [[ "${line_count}" -gt "${MAX_JS_LINES}" ]]; then
    echo "error: bundled JS exceeds ${MAX_JS_LINES}-line limit (${line_count} lines): ${js_file}" >&2
    VIOLATIONS=$((VIOLATIONS + 1))
  fi
  inner_html_hits="$(grep -nE '\.innerHTML[[:space:]]*=' "${js_file}" | grep -v '// static-svg' || true)"
  if [[ -n "${inner_html_hits}" ]]; then
    echo "${inner_html_hits}" >&2
    echo "error: forbidden .innerHTML assignment detected in ${js_file}" >&2
    VIOLATIONS=$((VIOLATIONS + 1))
  fi
done < <(find "${DEST_DIR}/scripts" -type f -name '*.js' -print0)

if [[ "${VIOLATIONS}" -gt 0 ]]; then
  echo "error: ${VIOLATIONS} JS hygiene violation(s) detected in ${DEST_DIR}/scripts" >&2
  exit 1
fi

FILE_COUNT="$(find "${DEST_DIR}" -type f | wc -l | tr -d ' ')"
BUNDLE_SIZE="$(du -sh "${DEST_DIR}" | awk '{print $1}')"
echo "Synced ${FILE_COUNT} files (${BUNDLE_SIZE}) from web/ -> ios/MindWeave/WebBundle/ (V2 integrity verified)"
