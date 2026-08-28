"""Export Android layout/menu resource view declarations as JSON."""

from __future__ import annotations

import argparse
from pathlib import Path

from app_semantics_kb.static_analysis.android_resources.resource_inventory import (
    build_resource_inventory,
    write_resource_inventory,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Android XML layout/menu ResourceViewDecl inventory",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--res-root", required=True, help="Android res directory, e.g. app/src/main/res")
    parser.add_argument("--output", required=True, help="Path to write resource-view-decls.json")
    parser.add_argument("--resource-owner", default=None, help="Optional package owner for qualified R ids")
    parser.add_argument("--source-origin", default="app_xml", help="Inventory source origin label")
    parser.add_argument("--dependency-coordinate", default=None, help="Optional Gradle dependency coordinate")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    inventory = build_resource_inventory(
        Path(args.res_root),
        resource_owner=args.resource_owner,
        source_origin=args.source_origin,
        dependency_coordinate=args.dependency_coordinate,
    )
    output = write_resource_inventory(Path(args.output), inventory)
    summary = inventory.summary
    print(output)
    print(
        "resource inventory: "
        f"total={summary['total_declarations']} "
        f"unique={summary['unique_resource_names']} "
        f"layout={summary['layout_declarations']} "
        f"menu={summary['menu_declarations']} "
        f"duplicates={summary['duplicate_resource_names']}"
    )


if __name__ == "__main__":
    main()
