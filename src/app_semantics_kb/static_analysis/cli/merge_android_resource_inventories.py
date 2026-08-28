"""Merge multiple Android ResourceViewDecl inventory JSON files."""

from __future__ import annotations

import argparse
from pathlib import Path

from app_semantics_kb.static_analysis.android_resources.resource_inventory import (
    build_inventory_from_declarations,
    read_resource_inventory,
    write_resource_inventory,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge Android ResourceViewDecl inventory files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--inventory", action="append", required=True, help="Inventory JSON path")
    parser.add_argument("--output", required=True, help="Path to write merged inventory JSON")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    declarations = []
    for inventory_path in args.inventory:
        declarations.extend(read_resource_inventory(Path(inventory_path)).declarations)
    inventory = build_inventory_from_declarations(declarations)
    output = write_resource_inventory(Path(args.output), inventory)
    summary = inventory.summary
    print(output)
    print(
        "merged resource inventory: "
        f"total={summary['total_declarations']} "
        f"unique={summary['unique_resource_names']} "
        f"duplicates={summary['duplicate_resource_names']}"
    )


if __name__ == "__main__":
    main()
