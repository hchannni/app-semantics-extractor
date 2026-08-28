#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCALA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EVIDENCE_DIR="$SCALA_DIR/evidence"
JOERN_BIN="${JOERN_BIN:-$(command -v joern || true)}"
CPG_PATH="${CPG_PATH:-}"
SLICER_OUTPUT_DIR="${SLICER_OUTPUT_DIR:-$SCALA_DIR/runs/target_home_context_slicer_bridge/context-slicer-output}"
METHOD_BODIES="$SLICER_OUTPUT_DIR/method-bodies.json"
OUTPUT_INDEX="${OUTPUT_INDEX:-$SCALA_DIR/runs/target_home_method_cfg_bridge/method-cfg-index.json}"
CFG_REPORT_DIR="${CFG_REPORT_DIR:-$SCALA_DIR/runs/target_home_method_cfg_bridge/method-cfg-reports}"
METHOD_CFG_LIMIT="${METHOD_CFG_LIMIT:-}"

cd "$SCALA_DIR"

require_file() {
  local path="$1"
  local label="$2"
  if [[ ! -e "$path" ]]; then
    echo "[ERROR] ${label} not found: $path" >&2
    exit 1
  fi
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[ERROR] required command not found: $cmd" >&2
    exit 1
  fi
}

iso_now() {
  date +"%Y-%m-%dT%H:%M:%S%z" | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/'
}

require_cmd jq
require_cmd shasum
require_file "$JOERN_BIN" "joern executable"
require_file "$CPG_PATH" "cpg file"
require_file "$METHOD_BODIES" "method-bodies.json"

mkdir -p "$(dirname "$OUTPUT_INDEX")"
mkdir -p "$CFG_REPORT_DIR"

LOG_DIR="$CFG_REPORT_DIR/logs"
JSON_DIR="$CFG_REPORT_DIR/json"
mkdir -p "$LOG_DIR" "$JSON_DIR"

TMP_DIR="$(mktemp -d /tmp/joern-method-cfg-index.XXXXXX)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

METHODS_JSON="$TMP_DIR/methods.json"
ROWS_NDJSON="$TMP_DIR/rows.ndjson"

# method-bodies.json: { fullName → MethodInfo } 전역 lookup (이미 domain 메서드만 포함, dedupe 완료)
jq -c '[
  to_entries[] | . as $entry | .value | {
    method_body_key: $entry.key,
    method_full_name: .method_full_name,
    method_id: (.method_id // "-1"),
    method_name: (.method_name // ""),
    parameter_count: (.parameter_count // -1),
    unresolved_signature: (.unresolved_signature // false),
    file: .file,
    start_line: .start_line,
    end_line: .end_line
  }
] | sort_by(.method_full_name)' "$METHOD_BODIES" > "$METHODS_JSON"

full_method_count="$(jq 'length' "$METHODS_JSON")"
method_cfg_limit_json="null"
if [[ -n "$METHOD_CFG_LIMIT" ]]; then
  if [[ ! "$METHOD_CFG_LIMIT" =~ ^[0-9]+$ ]]; then
    echo "[ERROR] METHOD_CFG_LIMIT must be a non-negative integer: $METHOD_CFG_LIMIT" >&2
    exit 1
  fi
  method_cfg_limit_json="$METHOD_CFG_LIMIT"
  jq --argjson limit "$METHOD_CFG_LIMIT" '.[0:$limit]' "$METHODS_JSON" > "$TMP_DIR/methods-limited.json"
  mv "$TMP_DIR/methods-limited.json" "$METHODS_JSON"
fi
method_count="$(jq 'length' "$METHODS_JSON")"
success_count=0
failed_count=0
generated_at="$(iso_now)"

: > "$ROWS_NDJSON"

if [[ "$method_count" -gt 0 ]]; then
  echo "[*] method cfg batch start"
  echo "    methods=$method_count"
  echo "    full_methods=$full_method_count"
  echo "    method_cfg_limit=$METHOD_CFG_LIMIT"
  echo "    method_bodies=$METHOD_BODIES"
  echo "    cpg=$CPG_PATH"
  echo "    output_index=$OUTPUT_INDEX"
  echo "    cfg_report_dir=$CFG_REPORT_DIR"

  while IFS= read -r row; do
    method_body_key="$(jq -r '.method_body_key' <<<"$row")"
    method_full_name="$(jq -r '.method_full_name' <<<"$row")"
    raw_method_id="$(jq -r '.method_id // "-1"' <<<"$row")"
    method_name="$(jq -r '.method_name // ""' <<<"$row")"
    parameter_count="$(jq -r '.parameter_count // -1' <<<"$row")"
    unresolved_signature="$(jq -r '.unresolved_signature // false' <<<"$row")"
    file_path="$(jq -r '.file' <<<"$row")"
    start_line="$(jq -r '.start_line' <<<"$row")"
    end_line="$(jq -r '.end_line' <<<"$row")"

    method_id="-1"
    if [[ "$raw_method_id" =~ ^[1-9][0-9]*$ ]]; then
      method_id="$raw_method_id"
    fi
    if [[ ! "$parameter_count" =~ ^-?[0-9]+$ ]]; then
      parameter_count="-1"
    fi
    if [[ "$unresolved_signature" != "true" && "$unresolved_signature" != "false" ]]; then
      unresolved_signature="false"
    fi

    selector_kind="methodFullName"
    selector_value="$method_full_name"
    if [[ "$method_id" =~ ^[1-9][0-9]*$ ]]; then
      selector_kind="methodId"
      selector_value="$method_id"
    fi

    method_key_seed="$method_full_name"
    if [[ "$method_id" =~ ^[1-9][0-9]*$ ]]; then
      method_key_seed="methodId:$method_id:$method_full_name"
    fi
    method_key="$(printf "%s" "$method_key_seed" | shasum -a 1 | awk '{print $1}')"
    cfg_json_path="$JSON_DIR/$method_key.json"
    log_path="$LOG_DIR/$method_key.log"

    rm -f "$cfg_json_path" "$log_path"

    status="FAILED"
    cfg_path_value=""
    node_count_json="null"
    edge_count_json="null"

    joern_args=(
      --script "$EVIDENCE_DIR/MethodCfgAnalysis.sc"
      --param "cpgPath=$CPG_PATH"
      --param "outputPath=$cfg_json_path"
    )
    if [[ "$selector_kind" == "methodId" ]]; then
      joern_args+=(--param "methodId=$method_id")
    else
      joern_args+=(--param "methodFullName=$method_full_name")
    fi

    if "$JOERN_BIN" "${joern_args[@]}" >"$log_path" 2>&1; then
      if [[ -s "$cfg_json_path" ]]; then
        status="SUCCESS"
        cfg_path_value="$cfg_json_path"
        node_count_json="$(jq -r '.analysis.cfgSummary.nodeCount // null' "$cfg_json_path" 2>/dev/null || echo "null")"
        edge_count_json="$(jq -r '.analysis.cfgSummary.edgeCount // null' "$cfg_json_path" 2>/dev/null || echo "null")"
        [[ -n "$node_count_json" ]] || node_count_json="null"
        [[ -n "$edge_count_json" ]] || edge_count_json="null"
        success_count=$((success_count + 1))
      else
        echo "[WARN] cfg output missing/empty: $method_full_name" >>"$log_path"
        rm -f "$cfg_json_path"
        failed_count=$((failed_count + 1))
      fi
    else
      rm -f "$cfg_json_path"
      failed_count=$((failed_count + 1))
    fi

    jq -nc \
      --arg method_body_key "$method_body_key" \
      --arg method_full_name "$method_full_name" \
      --arg method_id "$method_id" \
      --arg method_name "$method_name" \
      --argjson parameter_count "$parameter_count" \
      --argjson unresolved_signature "$unresolved_signature" \
      --arg method_key "$method_key" \
      --arg selector_kind "$selector_kind" \
      --arg selector_value "$selector_value" \
      --arg file "$file_path" \
      --argjson start_line "$start_line" \
      --argjson end_line "$end_line" \
      --arg status "$status" \
      --arg cfg_path "$cfg_path_value" \
      --arg log_path "$log_path" \
      --argjson node_count "$node_count_json" \
      --argjson edge_count "$edge_count_json" \
      '{
        method_body_key: $method_body_key,
        method_full_name: $method_full_name,
        method_id: $method_id,
        method_name: $method_name,
        parameter_count: $parameter_count,
        unresolved_signature: $unresolved_signature,
        method_key: $method_key,
        selector: {
          kind: $selector_kind,
          value: $selector_value
        },
        file: $file,
        start_line: $start_line,
        end_line: $end_line,
        status: $status,
        cfg_path: (if $cfg_path == "" then null else $cfg_path end),
        log_path: $log_path,
        cfg_summary: (
          if $node_count == null and $edge_count == null then null
          else {
            node_count: $node_count,
            edge_count: $edge_count
          }
          end
        )
      }' >> "$ROWS_NDJSON"
  done < <(jq -c '.[]' "$METHODS_JSON")
fi

jq -s \
  --arg method_bodies "$METHOD_BODIES" \
  --arg cpg_path "$CPG_PATH" \
  --arg generated_at "$generated_at" \
  --argjson full_method_count "$full_method_count" \
  --argjson method_cfg_limit "$method_cfg_limit_json" \
  --argjson method_count "$method_count" \
  --argjson success_count "$success_count" \
  --argjson failed_count "$failed_count" \
  '
  (sort_by(.method_full_name)) as $methods
  | {
      meta: {
        method_bodies_path: $method_bodies,
        cpg_path: $cpg_path,
        generated_at: $generated_at,
        full_method_count: $full_method_count,
        method_cfg_limit: $method_cfg_limit,
        method_count: $method_count,
        success_count: $success_count,
        failed_count: $failed_count
      },
      methods: $methods,
      method_lookup: (reduce $methods[] as $item ({}; .[$item.method_full_name] = $item)),
      method_id_lookup: (reduce $methods[] as $item ({}; if $item.method_id == "-1" then . else .[$item.method_id] = $item end))
    }
  ' "$ROWS_NDJSON" > "$OUTPUT_INDEX"

echo "[+] method cfg batch done"
echo "    success=$success_count"
echo "    failed=$failed_count"
echo "    index=$OUTPUT_INDEX"
