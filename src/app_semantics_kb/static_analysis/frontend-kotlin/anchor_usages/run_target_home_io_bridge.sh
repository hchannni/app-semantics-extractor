#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_KOTLIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCALA_DIR="$(cd "$FRONTEND_KOTLIN_DIR/.." && pwd)"
LEGACY_DIR="$SCALA_DIR/legacy_joern"

JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
VIEW_ANCHORS="${VIEW_ANCHORS:-$LEGACY_DIR/view-anchors.json}"
ASSIGNMENT_DECL="${ASSIGNMENT_DECL:-$LEGACY_DIR/assignment-declarations.json}"
OUTPUT="${OUTPUT:-$SCALA_DIR/runs/target_home_anchor_usages_io_bridge/anchor-usages.json}"

echo "Running target-home anchor_usages io bridge..."
echo "  Joern: $JOERN_BIN"
echo "  CPG: $CPG_PATH"
echo "  View Anchors: $VIEW_ANCHORS"
echo "  Assignment Declarations: $ASSIGNMENT_DECL"
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

if [ ! -e "$ASSIGNMENT_DECL" ]; then
  echo "[ERROR] Assignment declarations file not found: $ASSIGNMENT_DECL"
  exit 1
fi

"$JOERN_BIN" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchorContract.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/io/AnchorUsagesModel.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/io/AnchorUsagesInput.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/io/AnchorUsagesJson.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/AnchorUsagesUiSignals.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/AnchorUsagesKotlinScope.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/cases/AnchorUsagesCaseSupport.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/cases/AnchorUsagesClosureCaptureCase.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/cases/AnchorUsagesChainingCase.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/cases/AnchorUsagesPassThroughCase.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/cases/AnchorUsagesFieldInstanceCase.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/cases/AnchorUsagesReceiverCase.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/AnchorUsagesSemanticUsageDetector.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/AnchorUsagesTargetResolver.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/AnchorUsagesPostProcessor.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/analysis/AnchorUsagesAnalysis.sc" \
  --script "$FRONTEND_KOTLIN_DIR/anchor_usages/AnchorUsages.sc" \
  --param cpgPath="$CPG_PATH" \
  --param viewAnchorsPath="$VIEW_ANCHORS" \
  --param assignmentDeclRefsPath="$ASSIGNMENT_DECL" \
  --param outputPath="$OUTPUT"

echo ""
echo "[+] Done. Check $OUTPUT"
