#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_JAVA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCALA_DIR="$(cd "$FRONTEND_JAVA_DIR/.." && pwd)"
RUNS_DIR="$SCALA_DIR/runs"

source "$FRONTEND_JAVA_DIR/common/java_output_guard.sh"

JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
OUTPUT_PATH="${OUTPUT_PATH:-$RUNS_DIR/java_view_anchors_bridge/view-anchors.json}"
OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
V2_OUTPUT_PATH="${ANCHORS_V2_OUTPUT:-${V2_OUTPUT_PATH:-$OUTPUT_DIR/view-anchors-v2.json}}"
VIEW_INSTANCES_OUTPUT="${VIEW_INSTANCES_OUTPUT:-$OUTPUT_DIR/view-instances.json}"
CANONICAL_VIEW_INSTANCES_OUTPUT="${CANONICAL_VIEW_INSTANCES_OUTPUT:-$OUTPUT_DIR/canonical-view-instances.json}"
RESOURCE_INVENTORY="${RESOURCE_INVENTORY:-}"
VIEW_BINDING_FIELD_TYPES="${VIEW_BINDING_FIELD_TYPES:-}"

if [[ ! -x "$JOERN_BIN" ]]; then
  echo "[ERROR] joern executable not found: $JOERN_BIN" >&2
  exit 1
fi

if [[ ! -f "$CPG_PATH" ]]; then
  echo "[ERROR] cpg file not found: $CPG_PATH" >&2
  exit 1
fi

java_frontend_require_output_under_runs "$OUTPUT_PATH" "OUTPUT_PATH" "$RUNS_DIR"
if [[ -n "$V2_OUTPUT_PATH" ]]; then
  java_frontend_require_output_under_runs "$V2_OUTPUT_PATH" "V2_OUTPUT_PATH" "$RUNS_DIR"
fi
if [[ -n "$VIEW_INSTANCES_OUTPUT" ]]; then
  java_frontend_require_output_under_runs "$VIEW_INSTANCES_OUTPUT" "VIEW_INSTANCES_OUTPUT" "$RUNS_DIR"
fi
if [[ -n "$CANONICAL_VIEW_INSTANCES_OUTPUT" ]]; then
  java_frontend_require_output_under_runs "$CANONICAL_VIEW_INSTANCES_OUTPUT" "CANONICAL_VIEW_INSTANCES_OUTPUT" "$RUNS_DIR"
fi
if [[ -n "$RESOURCE_INVENTORY" && ! -f "$RESOURCE_INVENTORY" ]]; then
  echo "[ERROR] Java resource inventory not found: $RESOURCE_INVENTORY" >&2
  exit 1
fi
if [[ -n "$VIEW_BINDING_FIELD_TYPES" && ! -f "$VIEW_BINDING_FIELD_TYPES" ]]; then
  echo "[ERROR] Java ViewBinding field types not found: $VIEW_BINDING_FIELD_TYPES" >&2
  exit 1
fi

echo "[*] Running Java ViewAnchors"
echo "    JOERN_BIN=$JOERN_BIN"
echo "    CPG_PATH=$CPG_PATH"
echo "    OUTPUT_PATH=$OUTPUT_PATH"
echo "    V2_OUTPUT_PATH=$V2_OUTPUT_PATH"
echo "    VIEW_INSTANCES_OUTPUT=$VIEW_INSTANCES_OUTPUT"
echo "    CANONICAL_VIEW_INSTANCES_OUTPUT=$CANONICAL_VIEW_INSTANCES_OUTPUT"
echo "    RESOURCE_INVENTORY=$RESOURCE_INVENTORY"
echo "    VIEW_BINDING_FIELD_TYPES=$VIEW_BINDING_FIELD_TYPES"

"$JOERN_BIN" \
  --import "$FRONTEND_JAVA_DIR/common/JavaOutputPathGuard.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewAnchorModel.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaResourceInventoryLoader.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewBindingFieldTypeLoader.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewInstanceRules.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewInstanceCanonicalizer.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewAnchorJson.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaResourceIdCallRules.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewAnchorClassifier.sc" \
  --import "$FRONTEND_JAVA_DIR/view_anchors/JavaViewAnchorCollector.sc" \
  --script "$FRONTEND_JAVA_DIR/view_anchors/JavaViewAnchors.sc" \
  --param inputPath="$CPG_PATH" \
  --param outputPath="$OUTPUT_PATH" \
  --param runsDir="$RUNS_DIR" \
  --param v2OutputPath="$V2_OUTPUT_PATH" \
  --param viewInstancesOutputPath="$VIEW_INSTANCES_OUTPUT" \
  --param canonicalViewInstancesOutputPath="$CANONICAL_VIEW_INSTANCES_OUTPUT" \
  --param resourceInventoryPath="$RESOURCE_INVENTORY" \
  --param viewBindingFieldTypesPath="$VIEW_BINDING_FIELD_TYPES"

echo "[+] Done: $OUTPUT_PATH"
