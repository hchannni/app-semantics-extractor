#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_KOTLIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCALA_DIR="$(cd "$FRONTEND_KOTLIN_DIR/.." && pwd)"
LEGACY_DIR="$SCALA_DIR/legacy_joern"

JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
VIEW_ANCHORS="${VIEW_ANCHORS:-$LEGACY_DIR/view-anchors.json}"
OUTPUT="${OUTPUT:-$SCALA_DIR/runs/target_home_assignment_decl_bridge/assignment-declarations.json}"

echo "Running target-home assignment declaration bridge..."
echo "  Joern: $JOERN_BIN"
echo "  CPG: $CPG_PATH"
echo "  View Anchors: $VIEW_ANCHORS"
echo "  Output: $OUTPUT"
echo ""

if [ -n "$JAVA_HOME" ]; then
  export PATH="$JAVA_HOME/bin:$PATH"
fi

if [ ! -x "$JOERN_BIN" ]; then
  echo "[ERROR] Joern executable not found: $JOERN_BIN"
  exit 1
fi

if [ ! -e "$CPG_PATH" ]; then
  echo "[ERROR] CPG not found: $CPG_PATH"
  exit 1
fi

if [ ! -e "$VIEW_ANCHORS" ]; then
  echo "[ERROR] View anchors file not found: $VIEW_ANCHORS"
  exit 1
fi

"$JOERN_BIN" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchorContract.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/decl_ref_resolver/AssignmentDeclRefInput.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/decl_ref_resolver/AssignmentDeclRefCore.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/decl_ref_resolver/AssignmentDeclRefExpansion.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/decl_ref_resolver/AssignmentDeclRefReport.sc" \
  --script "$FRONTEND_KOTLIN_DIR/anchor_usages/AssignmentDeclAndRefResolver.sc" \
  --param cpgPath="$CPG_PATH" \
  --param viewAnchorsPath="$VIEW_ANCHORS" \
  --param outputPath="$OUTPUT"

echo ""
echo "[+] Done. Check $OUTPUT"
