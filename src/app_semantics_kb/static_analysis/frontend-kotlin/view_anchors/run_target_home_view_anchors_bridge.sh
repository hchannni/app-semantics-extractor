#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_KOTLIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCALA_DIR="$(cd "$FRONTEND_KOTLIN_DIR/.." && pwd)"

JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
OUTPUT_PATH="${OUTPUT_PATH:-$SCALA_DIR/runs/target_home_view_anchors_bridge/view-anchors.json}"

if [[ ! -x "$JOERN_BIN" ]]; then
  echo "[ERROR] joern executable not found: $JOERN_BIN" >&2
  exit 1
fi

if [[ ! -f "$CPG_PATH" ]]; then
  echo "[ERROR] cpg file not found: $CPG_PATH" >&2
  exit 1
fi

echo "[*] Running target-home ViewAnchors"
echo "    JOERN_BIN=$JOERN_BIN"
echo "    CPG_PATH=$CPG_PATH"
echo "    OUTPUT_PATH=$OUTPUT_PATH"

"$JOERN_BIN" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchorContract.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ResourceLookupRules.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ResourceIdCarrierResolver.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/WrapperLookupDiscovery.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/BindingFieldRules.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchorUsageAnalyzer.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchorBuilder.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ResourceLookupCollector.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/BindingFieldCollector.sc" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchorsPipeline.sc" \
  --script "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchors.sc" \
  --param inputPath="$CPG_PATH" \
  --param outputPath="$OUTPUT_PATH"

echo "[+] Done: $OUTPUT_PATH"
