"""Read Android Gradle merged resource artifacts for external view declarations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import shutil
from pathlib import Path
import xml.etree.ElementTree as ET

from .resource_inventory import (
    ResourceInventory,
    ResourceViewDecl,
    build_inventory_from_declarations,
    build_resource_inventory,
)


@dataclass(frozen=True)
class DependencyResourceRoot:
    coordinate: str
    res_root: str
    package_name: str | None


def parse_dependency_resource_roots(merger_xml: Path) -> list[DependencyResourceRoot]:
    root = ET.parse(merger_xml).getroot()
    seen: set[tuple[str, str]] = set()
    resources: list[DependencyResourceRoot] = []
    for data_set in root.findall("dataSet"):
        if data_set.attrib.get("from-dependency") != "true":
            continue
        coordinate = data_set.attrib.get("config", "")
        for source in data_set.findall("source"):
            res_root = Path(source.attrib.get("path", ""))
            if not res_root.exists():
                continue
            key = (coordinate, str(res_root.resolve()))
            if key in seen:
                continue
            seen.add(key)
            resources.append(
                DependencyResourceRoot(
                    coordinate=coordinate,
                    res_root=str(res_root.resolve()),
                    package_name=android_manifest_package(res_root),
                )
            )
    return resources


def android_manifest_package(res_root: Path) -> str | None:
    manifest = res_root.parent / "AndroidManifest.xml"
    if not manifest.exists():
        return None
    try:
        return ET.parse(manifest).getroot().attrib.get("package")
    except ET.ParseError:
        return None


def build_external_resource_inventory_from_merger(merger_xml: Path) -> ResourceInventory:
    declarations: list[ResourceViewDecl] = []
    for dependency in parse_dependency_resource_roots(merger_xml):
        inventory = build_resource_inventory(
            Path(dependency.res_root),
            resource_owner=dependency.package_name,
            source_origin="external_dependency",
            dependency_coordinate=dependency.coordinate,
        )
        declarations.extend(inventory.declarations)
    return build_inventory_from_declarations(declarations)


def dependency_class_jars_from_merger(merger_xml: Path) -> list[Path]:
    jars: list[Path] = []
    seen: set[Path] = set()
    for dependency in parse_dependency_resource_roots(merger_xml):
        classes_jar = Path(dependency.res_root).parent / "jars" / "classes.jar"
        if classes_jar.exists() and classes_jar not in seen:
            seen.add(classes_jar)
            jars.append(classes_jar)
    return sorted(jars)


def materialize_cpg_classpath(
    *,
    output_dir: Path,
    android_jar: Path | None,
    r_jars: list[Path],
    dependency_jars: list[Path],
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for jar in [android_jar, *r_jars, *dependency_jars]:
        if jar is None or not jar.exists():
            continue
        target = output_dir / _stable_jar_name(jar)
        shutil.copy2(jar, target)
        copied.append(target)
    return copied


def _stable_jar_name(jar: Path) -> str:
    digest = hashlib.sha1(str(jar.resolve()).encode("utf-8")).hexdigest()[:10]
    stem = jar.stem.replace("classes", jar.parent.parent.name if jar.parent.name == "jars" else jar.stem)
    return f"{stem}-{digest}.jar"
