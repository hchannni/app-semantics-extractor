"""Run app-scoped Android static-semantics artifact preparation.

This wrapper standardizes where reusable CPG, Android resource, and
Joern/static-semantics artifacts live. It intentionally delegates parsing and
analysis to the existing Joern frontends and producer chains; the responsibility
here is engine artifact layout, manifest provenance, and safe stage orchestration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Sequence

from app_semantics_kb.static_analysis.android_resources.gradle_artifacts import (
    AndroidResourceArtifacts,
    module_to_dir,
    prepare_resource_artifacts,
    prepare_view_binding_artifacts,
    read_resource_artifacts_manifest,
    write_build_artifacts_manifest,
    write_resource_artifacts_manifest,
)
from app_semantics_kb.static_analysis.android_resources.merged_resources import (
    build_external_resource_inventory_from_merger,
    dependency_class_jars_from_merger,
    materialize_cpg_classpath,
)
from app_semantics_kb.static_analysis.android_resources.resource_inventory import (
    build_inventory_from_declarations,
    build_resource_inventory,
    read_resource_inventory,
    write_resource_inventory,
)
from app_semantics_kb.static_analysis.android_resources.view_binding_fields import (
    build_view_binding_field_inventory_from_manifest,
    write_view_binding_field_inventory,
)
from app_semantics_kb.static_analysis.cli.generate_android_cpg import (
    AndroidCpgPlan,
    DEFAULT_ANDROID_SDK_ROOTS,
    DEFAULT_JOERN_CLI_DIR,
    DEFAULT_SKIPPED_DIRS,
    build_android_cpg_plan,
    generate_android_cpg,
)
from app_semantics_kb.static_analysis.cli.runtime import (
    default_joern_bin,
)
from app_semantics_kb.static_analysis.cli.run_static_semantics_pipeline import (
    run_static_semantics_pipeline,
)


DEFAULT_ARTIFACT_RUNS_ROOT = Path(__file__).resolve().parents[1] / "runs" / "apps"
DEFAULT_ARTIFACT_CPG_EXCLUDES = tuple(
    name for name in (".git", ".gradle", ".idea", "build", "out") if name in DEFAULT_SKIPPED_DIRS
)
DEFAULT_ARTIFACT_CPG_EXCLUDE_REGEX = r"(^|.*/)(build|out)(/|$)"


@dataclass(frozen=True)
class AndroidStaticSemanticsArtifactPaths:
    run_root: str
    cpg_dir: str
    cpg_path: str
    cpg_manifest_path: str
    cpg_classpath_dir: str
    android_resources_dir: str
    resource_artifact_manifest_path: str
    app_resource_inventory_path: str
    external_resource_inventory_path: str
    resource_inventory_path: str
    view_binding_artifact_manifest_path: str
    generated_view_binding_field_types_path: str
    static_semantics_dir: str
    static_semantic_bundle_path: str
    logs_dir: str
    artifact_manifest_path: str


@dataclass(frozen=True)
class AndroidStaticSemanticsArtifactConfig:
    app_id: str
    project_root: Path
    source_root: Path | None = None
    frontend: str = "auto"
    run_id: str | None = None
    runs_root: Path = DEFAULT_ARTIFACT_RUNS_ROOT
    android_main_root: Path | None = None
    module: str = ":app"
    variant: str | None = None
    android_home: Path | None = None
    gradle_timeout_seconds: int = 300
    joern_bin: str = default_joern_bin()
    joern_cli_dir: Path = DEFAULT_JOERN_CLI_DIR
    android_jar: Path | None = None
    auto_android_jar: bool = True
    android_sdk_roots: Sequence[Path] | None = None
    classpath_entries: Sequence[Path] = ()
    inference_jar_entries: Sequence[Path] = ()
    include_java_sources: bool | None = None
    cpg_excludes: Sequence[str] = ()
    cpg_exclude_regex: str | None = None
    include_generated_sources: bool = False
    existing_cpg_path: Path | None = None
    resource_inventory_path: Path | None = None
    auto_resource_inventory: bool = True
    view_binding_field_types_path: Path | None = None
    prepare_all: bool = False
    prepare_gradle_resources: bool = False
    materialize_dependency_classpath: bool = False
    include_external_resources: bool = False
    prepare_view_binding: bool | None = None
    method_cfg_limit: int | None = None
    enable_file_content: bool = False
    dry_run: bool = False
    force: bool = False
    skip_cpg: bool = False
    skip_static_semantics: bool = False


@dataclass(frozen=True)
class AndroidStaticSemanticsArtifactPlan:
    app_id: str
    run_id: str
    project_root: str
    source_root: str
    android_main_root: str
    frontend: str
    module: str
    variant: str | None
    paths: AndroidStaticSemanticsArtifactPaths
    cpg_plan: AndroidCpgPlan | None
    resource_inventory_source: str | None
    resource_inventory_path: str | None
    view_binding_field_types_path: str | None
    stages: dict[str, str]


def _sanitize_app_id(app_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", app_id.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("app_id must contain at least one alphanumeric character")
    return normalized


def _default_run_id(app_id: str) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{timestamp}-{_sanitize_app_id(app_id)}"


def _infer_android_main_root(source_root: Path) -> Path:
    resolved = source_root.expanduser().resolve()
    if (resolved / "res").is_dir():
        return resolved
    if resolved.name in {"java", "kotlin"} and (resolved.parent / "res").is_dir():
        return resolved.parent
    for parent in resolved.parents:
        if parent.name == "main" and (parent / "res").is_dir():
            return parent
    return resolved


def _infer_source_root(project_root: Path) -> Path:
    resolved = project_root.expanduser().resolve()
    app_main = resolved / "app" / "src" / "main"
    if app_main.is_dir():
        return app_main
    main = resolved / "src" / "main"
    if main.is_dir():
        return main
    return resolved


def _build_paths(*, runs_root: Path, app_id: str, run_id: str) -> AndroidStaticSemanticsArtifactPaths:
    safe_app_id = _sanitize_app_id(app_id)
    run_root = runs_root.expanduser().resolve() / safe_app_id / run_id
    cpg_dir = run_root / "cpg"
    cpg_classpath_dir = run_root / "cpg-classpath"
    android_resources_dir = run_root / "android-resources"
    static_semantics_dir = run_root / "static-semantics"
    return AndroidStaticSemanticsArtifactPaths(
        run_root=str(run_root),
        cpg_dir=str(cpg_dir),
        cpg_path=str(cpg_dir / f"{safe_app_id}.cpg"),
        cpg_manifest_path=str(cpg_dir / "cpg-generation-manifest.json"),
        cpg_classpath_dir=str(cpg_classpath_dir),
        android_resources_dir=str(android_resources_dir),
        resource_artifact_manifest_path=str(android_resources_dir / "resource-artifacts.json"),
        app_resource_inventory_path=str(android_resources_dir / "app-resource-view-decls.json"),
        external_resource_inventory_path=str(android_resources_dir / "external-resource-view-decls.json"),
        resource_inventory_path=str(android_resources_dir / "resource-view-decls.json"),
        view_binding_artifact_manifest_path=str(android_resources_dir / "view-binding-artifacts.json"),
        generated_view_binding_field_types_path=str(android_resources_dir / "view-binding-field-types.json"),
        static_semantics_dir=str(static_semantics_dir),
        static_semantic_bundle_path=str(static_semantics_dir / "static_semantic_bundle.json"),
        logs_dir=str(run_root / "logs"),
        artifact_manifest_path=str(run_root / "artifact-manifest.json"),
    )


def _planned_resource_inventory_path(
    *,
    config: AndroidStaticSemanticsArtifactConfig,
    paths: AndroidStaticSemanticsArtifactPaths,
    android_main_root: Path,
) -> tuple[str | None, str | None]:
    if config.resource_inventory_path is not None:
        return (str(config.resource_inventory_path.expanduser().resolve()), "explicit")
    if config.auto_resource_inventory and (android_main_root / "res").is_dir():
        source = "auto:app+external-res" if _include_external_resources(config) else "auto:app-res"
        return (paths.resource_inventory_path, source)
    return (None, None)


def _prepare_gradle_resources(config: AndroidStaticSemanticsArtifactConfig) -> bool:
    return config.prepare_all or config.prepare_gradle_resources


def _materialize_dependency_classpath(config: AndroidStaticSemanticsArtifactConfig) -> bool:
    return config.prepare_all or config.materialize_dependency_classpath


def _dedupe_preserving_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _effective_cpg_excludes(config: AndroidStaticSemanticsArtifactConfig) -> list[str]:
    defaults = () if config.include_generated_sources else DEFAULT_ARTIFACT_CPG_EXCLUDES
    return _dedupe_preserving_order([*defaults, *config.cpg_excludes])


def _effective_cpg_exclude_regex(config: AndroidStaticSemanticsArtifactConfig) -> str | None:
    if config.include_generated_sources:
        return config.cpg_exclude_regex
    if config.cpg_exclude_regex:
        return f"(?:{DEFAULT_ARTIFACT_CPG_EXCLUDE_REGEX})|(?:{config.cpg_exclude_regex})"
    return DEFAULT_ARTIFACT_CPG_EXCLUDE_REGEX


def _include_external_resources(config: AndroidStaticSemanticsArtifactConfig) -> bool:
    return config.prepare_all or config.include_external_resources


def _requires_variant(config: AndroidStaticSemanticsArtifactConfig) -> bool:
    return (
        _prepare_gradle_resources(config)
        or _materialize_dependency_classpath(config)
        or config.prepare_view_binding is True
        or config.prepare_all
    )


def _require_variant(config: AndroidStaticSemanticsArtifactConfig) -> str:
    if not config.variant:
        raise ValueError("--variant is required when Gradle artifact preparation is enabled")
    return config.variant


def _android_home(config: AndroidStaticSemanticsArtifactConfig) -> str | None:
    candidates: list[Path] = []
    if config.android_home is not None:
        candidates.append(config.android_home)
    for env_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value))
    if config.android_sdk_roots is not None:
        candidates.extend(config.android_sdk_roots)
    candidates.extend(DEFAULT_ANDROID_SDK_ROOTS)

    seen: set[str] = set()
    for candidate in candidates:
        expanded = candidate.expanduser()
        key = str(expanded)
        if key in seen:
            continue
        seen.add(key)
        if (expanded / "platforms").is_dir():
            return str(expanded.resolve())
    return None


def _view_binding_likely_enabled(project_root: Path, module: str) -> bool:
    module_dir = module_to_dir(project_root, module)
    candidates = [
        module_dir / "build.gradle.kts",
        module_dir / "build.gradle",
        project_root / "common.gradle",
        project_root / "build.gradle.kts",
        project_root / "build.gradle",
    ]
    patterns = (
        r"\bviewBinding\s*=\s*true\b",
        r"\bviewBinding\s+true\b",
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8")
        if any(re.search(pattern, text) for pattern in patterns):
            return True
    return False


def _should_prepare_view_binding(
    config: AndroidStaticSemanticsArtifactConfig,
    project_root: Path,
) -> bool:
    if config.view_binding_field_types_path is not None:
        return False
    if config.prepare_view_binding is not None:
        return config.prepare_view_binding
    return config.prepare_all and _view_binding_likely_enabled(project_root, config.module)


def build_android_static_semantics_artifact_plan(config: AndroidStaticSemanticsArtifactConfig) -> AndroidStaticSemanticsArtifactPlan:
    app_id = _sanitize_app_id(config.app_id)
    run_id = config.run_id or _default_run_id(app_id)
    project_root = config.project_root.expanduser().resolve()
    if _requires_variant(config):
        _require_variant(config)
    source_root = (
        config.source_root.expanduser().resolve()
        if config.source_root is not None
        else _infer_source_root(project_root)
    )
    android_main_root = (
        config.android_main_root.expanduser().resolve()
        if config.android_main_root is not None
        else _infer_android_main_root(source_root)
    )
    paths = _build_paths(runs_root=config.runs_root, app_id=app_id, run_id=run_id)
    cpg_output = (
        config.existing_cpg_path.expanduser().resolve()
        if config.existing_cpg_path is not None
        else Path(paths.cpg_path)
    )

    cpg_plan = None
    if not config.skip_cpg and config.existing_cpg_path is None:
        cpg_plan = build_android_cpg_plan(
            source_root=source_root,
            output_path=cpg_output,
            frontend=config.frontend,
            joern_cli_dir=config.joern_cli_dir,
            android_jar=config.android_jar,
            auto_android_jar=config.auto_android_jar,
            android_sdk_roots=config.android_sdk_roots,
            classpath_entries=config.classpath_entries,
            inference_jar_entries=config.inference_jar_entries,
            include_java_sources=config.include_java_sources,
            excludes=_effective_cpg_excludes(config),
            exclude_regex=_effective_cpg_exclude_regex(config),
            enable_file_content=config.enable_file_content,
        )
    effective_frontend = cpg_plan.frontend if cpg_plan is not None else config.frontend

    resource_inventory_path, resource_inventory_source = _planned_resource_inventory_path(
        config=config,
        paths=paths,
        android_main_root=android_main_root,
    )

    stages = {
        "cpg": (
            "external"
            if config.existing_cpg_path is not None
            else "skipped"
            if config.skip_cpg
            else "planned"
        ),
        "resource_inventory": "planned" if resource_inventory_source and resource_inventory_source.startswith("auto:") else (
            "external" if resource_inventory_source == "explicit" else "skipped"
        ),
        "gradle_resources": "planned" if _prepare_gradle_resources(config) else "skipped",
        "dependency_classpath": "planned" if _materialize_dependency_classpath(config) else "skipped",
        "external_resource_inventory": "planned" if _include_external_resources(config) else "skipped",
        "view_binding_field_types": (
            "planned" if _should_prepare_view_binding(config, project_root) else "skipped"
        ),
        "static_semantics": "skipped" if config.skip_static_semantics else "planned",
    }

    return AndroidStaticSemanticsArtifactPlan(
        app_id=app_id,
        run_id=run_id,
        project_root=str(project_root),
        source_root=str(source_root),
        android_main_root=str(android_main_root),
        frontend=effective_frontend,
        module=config.module,
        variant=config.variant,
        paths=paths,
        cpg_plan=cpg_plan,
        resource_inventory_source=resource_inventory_source,
        resource_inventory_path=resource_inventory_path,
        view_binding_field_types_path=(
            str(config.view_binding_field_types_path.expanduser().resolve())
            if config.view_binding_field_types_path
            else None
        ),
        stages=stages,
    )


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _write_app_resource_inventory(plan: AndroidStaticSemanticsArtifactPlan, output_path: Path) -> Path:
    res_root = Path(plan.android_main_root) / "res"
    inventory = build_resource_inventory(res_root, source_origin="app_xml")
    return write_resource_inventory(output_path, inventory)


def _write_resource_inventory_if_needed(plan: AndroidStaticSemanticsArtifactPlan, *, dry_run: bool) -> None:
    if not plan.resource_inventory_source or not plan.resource_inventory_source.startswith("auto:"):
        return
    if plan.resource_inventory_path is None:
        return
    if dry_run:
        return
    if plan.resource_inventory_source == "auto:app-res":
        _write_app_resource_inventory(plan, Path(plan.resource_inventory_path))
        return

    app_inventory_path = _write_app_resource_inventory(
        plan,
        Path(plan.paths.app_resource_inventory_path),
    )
    resource_artifact_manifest = Path(plan.paths.resource_artifact_manifest_path)
    merger_xml = None
    if resource_artifact_manifest.is_file():
        artifacts = read_resource_artifacts_manifest(resource_artifact_manifest)
        merger_xml = Path(artifacts.merger_xml) if artifacts.merger_xml else None

    if merger_xml and merger_xml.is_file():
        external = build_external_resource_inventory_from_merger(merger_xml)
        external_path = write_resource_inventory(Path(plan.paths.external_resource_inventory_path), external)
        declarations = []
        declarations.extend(read_resource_inventory(app_inventory_path).declarations)
        declarations.extend(read_resource_inventory(external_path).declarations)
        merged = build_inventory_from_declarations(declarations)
        write_resource_inventory(Path(plan.resource_inventory_path), merged)
    else:
        _write_app_resource_inventory(plan, Path(plan.resource_inventory_path))


def _prepare_gradle_resource_artifacts_if_needed(
    config: AndroidStaticSemanticsArtifactConfig,
    paths: AndroidStaticSemanticsArtifactPaths,
) -> AndroidResourceArtifacts | None:
    manifest_path = Path(paths.resource_artifact_manifest_path)
    if not _prepare_gradle_resources(config):
        return read_resource_artifacts_manifest(manifest_path) if manifest_path.is_file() else None
    if config.dry_run:
        return None

    artifacts = prepare_resource_artifacts(
        project_root=config.project_root,
        module=config.module,
        variant=_require_variant(config),
        android_home=_android_home(config),
        timeout_seconds=config.gradle_timeout_seconds,
    )
    write_resource_artifacts_manifest(manifest_path, artifacts)
    if artifacts.status != "generated":
        raise RuntimeError(f"resource artifact preparation failed: {artifacts.message}")
    return artifacts


def _materialize_dependency_classpath_if_needed(
    config: AndroidStaticSemanticsArtifactConfig,
    paths: AndroidStaticSemanticsArtifactPaths,
    artifacts: AndroidResourceArtifacts | None,
) -> Path | None:
    if not _materialize_dependency_classpath(config):
        return None
    if config.dry_run:
        return Path(paths.cpg_classpath_dir)

    manifest_path = Path(paths.resource_artifact_manifest_path)
    if artifacts is None and manifest_path.is_file():
        artifacts = read_resource_artifacts_manifest(manifest_path)
    if artifacts is None:
        raise RuntimeError("dependency classpath materialization requires resource artifacts")

    merger_xml = Path(artifacts.merger_xml) if artifacts.merger_xml else None
    dependency_jars = dependency_class_jars_from_merger(merger_xml) if merger_xml else []
    copied = materialize_cpg_classpath(
        output_dir=Path(paths.cpg_classpath_dir),
        android_jar=None,
        r_jars=[Path(path) for path in artifacts.r_jars],
        dependency_jars=dependency_jars,
    )
    if not copied:
        raise RuntimeError("dependency classpath materialization produced no jars")
    return Path(paths.cpg_classpath_dir)


def _prepare_view_binding_if_needed(
    config: AndroidStaticSemanticsArtifactConfig,
    plan: AndroidStaticSemanticsArtifactPlan,
) -> Path | None:
    project_root = Path(plan.project_root)
    if not _should_prepare_view_binding(config, project_root):
        return None
    if config.dry_run:
        return Path(plan.paths.generated_view_binding_field_types_path)

    artifacts = prepare_view_binding_artifacts(
        project_root=project_root,
        module=config.module,
        variant=_require_variant(config),
        android_home=_android_home(config),
        timeout_seconds=config.gradle_timeout_seconds,
    )
    manifest_path = Path(plan.paths.view_binding_artifact_manifest_path)
    write_build_artifacts_manifest(manifest_path, artifacts)
    if artifacts.status != "generated":
        raise RuntimeError(f"ViewBinding artifact preparation failed: {artifacts.message}")
    inventory = build_view_binding_field_inventory_from_manifest(manifest_path)
    return write_view_binding_field_inventory(
        Path(plan.paths.generated_view_binding_field_types_path),
        inventory,
    )


def _rebuild_plan_with_generated_inputs(
    config: AndroidStaticSemanticsArtifactConfig,
    plan: AndroidStaticSemanticsArtifactPlan,
    *,
    generated_classpath_dir: Path | None,
    generated_view_binding_field_types: Path | None,
) -> AndroidStaticSemanticsArtifactPlan:
    classpath_entries = list(config.classpath_entries)
    if generated_classpath_dir is not None and not config.dry_run:
        classpath_entries.append(generated_classpath_dir)

    cpg_plan = None
    if not config.skip_cpg and config.existing_cpg_path is None:
        cpg_plan = build_android_cpg_plan(
            source_root=Path(plan.source_root),
            output_path=Path(plan.paths.cpg_path),
            frontend=config.frontend,
            joern_cli_dir=config.joern_cli_dir,
            android_jar=config.android_jar,
            auto_android_jar=config.auto_android_jar,
            android_sdk_roots=config.android_sdk_roots,
            classpath_entries=classpath_entries,
            inference_jar_entries=config.inference_jar_entries,
            include_java_sources=config.include_java_sources,
            excludes=_effective_cpg_excludes(config),
            exclude_regex=_effective_cpg_exclude_regex(config),
            enable_file_content=config.enable_file_content,
        )

    view_binding_path = plan.view_binding_field_types_path
    if generated_view_binding_field_types is not None:
        view_binding_path = str(
            generated_view_binding_field_types
            if config.dry_run
            else generated_view_binding_field_types.resolve()
        )

    stages = dict(plan.stages)
    stages["view_binding_field_types"] = (
        "planned" if generated_view_binding_field_types is not None else stages["view_binding_field_types"]
    )

    return replace(
        plan,
        cpg_plan=cpg_plan,
        frontend=cpg_plan.frontend if cpg_plan is not None else plan.frontend,
        view_binding_field_types_path=view_binding_path,
        stages=stages,
    )


def run_android_static_semantics_artifacts(config: AndroidStaticSemanticsArtifactConfig) -> AndroidStaticSemanticsArtifactPlan:
    plan = build_android_static_semantics_artifact_plan(config)
    paths = plan.paths

    for directory in (
        paths.cpg_dir,
        paths.cpg_classpath_dir,
        paths.android_resources_dir,
        paths.static_semantics_dir,
        paths.logs_dir,
    ):
        if not config.dry_run:
            Path(directory).mkdir(parents=True, exist_ok=True)

    resource_artifacts = _prepare_gradle_resource_artifacts_if_needed(config, paths)
    generated_classpath = _materialize_dependency_classpath_if_needed(
        config,
        paths,
        resource_artifacts,
    )
    _write_resource_inventory_if_needed(plan, dry_run=config.dry_run)
    generated_view_binding = _prepare_view_binding_if_needed(config, plan)
    plan = _rebuild_plan_with_generated_inputs(
        config,
        plan,
        generated_classpath_dir=generated_classpath,
        generated_view_binding_field_types=generated_view_binding,
    )

    if plan.cpg_plan is not None:
        generate_android_cpg(
            plan=plan.cpg_plan,
            manifest_path=Path(paths.cpg_manifest_path),
            dry_run=config.dry_run,
            force=config.force,
        )

    if not config.dry_run and not config.skip_static_semantics:
        cpg_path = (
            str(config.existing_cpg_path.expanduser().resolve())
            if config.existing_cpg_path is not None
            else paths.cpg_path
        )
        run_static_semantics_pipeline(
            frontend=plan.frontend if plan.frontend in {"kotlin", "java"} else (
                plan.cpg_plan.frontend if plan.cpg_plan is not None else "kotlin"
            ),
            joern_bin=config.joern_bin,
            cpg_path=cpg_path,
            source_path=plan.source_root,
            run_id=f"{plan.run_id}:static",
            resource_inventory_path=plan.resource_inventory_path,
            view_binding_field_types_path=plan.view_binding_field_types_path,
            method_cfg_limit=config.method_cfg_limit,
            output_root=Path(paths.static_semantics_dir),
            bundle_output_path=Path(paths.static_semantic_bundle_path),
        )

    manifest = asdict(plan)
    manifest["dry_run"] = config.dry_run
    manifest["force"] = config.force
    manifest["skip_cpg"] = config.skip_cpg
    manifest["skip_static_semantics"] = config.skip_static_semantics
    manifest["method_cfg_limit"] = config.method_cfg_limit
    manifest["cpg_excludes"] = _effective_cpg_excludes(config)
    manifest["cpg_exclude_regex"] = _effective_cpg_exclude_regex(config)
    manifest["include_generated_sources"] = config.include_generated_sources
    _write_json(Path(paths.artifact_manifest_path), manifest)
    return plan


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare an app-scoped Android static-semantics artifact run",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--source-root", default=None)
    parser.add_argument("--android-main-root", default=None)
    parser.add_argument("--module", default=":app")
    parser.add_argument("--variant", default=None)
    parser.add_argument("--android-home", default=None)
    parser.add_argument("--gradle-timeout-seconds", type=int, default=300)
    parser.add_argument("--frontend", choices=["auto", "kotlin", "java"], default="auto")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-root", default=str(DEFAULT_ARTIFACT_RUNS_ROOT))
    parser.add_argument("--joern-bin", default=default_joern_bin())
    parser.add_argument("--joern-cli-dir", default=str(DEFAULT_JOERN_CLI_DIR))
    parser.add_argument("--cpg-path", default=None, help="Use an existing CPG instead of generating one")
    parser.add_argument("--android-jar", default=None)
    parser.add_argument("--android-sdk-root", action="append", default=[])
    parser.add_argument("--no-auto-android-jar", dest="auto_android_jar", action="store_false", default=True)
    parser.add_argument("--classpath", action="append", default=[])
    parser.add_argument("--dependency-jar", action="append", default=[])
    parser.add_argument("--inference-jar-path", action="append", default=[])
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Additional path segment to exclude from Joern CPG generation",
    )
    parser.add_argument(
        "--exclude-regex",
        default=None,
        help="Regex passed through to Joern CPG generation for source exclusion",
    )
    parser.add_argument(
        "--include-generated-sources",
        action="store_true",
        help="Do not apply the artifact wrapper's default generated/build source exclusions",
    )
    java_group = parser.add_mutually_exclusive_group()
    java_group.add_argument("--include-java-sources", action="store_true", default=None)
    java_group.add_argument("--no-include-java-sources", dest="include_java_sources", action="store_false")
    parser.add_argument("--resource-inventory", default=None)
    parser.add_argument("--no-auto-resource-inventory", dest="auto_resource_inventory", action="store_false", default=True)
    parser.add_argument("--view-binding-field-types", default=None)
    parser.add_argument("--prepare-all", action="store_true")
    parser.add_argument("--prepare-gradle-resources", action="store_true")
    parser.add_argument("--materialize-dependency-classpath", action="store_true")
    parser.add_argument("--include-external-resources", action="store_true")
    view_binding_group = parser.add_mutually_exclusive_group()
    view_binding_group.add_argument("--prepare-view-binding", dest="prepare_view_binding", action="store_true", default=None)
    view_binding_group.add_argument("--no-prepare-view-binding", dest="prepare_view_binding", action="store_false")
    parser.add_argument("--method-cfg-limit", type=int, default=None)
    parser.add_argument("--enable-file-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-cpg", action="store_true")
    parser.add_argument("--skip-static-semantics", action="store_true")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    config = AndroidStaticSemanticsArtifactConfig(
        app_id=args.app_id,
        project_root=Path(args.project_root),
        source_root=Path(args.source_root) if args.source_root else None,
        android_main_root=Path(args.android_main_root) if args.android_main_root else None,
        module=args.module,
        variant=args.variant,
        android_home=Path(args.android_home) if args.android_home else None,
        gradle_timeout_seconds=args.gradle_timeout_seconds,
        frontend=args.frontend,
        run_id=args.run_id,
        runs_root=Path(args.runs_root),
        joern_bin=args.joern_bin,
        joern_cli_dir=Path(args.joern_cli_dir),
        existing_cpg_path=Path(args.cpg_path) if args.cpg_path else None,
        android_jar=Path(args.android_jar) if args.android_jar else None,
        auto_android_jar=args.auto_android_jar,
        android_sdk_roots=[Path(entry) for entry in args.android_sdk_root] or None,
        classpath_entries=[Path(entry) for entry in [*args.classpath, *args.dependency_jar]],
        inference_jar_entries=[Path(entry) for entry in args.inference_jar_path],
        include_java_sources=args.include_java_sources,
        cpg_excludes=args.exclude,
        cpg_exclude_regex=args.exclude_regex,
        include_generated_sources=args.include_generated_sources,
        resource_inventory_path=Path(args.resource_inventory) if args.resource_inventory else None,
        auto_resource_inventory=args.auto_resource_inventory,
        view_binding_field_types_path=Path(args.view_binding_field_types) if args.view_binding_field_types else None,
        prepare_all=args.prepare_all,
        prepare_gradle_resources=args.prepare_gradle_resources,
        materialize_dependency_classpath=args.materialize_dependency_classpath,
        include_external_resources=args.include_external_resources,
        prepare_view_binding=args.prepare_view_binding,
        method_cfg_limit=args.method_cfg_limit,
        enable_file_content=args.enable_file_content,
        dry_run=args.dry_run,
        force=args.force,
        skip_cpg=args.skip_cpg,
        skip_static_semantics=args.skip_static_semantics,
    )
    plan = run_android_static_semantics_artifacts(config)
    print(json.dumps(asdict(plan), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
