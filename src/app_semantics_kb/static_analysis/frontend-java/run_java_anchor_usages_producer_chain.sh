#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCALA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VIEW_ROOT="$SCRIPT_DIR/view_anchors"
ANCHOR_ROOT="$SCRIPT_DIR/anchor_usages"
SLICER_ROOT="$SCALA_DIR/slicer"
EVIDENCE_ROOT="$SCALA_DIR/evidence"
RUNS_DIR="$SCALA_DIR/runs"

source "$SCRIPT_DIR/common/java_output_guard.sh"

JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
RESOURCE_INVENTORY="${RESOURCE_INVENTORY:-}"
VIEW_BINDING_FIELD_TYPES="${VIEW_BINDING_FIELD_TYPES:-}"
mkdir -p "$RUNS_DIR"
OUTPUT_ROOT="${OUTPUT_ROOT:-$(mktemp -d "$RUNS_DIR/java_anchor_usages_producer_chain.XXXXXX")}"
java_frontend_require_output_under_runs "$OUTPUT_ROOT" "OUTPUT_ROOT" "$RUNS_DIR"

VIEW_OUTPUT="$OUTPUT_ROOT/view-anchors.json"
VIEW_INSTANCES_OUTPUT="$OUTPUT_ROOT/view-instances.json"
CANONICAL_VIEW_INSTANCES_OUTPUT="$OUTPUT_ROOT/canonical-view-instances.json"
VIEW_V2_OUTPUT="$OUTPUT_ROOT/view-anchors-v2.json"
ASSIGNMENT_OUTPUT="$OUTPUT_ROOT/assignment-declarations.json"
ANCHOR_OUTPUT="$OUTPUT_ROOT/anchor-usages.json"
CONTEXT_OUTPUT_DIR="$OUTPUT_ROOT/context-slicer-output"
METHOD_CFG_INDEX_OUTPUT="$OUTPUT_ROOT/method-cfg-index.json"
METHOD_CFG_REPORT_DIR="$OUTPUT_ROOT/method-cfg-reports"

mkdir -p "$OUTPUT_ROOT"

echo "[*] Running Java frontend producer chain"
echo "    JOERN_BIN=$JOERN_BIN"
echo "    CPG_PATH=$CPG_PATH"
echo "    RESOURCE_INVENTORY=$RESOURCE_INVENTORY"
echo "    VIEW_BINDING_FIELD_TYPES=$VIEW_BINDING_FIELD_TYPES"
echo "    OUTPUT_ROOT=$OUTPUT_ROOT"

JOERN_BIN="$JOERN_BIN" \
CPG_PATH="$CPG_PATH" \
OUTPUT_PATH="$VIEW_OUTPUT" \
ANCHORS_V2_OUTPUT="$VIEW_V2_OUTPUT" \
VIEW_INSTANCES_OUTPUT="$VIEW_INSTANCES_OUTPUT" \
CANONICAL_VIEW_INSTANCES_OUTPUT="$CANONICAL_VIEW_INSTANCES_OUTPUT" \
RESOURCE_INVENTORY="$RESOURCE_INVENTORY" \
VIEW_BINDING_FIELD_TYPES="$VIEW_BINDING_FIELD_TYPES" \
bash "$VIEW_ROOT/run_java_view_anchors_bridge.sh"

JOERN_BIN="$JOERN_BIN" \
CPG_PATH="$CPG_PATH" \
VIEW_ANCHORS="$VIEW_V2_OUTPUT" \
OUTPUT="$ASSIGNMENT_OUTPUT" \
bash "$ANCHOR_ROOT/run_java_assignment_decl_bridge.sh"

JOERN_BIN="$JOERN_BIN" \
CPG_PATH="$CPG_PATH" \
VIEW_ANCHORS="$VIEW_V2_OUTPUT" \
ASSIGNMENT_DECL="$ASSIGNMENT_OUTPUT" \
OUTPUT="$ANCHOR_OUTPUT" \
bash "$ANCHOR_ROOT/run_java_analysis_bridge.sh"

JOERN_BIN="$JOERN_BIN" \
CPG_PATH="$CPG_PATH" \
ANCHOR_USAGES="$ANCHOR_OUTPUT" \
OUTPUT_DIR="$CONTEXT_OUTPUT_DIR" \
SOURCE_PATH="${SOURCE_PATH:-}" \
bash "$SLICER_ROOT/run_target_home_context_slicer_bridge.sh"

JOERN_BIN="$JOERN_BIN" \
CPG_PATH="$CPG_PATH" \
SLICER_OUTPUT_DIR="$CONTEXT_OUTPUT_DIR" \
OUTPUT_INDEX="$METHOD_CFG_INDEX_OUTPUT" \
CFG_REPORT_DIR="$METHOD_CFG_REPORT_DIR" \
bash "$EVIDENCE_ROOT/run_target_home_method_cfg_index_bridge.sh"

echo "[+] Java frontend producer chain complete: $OUTPUT_ROOT"
