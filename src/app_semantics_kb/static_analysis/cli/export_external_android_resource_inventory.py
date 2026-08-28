"""Export dependency Android layout/menu ResourceViewDecl inventory from merger.xml."""

from __future__ import annotations

import argparse
from pathlib import Path

from app_semantics_kb.static_analysis.android_resources.merged_resources import (
    build_external_resource_inventory_from_merger,
    parse_dependency_resource_roots,
)
from app_semantics_kb.static_analysis.android_resources.resource_inventory import (
    write_resource_inventory,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export external Android ResourceViewDecl inventory from Gradle resource merger.xml",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--merger-xml", required=True, help="merge<Variant>Resources merger.xml")
    parser.add_argument("--output", required=True, help="Path to write external-resource-view-decls.json")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    merger_xml = Path(args.merger_xml)
    roots = parse_dependency_resource_roots(merger_xml)
    inventory = build_external_resource_inventory_from_merger(merger_xml)
    output = write_resource_inventory(Path(args.output), inventory)
    summary = inventory.summary
    print(output)
    print(
        "external resource inventory: "
        f"dependencies={len(roots)} "
        f"total={summary['total_declarations']} "
        f"unique={summary['unique_resource_names']} "
        f"layout={summary['layout_declarations']} "
        f"menu={summary['menu_declarations']}"
    )


if __name__ == "__main__":
    main()
