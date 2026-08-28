"""Extract field types from generated Android ViewBinding Java sources."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from .gradle_artifacts import read_build_artifacts_manifest


PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z0-9_.]+)\s*;")
IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z0-9_.]+)\s*;")
CLASS_RE = re.compile(r"\bpublic\s+final\s+class\s+([A-Za-z][A-Za-z0-9_]*)\b")
FIELD_RE = re.compile(
    r"^\s*public\s+final\s+"
    r"(?P<type>[A-Za-z_$][A-Za-z0-9_.$]*(?:<[^;]+>)?)\s+"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*;"
)


@dataclass(frozen=True)
class ViewBindingFieldType:
    binding_class: str
    binding_class_full_name: str
    field_name: str
    field_type: str
    field_type_raw: str
    source_file: str
    source_path: str
    line: int
    resolution: str
    evidence: list[str]


@dataclass(frozen=True)
class ViewBindingFieldInventory:
    summary: dict[str, int]
    fields: list[ViewBindingFieldType]

    def to_json_dict(self) -> dict:
        return {
            "summary": self.summary,
            "fields": [asdict(field) for field in self.fields],
        }


def build_view_binding_field_inventory(source_root: Path) -> ViewBindingFieldInventory:
    source_root = source_root.resolve()
    fields: list[ViewBindingFieldType] = []
    for java_path in sorted(source_root.rglob("*Binding.java")):
        fields.extend(_parse_binding_source(java_path, source_root))

    fields.sort(key=lambda field: (field.binding_class_full_name, field.field_name, field.line))
    summary = {
        "binding_classes": len({field.binding_class_full_name for field in fields}),
        "fields": len(fields),
    }
    return ViewBindingFieldInventory(summary=summary, fields=fields)


def build_view_binding_field_inventory_from_manifest(
    artifact_manifest_path: Path,
) -> ViewBindingFieldInventory:
    artifacts = read_build_artifacts_manifest(artifact_manifest_path)
    if not artifacts.generated_source_dir:
        raise ValueError(f"manifest has no generated_source_dir: {artifact_manifest_path}")
    return build_view_binding_field_inventory(Path(artifacts.generated_source_dir))


def write_view_binding_field_inventory(
    output_path: Path,
    inventory: ViewBindingFieldInventory,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(inventory.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def _parse_binding_source(java_path: Path, source_root: Path) -> list[ViewBindingFieldType]:
    lines = java_path.read_text(encoding="utf-8").splitlines()
    package_name = _first_match(lines, PACKAGE_RE)
    imports = _imports_by_simple_name(lines)
    class_name = _first_match(lines, CLASS_RE)
    if not package_name or not class_name:
        return []

    source_path = _relative_or_absolute(java_path, source_root)
    full_class_name = f"{package_name}.{class_name}"
    return [
        _field_from_match(
            match=match,
            line_number=line_number,
            imports=imports,
            java_path=java_path,
            source_path=source_path,
            class_name=class_name,
            full_class_name=full_class_name,
        )
        for line_number, line in enumerate(lines, start=1)
        for match in [FIELD_RE.match(line)]
        if match is not None
    ]


def _field_from_match(
    *,
    match: re.Match[str],
    line_number: int,
    imports: dict[str, str],
    java_path: Path,
    source_path: str,
    class_name: str,
    full_class_name: str,
) -> ViewBindingFieldType:
    raw_type = match.group("type")
    resolved_type, resolution = _resolve_type(raw_type, imports)
    field_name = match.group("name")
    return ViewBindingFieldType(
        binding_class=class_name,
        binding_class_full_name=full_class_name,
        field_name=field_name,
        field_type=resolved_type,
        field_type_raw=raw_type,
        source_file=java_path.name,
        source_path=source_path,
        line=line_number,
        resolution=resolution,
        evidence=["generated_view_binding"],
    )


def _first_match(lines: list[str], pattern: re.Pattern[str]) -> str | None:
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None


def _imports_by_simple_name(lines: list[str]) -> dict[str, str]:
    imports: dict[str, str] = {}
    for line in lines:
        match = IMPORT_RE.match(line)
        if match:
            full_name = match.group(1)
            imports[full_name.rsplit(".", 1)[-1]] = full_name
    return imports


def _resolve_type(raw_type: str, imports: dict[str, str]) -> tuple[str, str]:
    if "." in raw_type:
        return raw_type, "qualified"
    resolved = imports.get(raw_type)
    if resolved:
        return resolved, "import"
    return raw_type, "raw"


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
