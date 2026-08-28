#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_KOTLIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCALA_DIR="$(cd "$FRONTEND_KOTLIN_DIR/.." && pwd)"

JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
RESOURCE_INVENTORY="${RESOURCE_INVENTORY:-$SCALA_DIR/runs/resource-view-decls.json}"
VIEW_BINDING_FIELD_TYPES="${VIEW_BINDING_FIELD_TYPES:-}"
OUTPUT_ROOT="${OUTPUT_ROOT:-$SCALA_DIR/runs/view_anchors_v2}"
VIEW_INSTANCES_OUTPUT="${VIEW_INSTANCES_OUTPUT:-$OUTPUT_ROOT/view-instances.json}"
CANONICAL_VIEW_INSTANCES_OUTPUT="${CANONICAL_VIEW_INSTANCES_OUTPUT:-$OUTPUT_ROOT/canonical-view-instances.json}"
ANCHORS_V2_OUTPUT="${ANCHORS_V2_OUTPUT:-$OUTPUT_ROOT/view-anchors-v2.json}"
LEGACY_OUTPUT="${LEGACY_OUTPUT:-$OUTPUT_ROOT/view-anchors.json}"

if [[ ! -x "$JOERN_BIN" ]]; then
  echo "[ERROR] joern executable not found: $JOERN_BIN" >&2
  exit 1
fi

if [[ ! -f "$CPG_PATH" ]]; then
  echo "[ERROR] cpg file not found: $CPG_PATH" >&2
  exit 1
fi

if [[ ! -f "$RESOURCE_INVENTORY" ]]; then
  echo "[ERROR] resource inventory not found: $RESOURCE_INVENTORY" >&2
  exit 1
fi

mkdir -p "$OUTPUT_ROOT"

echo "[*] Running Kotlin ViewAnchors V2"
echo "    JOERN_BIN=$JOERN_BIN"
echo "    CPG_PATH=$CPG_PATH"
echo "    RESOURCE_INVENTORY=$RESOURCE_INVENTORY"
echo "    VIEW_BINDING_FIELD_TYPES=$VIEW_BINDING_FIELD_TYPES"
echo "    OUTPUT_ROOT=$OUTPUT_ROOT"
echo "    CANONICAL_VIEW_INSTANCES_OUTPUT=$CANONICAL_VIEW_INSTANCES_OUTPUT"

"$JOERN_BIN" \
  --import "$SCRIPT_DIR/ViewAnchorV2Contract.sc" \
  --import "$SCRIPT_DIR/ResourceInventoryLoader.sc" \
  --import "$SCRIPT_DIR/ViewBindingFieldTypeLoader.sc" \
  --import "$SCRIPT_DIR/ViewInstanceRules.sc" \
  --import "$SCRIPT_DIR/ViewInstanceCollector.sc" \
  --import "$SCRIPT_DIR/ViewInstanceCanonicalizer.sc" \
  --import "$SCRIPT_DIR/ViewAnchorV2Json.sc" \
  --import "$SCRIPT_DIR/ViewAnchorV2Pipeline.sc" \
  --script "$SCRIPT_DIR/ViewAnchorsV2.sc" \
  --param inputPath="$CPG_PATH" \
  --param resourceInventoryPath="$RESOURCE_INVENTORY" \
  --param viewInstancesOutputPath="$VIEW_INSTANCES_OUTPUT" \
  --param canonicalViewInstancesOutputPath="$CANONICAL_VIEW_INSTANCES_OUTPUT" \
  --param anchorsOutputPath="$ANCHORS_V2_OUTPUT" \
  --param legacyOutputPath="$LEGACY_OUTPUT" \
  --param viewBindingFieldTypesPath="$VIEW_BINDING_FIELD_TYPES"

echo "[+] Done: $OUTPUT_ROOT"
