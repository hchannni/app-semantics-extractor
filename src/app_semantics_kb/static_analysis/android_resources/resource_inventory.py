"""Build a deterministic Android XML resource view declaration inventory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .view_binding_names import layout_name_to_binding_class, resource_id_to_binding_field

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ID_VALUE_RE = re.compile(r"^@(?P<kind>\+?id)/(?P<name>[A-Za-z0-9_.:-]+)$")


@dataclass(frozen=True)
class ResourceViewDecl:
    resource_id: str
    resource_name: str
    resource_kind: str
    source_type: str
    source_file: str
    source_path: str
    layout_name: str | None
    menu_name: str | None
    xml_tag: str
    binding_class: str | None
    binding_field: str | None
    id_decl_kind: str
    line: int | None
    resource_owner: str | None = None
    qualified_resource_id: str | None = None
    source_origin: str = "app_xml"
    dependency_coordinate: str | None = None
    qualifiers: str | None = None


@dataclass(frozen=True)
class ResourceInventory:
    declarations: list[ResourceViewDecl]
    summary: dict[str, int]
    duplicate_resource_names: dict[str, int]

    def to_json_dict(self) -> dict:
        return {
            "summary": self.summary,
            "duplicate_resource_names": self.duplicate_resource_names,
            "declarations": [asdict(decl) for decl in self.declarations],
        }


def _local_name(name: str) -> str:
    if "}" in name:
        return name.rsplit("}", 1)[1]
    return name


def _namespace(name: str) -> str | None:
    if name.startswith("{") and "}" in name:
        return name[1:].split("}", 1)[0]
    return None


def _strip_tag(tag: str) -> str:
    return _local_name(tag)


def _resource_family(dirname: str) -> str:
    return dirname.split("-", 1)[0]


def _resource_qualifiers(dirname: str) -> str | None:
    if "-" not in dirname:
        return None
    return dirname.split("-", 1)[1]


def _iter_resource_xml(res_root: Path) -> list[Path]:
    candidates: list[Path] = []
    if not res_root.exists():
        return candidates
    for subdir in sorted(path for path in res_root.iterdir() if path.is_dir()):
        family = _resource_family(subdir.name)
        if family in {"layout", "menu"}:
            candidates.extend(sorted(subdir.glob("*.xml")))
    return candidates


def _line_number_by_id(xml_text: str, resource_name: str) -> int | None:
    patterns = (
        f'@+id/{resource_name}',
        f'@id/{resource_name}',
    )
    for idx, line in enumerate(xml_text.splitlines(), start=1):
        if any(pattern in line for pattern in patterns):
            return idx
    return None


def _id_attribute_value(element: ET.Element) -> str | None:
    for attr_name, attr_value in element.attrib.items():
        if _local_name(attr_name) != "id":
            continue
        ns = _namespace(attr_name)
        if ns is not None and ns != ANDROID_NS:
            continue
        if ID_VALUE_RE.match(attr_value):
            return attr_value
    return None


def _source_type(xml_path: Path) -> str:
    return _resource_family(xml_path.parent.name)


def _resource_id(resource_owner: str | None, resource_name: str) -> str:
    if resource_owner:
        return f"{resource_owner}.R.id.{resource_name}"
    return f"R.id.{resource_name}"


def _resource_kind(source_type: str, tag: str) -> str:
    if source_type == "menu":
        return "menu_item" if tag == "item" else "menu_view"
    return "layout_view"


def _decl_from_element(
    *,
    element: ET.Element,
    xml_path: Path,
    res_root: Path,
    xml_text: str,
    resource_owner: str | None,
    source_origin: str,
    dependency_coordinate: str | None,
) -> ResourceViewDecl | None:
    id_value = _id_attribute_value(element)
    if id_value is None:
        return None
    match = ID_VALUE_RE.match(id_value)
    if match is None:
        return None

    resource_name = match.group("name")
    source_type = _source_type(xml_path)
    xml_stem = xml_path.stem
    is_layout = source_type == "layout"
    is_menu = source_type == "menu"

    binding_class = layout_name_to_binding_class(xml_stem) if is_layout else None
    binding_field = resource_id_to_binding_field(resource_name) if is_layout else None

    try:
        source_path = str(xml_path.relative_to(res_root.parent))
    except ValueError:
        source_path = str(xml_path)

    tag = _strip_tag(element.tag)
    resource_id = _resource_id(resource_owner, resource_name)
    return ResourceViewDecl(
        resource_id=resource_id,
        resource_name=resource_name,
        resource_kind=_resource_kind(source_type, tag),
        source_type=source_type,
        source_file=xml_path.name,
        source_path=source_path,
        layout_name=xml_stem if is_layout else None,
        menu_name=xml_stem if is_menu else None,
        xml_tag=tag,
        binding_class=binding_class,
        binding_field=binding_field,
        id_decl_kind=f"@{match.group('kind')}",
        line=_line_number_by_id(xml_text, resource_name),
        resource_owner=resource_owner,
        qualified_resource_id=resource_id,
        source_origin=source_origin,
        dependency_coordinate=dependency_coordinate,
        qualifiers=_resource_qualifiers(xml_path.parent.name),
    )


def build_resource_inventory(
    res_root: Path,
    *,
    resource_owner: str | None = None,
    source_origin: str = "app_xml",
    dependency_coordinate: str | None = None,
) -> ResourceInventory:
    res_root = res_root.resolve()
    declarations: list[ResourceViewDecl] = []

    for xml_path in _iter_resource_xml(res_root):
        xml_text = xml_path.read_text(encoding="utf-8")
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"failed to parse Android resource XML: {xml_path}") from exc

        for element in root.iter():
            decl = _decl_from_element(
                element=element,
                xml_path=xml_path,
                res_root=res_root,
                xml_text=xml_text,
                resource_owner=resource_owner,
                source_origin=source_origin,
                dependency_coordinate=dependency_coordinate,
            )
            if decl is not None:
                declarations.append(decl)

    return build_inventory_from_declarations(declarations)


def build_inventory_from_declarations(declarations: list[ResourceViewDecl]) -> ResourceInventory:
    declarations.sort(
        key=lambda decl: (
            decl.resource_id,
            decl.source_type,
            decl.source_file,
            decl.line if decl.line is not None else -1,
        )
    )

    name_counts: dict[str, int] = {}
    for decl in declarations:
        name_counts[decl.resource_name] = name_counts.get(decl.resource_name, 0) + 1

    duplicate_names = {
        name: count for name, count in sorted(name_counts.items()) if count > 1
    }
    summary = {
        "total_declarations": len(declarations),
        "unique_resource_names": len(name_counts),
        "layout_declarations": sum(1 for decl in declarations if decl.source_type == "layout"),
        "menu_declarations": sum(1 for decl in declarations if decl.source_type == "menu"),
        "duplicate_resource_names": len(duplicate_names),
    }
    return ResourceInventory(
        declarations=declarations,
        summary=summary,
        duplicate_resource_names=duplicate_names,
    )


def write_resource_inventory(output_path: Path, inventory: ResourceInventory) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(inventory.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def read_resource_inventory(path: Path) -> ResourceInventory:
    data = json.loads(path.read_text(encoding="utf-8"))
    declarations = [_decl_from_json(item) for item in data.get("declarations", [])]
    return build_inventory_from_declarations(declarations)


def _decl_from_json(data: dict) -> ResourceViewDecl:
    return ResourceViewDecl(
        resource_id=str(data.get("resource_id", "")),
        resource_name=str(data.get("resource_name", "")),
        resource_kind=str(data.get("resource_kind", "")),
        source_type=str(data.get("source_type", "")),
        source_file=str(data.get("source_file", "")),
        source_path=str(data.get("source_path", "")),
        layout_name=_optional_str(data.get("layout_name")),
        menu_name=_optional_str(data.get("menu_name")),
        xml_tag=str(data.get("xml_tag", "")),
        binding_class=_optional_str(data.get("binding_class")),
        binding_field=_optional_str(data.get("binding_field")),
        id_decl_kind=str(data.get("id_decl_kind", "")),
        line=_optional_int(data.get("line")),
        resource_owner=_optional_str(data.get("resource_owner")),
        qualified_resource_id=_optional_str(data.get("qualified_resource_id")),
        source_origin=str(data.get("source_origin", "app_xml")),
        dependency_coordinate=_optional_str(data.get("dependency_coordinate")),
        qualifiers=_optional_str(data.get("qualifiers")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None
