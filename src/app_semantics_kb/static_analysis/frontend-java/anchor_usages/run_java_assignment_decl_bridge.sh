#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_JAVA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCALA_DIR="$(cd "$FRONTEND_JAVA_DIR/.." && pwd)"
RUNS_DIR="$SCALA_DIR/runs"

source "$FRONTEND_JAVA_DIR/common/java_output_guard.sh"

JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
VIEW_ANCHORS="${VIEW_ANCHORS:-$RUNS_DIR/java_view_anchors_bridge/view-anchors.json}"
OUTPUT="${OUTPUT:-$RUNS_DIR/java_assignment_decl_bridge/assignment-declarations.json}"

if [[ ! -x "$JOERN_BIN" ]]; then
  echo "[ERROR] joern executable not found: $JOERN_BIN" >&2
  exit 1
fi

if [[ ! -f "$CPG_PATH" ]]; then
  echo "[ERROR] cpg file not found: $CPG_PATH" >&2
  exit 1
fi

if [[ ! -f "$VIEW_ANCHORS" ]]; then
  echo "[ERROR] Java view anchors file not found: $VIEW_ANCHORS" >&2
  exit 1
fi

java_frontend_require_output_under_runs "$OUTPUT" "OUTPUT" "$RUNS_DIR"

echo "[*] Running Java assignment declaration bridge"
echo "    JOERN_BIN=$JOERN_BIN"
echo "    CPG_PATH=$CPG_PATH"
echo "    VIEW_ANCHORS=$VIEW_ANCHORS"
echo "    OUTPUT=$OUTPUT"

"$JOERN_BIN" \
  --import "$FRONTEND_JAVA_DIR/common/JavaOutputPathGuard.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewAnchorModel.sc" \
  --import "$FRONTEND_JAVA_DIR/anchor_usages/decl_ref_resolver/JavaAssignmentDeclRefInput.sc" \
  --import "$FRONTEND_JAVA_DIR/anchor_usages/decl_ref_resolver/JavaAssignmentDeclRefCore.sc" \
  --import "$FRONTEND_JAVA_DIR/anchor_usages/decl_ref_resolver/JavaAssignmentDeclRefExpansion.sc" \
  --import "$FRONTEND_JAVA_DIR/anchor_usages/decl_ref_resolver/JavaAssignmentDeclRefReport.sc" \
  --script "$FRONTEND_JAVA_DIR/anchor_usages/JavaAssignmentDeclAndRefResolver.sc" \
  --param cpgPath="$CPG_PATH" \
  --param viewAnchorsPath="$VIEW_ANCHORS" \
  --param outputPath="$OUTPUT" \
  --param runsDir="$RUNS_DIR"

echo "[+] Done: $OUTPUT"
