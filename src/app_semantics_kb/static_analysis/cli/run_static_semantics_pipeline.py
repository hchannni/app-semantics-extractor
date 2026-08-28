"""
run_static_semantics_pipeline.py — target-home static semantics end-to-end pipeline

target-home producer chain 실행과 canonical StaticSemanticBundle export를
하나의 공식 실행 경로로 묶는다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .runtime import StaticAnalysisResult, default_cpg_path, default_joern_bin


def _load_builder():
    builder_path = Path(__file__).resolve().parents[1] / "exporter" / "static_semantic_bundle_builder.py"
    spec = importlib.util.spec_from_file_location("static_semantic_bundle_builder", builder_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load builder from {builder_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
def _producer_chain_runner_path(scala_dir: Path, frontend: str) -> tuple[Path, str]:
    if frontend == "kotlin":
        return (
            scala_dir / "frontend-kotlin" / "run_target_home_anchor_usages_producer_chain.sh",
            "target_home_static_semantics_pipeline.",
        )
    if frontend == "java":
        return (
            scala_dir / "frontend-java" / "run_java_anchor_usages_producer_chain.sh",
            "java_static_semantics_pipeline.",
        )
    raise ValueError(f"unsupported frontend: {frontend!r}")


def _run_static_semantics_producer_chain(
    *,
    frontend: str,
    joern_bin: str,
    cpg_path: str,
    source_path: str | None,
    resource_inventory_path: str | None = None,
    view_binding_field_types_path: str | None = None,
    method_cfg_limit: int | None = None,
    output_root: Path | None = None,
) -> StaticAnalysisResult:
    scala_dir = Path(__file__).resolve().parents[1]
    runner_path, output_prefix = _producer_chain_runner_path(scala_dir, frontend)
    runs_dir = scala_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    output_root = output_root or Path(
        tempfile.mkdtemp(
            prefix=output_prefix,
            dir=str(runs_dir),
        )
    )
    output_root.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["JOERN_BIN"] = joern_bin
    env["CPG_PATH"] = cpg_path
    env["OUTPUT_ROOT"] = str(output_root)
    if source_path:
        env["SOURCE_PATH"] = source_path
    if resource_inventory_path:
        env["RESOURCE_INVENTORY"] = resource_inventory_path
    if view_binding_field_types_path:
        env["VIEW_BINDING_FIELD_TYPES"] = view_binding_field_types_path
    if method_cfg_limit is not None:
        if method_cfg_limit < 0:
            raise ValueError("method_cfg_limit must be non-negative")
        env["METHOD_CFG_LIMIT"] = str(method_cfg_limit)

    subprocess.run(["bash", str(runner_path)], cwd=scala_dir, env=env, check=True)

    outputs = {
        "view_anchors": str(output_root / "view-anchors.json"),
        "view_instances": str(output_root / "view-instances.json"),
        "canonical_view_instances": str(output_root / "canonical-view-instances.json"),
        "view_anchors_v2": str(output_root / "view-anchors-v2.json"),
        "assignment_declarations": str(output_root / "assignment-declarations.json"),
        "anchor_usages": str(output_root / "anchor-usages.json"),
        "context_slicer_output": str(output_root / "context-slicer-output"),
        "method_cfg_index": str(output_root / "method-cfg-index.json"),
        "cfg_report_dir": str(output_root / "method-cfg-reports"),
    }

    manifest_path = output_root / "producer_chain_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "frontend": frontend,
                "joern_bin": joern_bin,
                "cpg_path": cpg_path,
                "source_path": source_path,
                "resource_inventory_path": resource_inventory_path,
                "view_binding_field_types_path": view_binding_field_types_path,
                "method_cfg_limit": method_cfg_limit,
                "runner_path": str(runner_path),
                "outputs": outputs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return StaticAnalysisResult(
        output_root=output_root,
        manifest_path=manifest_path,
        outputs={key: Path(value) for key, value in outputs.items()},
    )


def _run_target_home_producer_chain(
    *,
    joern_bin: str,
    cpg_path: str,
    source_path: str | None,
    resource_inventory_path: str | None = None,
    view_binding_field_types_path: str | None = None,
    method_cfg_limit: int | None = None,
    output_root: Path | None = None,
) -> StaticAnalysisResult:
    """Backward-compatible Kotlin producer chain wrapper."""
    return _run_static_semantics_producer_chain(
        frontend="kotlin",
        joern_bin=joern_bin,
        cpg_path=cpg_path,
        source_path=source_path,
        resource_inventory_path=resource_inventory_path,
        view_binding_field_types_path=view_binding_field_types_path,
        method_cfg_limit=method_cfg_limit,
        output_root=output_root,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run target-home static-semantics producer chain and export StaticSemanticBundle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--joern-bin", default=default_joern_bin(), help="Path to external joern executable")
    parser.add_argument("--cpg-path", default=default_cpg_path(), help="Path to external CPG file")
    parser.add_argument("--source-path", default=None, help="Optional source root for context slicer")
    parser.add_argument("--resource-inventory", default=None, help="Optional ViewAnchors-v2 resource inventory JSON path")
    parser.add_argument("--view-binding-field-types", default=None, help="Optional ViewBinding field type JSON path")
    parser.add_argument("--method-cfg-limit", type=int, default=None, help="Limit MethodCfgAnalysis to the first N sliced methods")
    parser.add_argument("--output-root", default=None, help="Output directory for repro artifacts")
    parser.add_argument("--bundle-output-path", default=None, help="Path to write static_semantic_bundle.json (default: <output-root>/static_semantic_bundle.json)")
    parser.add_argument("--frontend", choices=["kotlin", "java"], default="kotlin", help="Static frontend producer chain to run")
    parser.add_argument("--run-id", required=True, help="StaticSemanticBundle run_id")
    return parser


def run_static_semantics_pipeline(
    *,
    frontend: str = "kotlin",
    joern_bin: str,
    cpg_path: str,
    source_path: str | None,
    run_id: str,
    resource_inventory_path: str | None = None,
    view_binding_field_types_path: str | None = None,
    method_cfg_limit: int | None = None,
    output_root: Path | None = None,
    bundle_output_path: Path | None = None,
) -> tuple[StaticAnalysisResult, Path]:
    repro_result = _run_static_semantics_producer_chain(
        frontend=frontend,
        joern_bin=joern_bin,
        cpg_path=cpg_path,
        source_path=source_path,
        resource_inventory_path=resource_inventory_path,
        view_binding_field_types_path=view_binding_field_types_path,
        method_cfg_limit=method_cfg_limit,
        output_root=output_root,
    )
    output_path = bundle_output_path or (repro_result.output_root / "static_semantic_bundle.json")
    builder = _load_builder()
    bundle = builder.build_static_semantic_bundle(
        legacy_joern_dir=repro_result.output_root,
        run_id=run_id,
    )
    bundle_path = builder.save_static_semantic_bundle(output_path, bundle)
    return repro_result, bundle_path


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.joern_bin:
        raise SystemExit("joern executable path is required (--joern-bin)")
    if not args.cpg_path:
        raise SystemExit("CPG path is required (--cpg-path)")

    repro_result, bundle_path = run_static_semantics_pipeline(
        frontend=args.frontend,
        joern_bin=args.joern_bin,
        cpg_path=args.cpg_path,
        source_path=args.source_path,
        resource_inventory_path=args.resource_inventory,
        view_binding_field_types_path=args.view_binding_field_types,
        method_cfg_limit=args.method_cfg_limit,
        output_root=Path(args.output_root) if args.output_root else None,
        bundle_output_path=Path(args.bundle_output_path) if args.bundle_output_path else None,
        run_id=args.run_id,
    )
    print(repro_result.output_root)
    print(bundle_path)


if __name__ == "__main__":
    main()
