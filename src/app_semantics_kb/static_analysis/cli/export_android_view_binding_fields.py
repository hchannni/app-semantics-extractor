"""Export generated Android ViewBinding field types as JSON."""

from __future__ import annotations

import argparse
from pathlib import Path

from app_semantics_kb.static_analysis.android_resources.view_binding_fields import (
    build_view_binding_field_inventory,
    build_view_binding_field_inventory_from_manifest,
    write_view_binding_field_inventory,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export generated ViewBinding field type inventory",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source-root", help="Generated data_binding_base_class_source_out/.../out dir")
    source_group.add_argument("--artifact-manifest", help="Manifest from prepare_android_view_binding_artifacts")
    parser.add_argument("--output", required=True, help="Path to write view-binding-field-types.json")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.artifact_manifest:
        inventory = build_view_binding_field_inventory_from_manifest(Path(args.artifact_manifest))
    else:
        inventory = build_view_binding_field_inventory(Path(args.source_root))

    output = write_view_binding_field_inventory(Path(args.output), inventory)
    print(output)
    print(
        "view binding fields: "
        f"classes={inventory.summary['binding_classes']} "
        f"fields={inventory.summary['fields']}"
    )


if __name__ == "__main__":
    main()
