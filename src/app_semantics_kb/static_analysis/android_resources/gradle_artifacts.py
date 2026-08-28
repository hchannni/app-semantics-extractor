"""Prepare Android Gradle artifacts used by static-semantics producers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping


@dataclass(frozen=True)
class AndroidBuildArtifacts:
    status: str
    project_root: str
    module: str
    variant: str
    gradle_task: str
    generated_source_dir: str | None
    generated_class_dir: str | None
    r_jar: str | None
    return_code: int | None
    message: str

    def to_json_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AndroidResourceArtifacts:
    status: str
    project_root: str
    module: str
    variant: str
    gradle_task: str
    merge_task: str
    merger_xml: str | None
    r_jars: list[str]
    runtime_symbol_lists: list[str]
    package_aware_symbol_lists: list[str]
    return_code: int | None
    message: str

    def to_json_dict(self) -> dict:
        return asdict(self)


def variant_to_pascal(variant: str) -> str:
    """Convert a Gradle variant such as coreDebug to CoreDebug."""
    if not variant:
        raise ValueError("variant must not be empty")

    parts: list[str] = []
    current = variant[0]
    for char in variant[1:]:
        if char.isupper() and current:
            parts.append(current)
            current = char
        else:
            current += char
    parts.append(current)
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def module_to_dir(project_root: Path, module: str) -> Path:
    module_path = module.strip(":")
    if not module_path:
        return project_root
    return project_root.joinpath(*module_path.split(":"))


def view_binding_task(module: str, variant: str) -> str:
    task_name = f"dataBindingGenBaseClasses{variant_to_pascal(variant)}"
    normalized_module = module.rstrip(":")
    if not normalized_module or normalized_module == ":":
        return f":{task_name}"
    if not normalized_module.startswith(":"):
        normalized_module = f":{normalized_module}"
    return f"{normalized_module}:{task_name}"


def resource_merge_task(module: str, variant: str) -> str:
    return _module_task(module, f"merge{variant_to_pascal(variant)}Resources")


def resource_process_task(module: str, variant: str) -> str:
    return _module_task(module, f"process{variant_to_pascal(variant)}Resources")


def _module_task(module: str, task_name: str) -> str:
    normalized_module = module.rstrip(":")
    if not normalized_module or normalized_module == ":":
        return f":{task_name}"
    if not normalized_module.startswith(":"):
        normalized_module = f":{normalized_module}"
    return f"{normalized_module}:{task_name}"


def _existing_path(path: Path) -> str | None:
    return str(path) if path.exists() else None


def _artifact_paths(project_root: Path, module: str, variant: str) -> tuple[Path, Path, Path]:
    module_dir = module_to_dir(project_root, module)
    variant_pascal = variant_to_pascal(variant)
    source_dir = (
        module_dir
        / "build"
        / "generated"
        / "data_binding_base_class_source_out"
        / variant
        / "out"
    )
    class_dir = (
        module_dir
        / "build"
        / "intermediates"
        / "javac"
        / variant
        / f"compile{variant_pascal}JavaWithJavac"
        / "classes"
    )
    r_jar = (
        module_dir
        / "build"
        / "intermediates"
        / "compile_r_class_jar"
        / variant
        / f"generate{variant_pascal}RFile"
        / "R.jar"
    )
    return source_dir, class_dir, r_jar


def _resource_artifact_paths(project_root: Path, module: str, variant: str) -> tuple[Path, list[Path], list[Path], list[Path]]:
    module_dir = module_to_dir(project_root, module)
    variant_pascal = variant_to_pascal(variant)
    merger_xml = (
        module_dir
        / "build"
        / "intermediates"
        / "incremental"
        / variant
        / f"merge{variant_pascal}Resources"
        / "merger.xml"
    )
    intermediates = module_dir / "build" / "intermediates"
    r_jars = _find_intermediate_files(intermediates, variant, "R.jar")
    runtime_symbols = _find_intermediate_files(intermediates, variant, "R.txt")
    package_symbols = _find_intermediate_files(intermediates, variant, "package-aware-r.txt")
    return merger_xml, r_jars, runtime_symbols, package_symbols


def _find_intermediate_files(intermediates: Path, variant: str, filename: str) -> list[Path]:
    if not intermediates.exists():
        return []
    return sorted(
        path
        for path in intermediates.glob(f"**/{filename}")
        if variant in path.parts
    )


def _run_gradle(
    *,
    project_root: Path,
    task: str,
    android_home: str | None,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if android_home:
        env["ANDROID_HOME"] = android_home
    return subprocess.run(
        ["./gradlew", task],
        cwd=project_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )


def prepare_view_binding_artifacts(
    *,
    project_root: Path,
    module: str,
    variant: str,
    android_home: str | None = None,
    timeout_seconds: int = 300,
) -> AndroidBuildArtifacts:
    project_root = project_root.resolve()
    task = view_binding_task(module, variant)
    gradlew = project_root / "gradlew"
    source_dir, class_dir, r_jar = _artifact_paths(project_root, module, variant)

    if not gradlew.exists():
        return AndroidBuildArtifacts(
            status="missing_gradle",
            project_root=str(project_root),
            module=module,
            variant=variant,
            gradle_task=task,
            generated_source_dir=_existing_path(source_dir),
            generated_class_dir=_existing_path(class_dir),
            r_jar=_existing_path(r_jar),
            return_code=None,
            message=f"Gradle wrapper not found: {gradlew}",
        )

    try:
        result = _run_gradle(
            project_root=project_root,
            task=task,
            android_home=android_home,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return _manifest_for_failure(project_root, module, variant, task, source_dir, class_dir, r_jar, exc)

    status = "generated" if result.returncode == 0 and source_dir.exists() else "task_failed"
    message = "generated ViewBinding base classes" if status == "generated" else _tail(result.stdout)
    return AndroidBuildArtifacts(
        status=status,
        project_root=str(project_root),
        module=module,
        variant=variant,
        gradle_task=task,
        generated_source_dir=_existing_path(source_dir),
        generated_class_dir=_existing_path(class_dir),
        r_jar=_existing_path(r_jar),
        return_code=result.returncode,
        message=message,
    )


def prepare_resource_artifacts(
    *,
    project_root: Path,
    module: str,
    variant: str,
    android_home: str | None = None,
    timeout_seconds: int = 300,
) -> AndroidResourceArtifacts:
    project_root = project_root.resolve()
    task = resource_process_task(module, variant)
    merge_task_name = resource_merge_task(module, variant)
    gradlew = project_root / "gradlew"

    if not gradlew.exists():
        return _resource_artifacts_result(
            project_root, module, variant, task, merge_task_name, None, "missing_gradle",
            f"Gradle wrapper not found: {gradlew}",
        )

    try:
        result = _run_gradle(
            project_root=project_root,
            task=task,
            android_home=android_home,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        message = f"Gradle task timed out after {exc.timeout} seconds"
        return _resource_artifacts_result(project_root, module, variant, task, merge_task_name, None, "task_failed", message)

    merger_xml, r_jars, runtime_symbols, package_symbols = _resource_artifact_paths(project_root, module, variant)
    status = "generated" if result.returncode == 0 and merger_xml.exists() else "task_failed"
    message = "generated Android resource merge artifacts" if status == "generated" else _tail(result.stdout)
    return AndroidResourceArtifacts(
        status=status,
        project_root=str(project_root),
        module=module,
        variant=variant,
        gradle_task=task,
        merge_task=merge_task_name,
        merger_xml=_existing_path(merger_xml),
        r_jars=[str(path) for path in r_jars],
        runtime_symbol_lists=[str(path) for path in runtime_symbols],
        package_aware_symbol_lists=[str(path) for path in package_symbols],
        return_code=result.returncode,
        message=message,
    )


def _resource_artifacts_result(
    project_root: Path,
    module: str,
    variant: str,
    task: str,
    merge_task_name: str,
    return_code: int | None,
    status: str,
    message: str,
) -> AndroidResourceArtifacts:
    merger_xml, r_jars, runtime_symbols, package_symbols = _resource_artifact_paths(project_root, module, variant)
    return AndroidResourceArtifacts(
        status=status,
        project_root=str(project_root),
        module=module,
        variant=variant,
        gradle_task=task,
        merge_task=merge_task_name,
        merger_xml=_existing_path(merger_xml),
        r_jars=[str(path) for path in r_jars],
        runtime_symbol_lists=[str(path) for path in runtime_symbols],
        package_aware_symbol_lists=[str(path) for path in package_symbols],
        return_code=return_code,
        message=message,
    )


def _manifest_for_failure(
    project_root: Path,
    module: str,
    variant: str,
    task: str,
    source_dir: Path,
    class_dir: Path,
    r_jar: Path,
    exc: subprocess.TimeoutExpired,
) -> AndroidBuildArtifacts:
    return AndroidBuildArtifacts(
        status="task_failed",
        project_root=str(project_root),
        module=module,
        variant=variant,
        gradle_task=task,
        generated_source_dir=_existing_path(source_dir),
        generated_class_dir=_existing_path(class_dir),
        r_jar=_existing_path(r_jar),
        return_code=None,
        message=f"Gradle task timed out after {exc.timeout} seconds",
    )


def _tail(text: str, max_lines: int = 30) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


def write_build_artifacts_manifest(output_path: Path, artifacts: AndroidBuildArtifacts) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifacts.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def write_resource_artifacts_manifest(output_path: Path, artifacts: AndroidResourceArtifacts) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifacts.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def read_resource_artifacts_manifest(path: Path) -> AndroidResourceArtifacts:
    data: Mapping[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return AndroidResourceArtifacts(
        status=str(data.get("status", "")),
        project_root=str(data.get("project_root", "")),
        module=str(data.get("module", "")),
        variant=str(data.get("variant", "")),
        gradle_task=str(data.get("gradle_task", "")),
        merge_task=str(data.get("merge_task", "")),
        merger_xml=_optional_str(data.get("merger_xml")),
        r_jars=_optional_str_list(data.get("r_jars")),
        runtime_symbol_lists=_optional_str_list(data.get("runtime_symbol_lists")),
        package_aware_symbol_lists=_optional_str_list(data.get("package_aware_symbol_lists")),
        return_code=_optional_int(data.get("return_code")),
        message=str(data.get("message", "")),
    )


def read_build_artifacts_manifest(path: Path) -> AndroidBuildArtifacts:
    data: Mapping[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return AndroidBuildArtifacts(
        status=str(data.get("status", "")),
        project_root=str(data.get("project_root", "")),
        module=str(data.get("module", "")),
        variant=str(data.get("variant", "")),
        gradle_task=str(data.get("gradle_task", "")),
        generated_source_dir=_optional_str(data.get("generated_source_dir")),
        generated_class_dir=_optional_str(data.get("generated_class_dir")),
        r_jar=_optional_str(data.get("r_jar")),
        return_code=_optional_int(data.get("return_code")),
        message=str(data.get("message", "")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _optional_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]
