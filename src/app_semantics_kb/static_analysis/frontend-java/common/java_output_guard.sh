#!/usr/bin/env bash

java_frontend_realpath() {
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

java_frontend_require_output_under_runs() {
  local candidate="$1"
  local variable_name="$2"
  local runs_dir="$3"

  mkdir -p "$runs_dir"
  local runs_real
  local candidate_real
  runs_real="$(java_frontend_realpath "$runs_dir")"
  candidate_real="$(java_frontend_realpath "$candidate")"

  case "$candidate_real" in
    "$runs_real"/*)
      ;;
    *)
      echo "[ERROR] $variable_name must resolve under $runs_real: $candidate" >&2
      exit 1
      ;;
  esac
}
