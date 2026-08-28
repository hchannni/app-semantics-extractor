#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCALA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SLICER_DIR="$SCALA_DIR/slicer"
FRONTEND_KOTLIN_DIR="$SCALA_DIR/frontend-kotlin"
JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
ANCHOR_USAGES="${ANCHOR_USAGES:-$SCALA_DIR/runs/target_home_anchor_usages_analysis_bridge/anchor-usages.json}"
OUTPUT_DIR="${OUTPUT_DIR:-$SCALA_DIR/runs/target_home_context_slicer_bridge/context-slicer-output}"
cd "$SCALA_DIR"
# 소스 루트 디렉토리 (재정의 가능; 기본값: CPG 파일명 기반 추론, 없으면 빈 문자열 → fallback)
_cpg_basename="${CPG_PATH%.cpg}"
SOURCE_PATH="${SOURCE_PATH:-}"
if [[ -z "$SOURCE_PATH" ]]; then
  # 1) CPG와 동명의 디렉토리가 있으면 사용
  if [[ -d "$_cpg_basename" ]]; then
    SOURCE_PATH="$_cpg_basename"
  # 2) joern-cli 상위 루트의 source/ 하위에서 첫 번째 디렉토리 사용
  elif [[ -d "$(dirname "$(dirname "$CPG_PATH")")/source" ]]; then
    SOURCE_PATH="$(ls -d "$(dirname "$(dirname "$CPG_PATH")")"/source/*/ 2>/dev/null | head -1)"
    SOURCE_PATH="${SOURCE_PATH%/}"
  # 3) scala/ 하위 source/가 있으면 사용
  elif [[ -d "$SCALA_DIR/source" ]]; then
    SOURCE_PATH="$(ls -d "$SCALA_DIR/source"/*/ 2>/dev/null | head -1)"
    SOURCE_PATH="${SOURCE_PATH%/}"
  fi
fi

if [[ ! -x "$JOERN_BIN" ]]; then
  echo "[ERROR] joern executable not found: $JOERN_BIN" >&2
  exit 1
fi

if [[ ! -f "$CPG_PATH" ]]; then
  echo "[ERROR] cpg file not found: $CPG_PATH" >&2
  exit 1
fi

if [[ ! -f "$ANCHOR_USAGES" ]]; then
  echo "[ERROR] anchor usages file not found: $ANCHOR_USAGES" >&2
  exit 1
fi

echo "[*] Running ContextSlicer"
echo "    CPG_PATH=$CPG_PATH"
echo "    ANCHOR_USAGES=$ANCHOR_USAGES"
echo "    OUTPUT_DIR=$OUTPUT_DIR"
echo "    SOURCE_PATH=$SOURCE_PATH"

"$JOERN_BIN" \
  --import "$FRONTEND_KOTLIN_DIR/view_anchors/ViewAnchorContract.sc" \
  --import "$FRONTEND_KOTLIN_DIR/anchor_usages/io/AnchorUsagesModel.sc" \
  --import "$SLICER_DIR/io/ContextSlicerModel.sc" \
  --import "$SLICER_DIR/io/ContextSlicerJson.sc" \
  --import "$SLICER_DIR/analysis/TypeResolution.sc" \
  --import "$SLICER_DIR/analysis/ContextCallbackResolver.sc" \
  --import "$SLICER_DIR/analysis/ContextMethodCollector.sc" \
  --script "$SLICER_DIR/ContextSlicer.sc" \
  --param cpgPath="$CPG_PATH" \
  --param anchorUsagesPath="$ANCHOR_USAGES" \
  --param outputDir="$OUTPUT_DIR" \
  --param sourcePath="$SOURCE_PATH"

echo "[+] Done: $OUTPUT_DIR/"
