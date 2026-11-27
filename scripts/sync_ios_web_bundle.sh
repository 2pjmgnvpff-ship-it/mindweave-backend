#!/usr/bin/env bash
set -euo pipefail

# Single-Source-of-Truth sync: src/mindweave/webapp/ -> ios/MindWeave/WebBundle/
# Copies the live MindWeave v16 Lumina webapp into the Xcode iOS bundle folder and
# validates bundle integrity and JS syntax before XcodeGen / xcodebuild runs.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${REPO_ROOT}/src/mindweave/webapp"
DEST_DIR="${REPO_ROOT}/ios/MindWeave/WebBundle"

if [[ ! -d "${SRC_DIR}" ]]; then
  echo "error: webapp source directory not found: ${SRC_DIR}" >&2
  exit 1
fi

rm -rf "${DEST_DIR}"
mkdir -p "${DEST_DIR}"

rsync -a --delete \
  --exclude '.DS_Store' \
  --exclude 'tests/' \
  --exclude 'screenshots/' \
  --exclude '*.mp4' \
  --exclude '*.webm' \
  "${SRC_DIR}/" "${DEST_DIR}/"

REQUIRED_ARTIFACTS=(
  "index.html"
  "styles/app.css"
  "scripts/app.mjs"
  "assets/cupid/arrow_pin_gold.webp"
  "assets/cupid/arrow_pin_silver.webp"
  "assets/cupid/arrow_pin_crystal.webp"
  "assets/v16/seed_bundle.json"
  "assets/v16/pt_mom_garden.webp"
)

MISSING_COUNT=0
for required in "${REQUIRED_ARTIFACTS[@]}"; do
  if [[ ! -e "${DEST_DIR}/${required}" ]]; then
    echo "error: missing required v16 bundled artifact: ${DEST_DIR}/${required}" >&2
    MISSING_COUNT=$((MISSING_COUNT + 1))
  fi
done
if [[ "${MISSING_COUNT}" -gt 0 ]]; then
  echo "error: ${MISSING_COUNT} required v16 artifact(s) missing from ${DEST_DIR}" >&2
  exit 1
fi

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

if command -v node >/dev/null 2>&1; then
  while IFS= read -r -d '' js_file; do
    if ! node --check "${js_file}"; then
      echo "error: JS syntax check failed: ${js_file}" >&2
      exit 1
    fi
  done < <(find "${DEST_DIR}/scripts" -type f \( -name '*.js' -o -name '*.mjs' \) -print0)
fi

FILE_COUNT="$(find "${DEST_DIR}" -type f | wc -l | tr -d ' ')"
BUNDLE_SIZE="$(du -sh "${DEST_DIR}" | awk '{print $1}')"
echo "Synced ${FILE_COUNT} files (${BUNDLE_SIZE}) from src/mindweave/webapp/ -> ios/MindWeave/WebBundle/ (v16 Lumina integrity verified)"
