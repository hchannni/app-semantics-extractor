"""
export_static_semantic_bundle.py — target-home StaticSemanticBundle export CLI
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def _load_builder():
    builder_path = Path(__file__).resolve().parents[1] / "exporter" / "static_semantic_bundle_builder.py"
    spec = importlib.util.spec_from_file_location("static_semantic_bundle_builder", builder_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load builder from {builder_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export StaticSemanticBundle from copied legacy Joern outputs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--legacy-joern-dir", required=True, help="Path to copied target-home legacy_joern snapshot")
    parser.add_argument("--output-path", required=True, help="Path to write static_semantic_bundle.json")
    parser.add_argument("--run-id", required=True, help="Artifact run_id")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    builder = _load_builder()
    bundle = builder.build_static_semantic_bundle(
        legacy_joern_dir=Path(args.legacy_joern_dir),
        run_id=args.run_id,
    )
    out = builder.save_static_semantic_bundle(Path(args.output_path), bundle)
    print(out)


if __name__ == "__main__":
    main()
