"""Generate a Joern CPG for Android Java/Kotlin source trees.

Android CPG quality depends heavily on the symbols that the Joern frontend can
resolve while parsing source. For this project we treat dependencies as
type-resolution evidence, not as extra app source:

* android.jar should come from the target compileSdk platform. It teaches Joern
  about Activity/View/TextView/etc. without adding Android framework code to the
  app CPG. This CLI auto-detects it from Gradle compileSdk when possible, but
  passing --android-jar is still the most explicit form.
* AndroidX/Material/third-party libraries should be passed as dependency jars
  when their types matter to static semantics. Common examples are appcompat,
  core, fragment, recyclerview, constraintlayout, lifecycle, navigation,
  preference, and com.google.android.material:material.
* Android libraries are normally distributed as AARs. AARs are ZIP archives with
  classes.jar, resources, manifests, and metadata. Joern frontends need the
  class symbols, so pass classes.jar extracted from each AAR, or a jar directory
  materialized by our Gradle helpers. Do not pass raw .aar files unless you are
  intentionally using --allow-aar-classpath for a debugging run.
* Generated app artifacts often matter as much as library jars:
  - R.jar / dependency R jars resolve R.id, R.layout, and related generated
    resource symbols used by findViewById, menu APIs, and XML-backed anchors.
  - ViewBinding/DataBinding generated classes resolve ActivityFooBinding,
    FragmentBarBinding, binding.title, holder.binding.rowTitle, and similar
    binding-field chains.
  - BuildConfig jars are usually less important for UI anchors, but can help
    when app code gates flows through generated constants.
* Dependency node generation is intentionally opt-in. It can improve call paths
  through library internals, but it also makes the graph larger and noisier. For
  UI -> semantic-flow research, precise app-source anchors plus type resolution
  are usually better than importing every dependency body into the graph.

Practical rule for future Codex sessions:
  source_root = app source only
  android.jar = Android framework symbols
  --dependency-jar/--classpath = AndroidX, Material, app R.jar, ViewBinding,
      DataBinding, BuildConfig, and third-party classes.jar files
  raw .aar = extract classes.jar first
  XML resources = handled by the static-semantics artifact wrapper, not by CPG
      classpath

Reference CPG generation command shapes:

Java app:
  python3 -m app_semantics_kb.static_analysis.cli.generate_android_cpg \
    --source-root samples/markor/app/src/main/java \
    --frontend java \
    --output /path/to/markor.cpg \
    --dependency-jar /path/to/app-r.jar \
    --dependency-jar /path/to/androidx-and-material-classes-jars/

Kotlin or Kotlin+Java app:
  python3 -m app_semantics_kb.static_analysis.cli.generate_android_cpg \
    --source-root samples/Calendar/app/src/main \
    --frontend kotlin \
    --output /path/to/calendar.cpg \
    --dependency-jar /path/to/app-r.jar \
    --dependency-jar /path/to/androidx-and-material-classes-jars/

Replace /path/to/app-r.jar with the app/generated R.jar for the chosen variant.
Replace /path/to/androidx-and-material-classes-jars/ with a directory of
extracted classes.jar files for AndroidX, Material, ViewBinding/DataBinding, and
other third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_JOERN_CLI_DIR = Path(os.environ.get("JOERN_CLI_DIR", "joern-cli"))
DEFAULT_SKIPPED_DIRS = frozenset({".git", ".gradle", ".idea", "build", "out"})
SUPPORTED_CLASSPATH_SUFFIXES = frozenset({".jar"})
DEFAULT_ANDROID_SDK_ROOTS = (
    Path("~/Library/Android/sdk"),
    Path("~/Android/Sdk"),
    Path("/opt/android-sdk"),
    Path("/usr/local/share/android-sdk"),
)


@dataclass(frozen=True)
class SourceLanguageSummary:
    source_root: str
    kotlin_files: int
    java_files: int


@dataclass(frozen=True)
class AndroidCpgPlan:
    source_root: str
    output_path: str
    frontend: str
    include_java_sources: bool
    kotlin_files: int
    java_files: int
    android_jar: str | None
    android_jar_source: str | None
    classpath: list[str]
    inference_jar_paths: list[str]
    dependency_nodes_requested: bool
    warnings: list[str]
    command: list[str]


@dataclass(frozen=True)
class AndroidCpgResult:
    plan: AndroidCpgPlan
    manifest_path: str | None
    returncode: int | None


@dataclass(frozen=True)
class AndroidJarResolution:
    path: Path | None
    source: str | None
    warnings: list[str]


def _resolve_existing_path(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.exists():
        raise FileNotFoundError(f"{label} does not exist: {expanded}")
    return expanded.resolve()


def _require_jar_file(path: Path, *, label: str, allow_aar_classpath: bool = False) -> Path:
    resolved = _resolve_existing_path(path, label=label)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a file: {resolved}")
    suffix = resolved.suffix.lower()
    if suffix == ".aar" and not allow_aar_classpath:
        raise ValueError(
            f"{label} is an Android AAR, not a Joern classpath jar: {resolved}. "
            "Extract classes.jar first or use materialize_android_cpg_classpath.py."
        )
    if suffix not in SUPPORTED_CLASSPATH_SUFFIXES and not allow_aar_classpath:
        raise ValueError(f"{label} must be a .jar file: {resolved}")
    return resolved


def _is_skipped(path: Path, source_root: Path, skipped_dirs: set[str]) -> bool:
    try:
        relative = path.relative_to(source_root)
    except ValueError:
        return False
    return any(part in skipped_dirs for part in relative.parts)


def detect_source_languages(
    source_root: Path,
    *,
    skipped_dirs: Iterable[str] = DEFAULT_SKIPPED_DIRS,
) -> SourceLanguageSummary:
    root = _resolve_existing_path(source_root, label="source root")
    skipped = set(skipped_dirs)
    kotlin_files = 0
    java_files = 0
    for path in root.rglob("*"):
        if not path.is_file() or _is_skipped(path, root, skipped):
            continue
        if path.suffix == ".kt":
            kotlin_files += 1
        elif path.suffix == ".java":
            java_files += 1
    return SourceLanguageSummary(
        source_root=str(root),
        kotlin_files=kotlin_files,
        java_files=java_files,
    )


def _expand_jar_entries(
    entries: Sequence[Path],
    *,
    label: str,
    allow_aar_classpath: bool = False,
) -> list[str]:
    expanded: list[str] = []
    for entry in entries:
        resolved = _resolve_existing_path(entry, label=label)
        if resolved.is_dir():
            aar_files = sorted(path.resolve() for path in resolved.rglob("*.aar") if path.is_file())
            if aar_files and not allow_aar_classpath:
                raise ValueError(
                    f"{label} directory contains raw AAR files: {resolved}. "
                    "Extract their classes.jar files first or pass a materialized jar directory."
                )
            jars = sorted(path.resolve() for path in resolved.rglob("*.jar") if path.is_file())
            if not jars:
                raise FileNotFoundError(f"no .jar files found under {label} directory: {resolved}")
            expanded.extend(str(path) for path in jars)
        else:
            _require_jar_file(resolved, label=label, allow_aar_classpath=allow_aar_classpath)
            expanded.append(str(resolved))
    return expanded


def _dedupe_preserving_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _kotlin_classpath_entries(
    *,
    android_entries: Sequence[Path],
    classpath_entries: Sequence[Path],
    allow_aar_classpath: bool = False,
) -> list[str]:
    """Return kotlin2cpg classpath arguments.

    The local kotlin2cpg CLI reports "No jars found in the specified classpath"
    when a jar file is supplied directly. It expects directories that contain
    jars, while javasrc2cpg's --inference-jar-paths uses jar file paths. Keep
    the manifest's `classpath` field as explicit jar evidence, but pass
    directory entries to kotlin2cpg.
    """
    kotlin_entries: list[str] = []
    for android_jar in android_entries:
        kotlin_entries.append(str(android_jar.parent))
    for entry in classpath_entries:
        resolved = _resolve_existing_path(entry, label="Kotlin classpath entry")
        if resolved.is_dir():
            _expand_jar_entries(
                [resolved],
                label="Kotlin classpath entry",
                allow_aar_classpath=allow_aar_classpath,
            )
            kotlin_entries.append(str(resolved))
        else:
            jar = _require_jar_file(
                resolved,
                label="Kotlin classpath entry",
                allow_aar_classpath=allow_aar_classpath,
            )
            kotlin_entries.append(str(jar.parent))
    return _dedupe_preserving_order(kotlin_entries)


def _resolve_frontend(frontend: str, summary: SourceLanguageSummary) -> str:
    if frontend != "auto":
        selected = frontend
    elif summary.kotlin_files > 0:
        selected = "kotlin"
    elif summary.java_files > 0:
        selected = "java"
    else:
        raise ValueError(f"no .kt or .java source files found under {summary.source_root}")

    if selected == "kotlin" and summary.kotlin_files == 0:
        raise ValueError("kotlin frontend selected but no .kt files were found")
    if selected == "java" and summary.java_files == 0:
        raise ValueError("java frontend selected but no .java files were found")
    return selected


def _parse_compile_sdk(text: str) -> int | None:
    patterns = (
        r"\bcompileSdk(?:Version)?\s*(?:=|\(|\s)\s*[\"']?(\d+)[\"']?",
        r"\bcompileSdkVersion\s*\(?\s*[\"']?(\d+)[\"']?",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _parse_compile_sdk_version_catalog_key(text: str) -> str | None:
    pattern = (
        r"\bcompileSdk(?:Version)?\s*=\s*"
        r"(?:project\.)?libs\.versions\.([A-Za-z0-9_.]+)\.get\(\)(?:\.toInt\(\))?"
    )
    match = re.search(pattern, text)
    return match.group(1).replace(".", "-") if match else None


def _parse_compile_sdk_variable_name(text: str) -> str | None:
    patterns = (
        r"\bcompileSdk(?:Version)?\s*=\s*(?:rootProject\.ext\.)?([A-Za-z_][A-Za-z0-9_]*)",
        r"\bcompileSdkVersion\s+(?:rootProject\.ext\.)?([A-Za-z_][A-Za-z0-9_]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match and match.group(1) not in {"project", "libs"}:
            return match.group(1)
    return None


def _find_version_catalog_compile_sdk(gradle_file: Path, catalog_key: str) -> int | None:
    key_pattern = re.escape(catalog_key)
    value_pattern = re.compile(
        rf"^\s*[\"']?{key_pattern}[\"']?\s*=\s*[\"']?(\d+)[\"']?",
        re.MULTILINE,
    )
    for directory in (gradle_file.parent, *gradle_file.parents):
        catalog = directory / "gradle" / "libs.versions.toml"
        if not catalog.is_file():
            continue
        match = value_pattern.search(catalog.read_text(encoding="utf-8"))
        if match:
            return int(match.group(1))
    return None


def _find_gradle_variable_compile_sdk(gradle_file: Path, variable_name: str) -> int | None:
    key_pattern = re.escape(variable_name)
    value_pattern = re.compile(
        rf"\b{key_pattern}\b\s*=\s*[\"']?(\d+)[\"']?",
        re.MULTILINE,
    )
    for directory in (gradle_file.parent, *gradle_file.parents):
        for name in ("build.gradle.kts", "build.gradle", "common.gradle"):
            candidate = directory / name
            if not candidate.is_file():
                continue
            match = value_pattern.search(candidate.read_text(encoding="utf-8"))
            if match:
                return int(match.group(1))
    return None


def _find_compile_sdk(source_root: Path) -> int | None:
    gradle_names = ("build.gradle.kts", "build.gradle", "common.gradle")
    for directory in (source_root, *source_root.parents):
        for name in gradle_names:
            gradle_file = directory / name
            if not gradle_file.is_file():
                continue
            text = gradle_file.read_text(encoding="utf-8")
            compile_sdk = _parse_compile_sdk(text)
            if compile_sdk is not None:
                return compile_sdk
            catalog_key = _parse_compile_sdk_version_catalog_key(text)
            if catalog_key is not None:
                compile_sdk = _find_version_catalog_compile_sdk(gradle_file, catalog_key)
                if compile_sdk is not None:
                    return compile_sdk
            variable_name = _parse_compile_sdk_variable_name(text)
            if variable_name is not None:
                compile_sdk = _find_gradle_variable_compile_sdk(gradle_file, variable_name)
                if compile_sdk is not None:
                    return compile_sdk
    return None


def _candidate_android_sdk_roots(explicit_roots: Sequence[Path] | None) -> list[Path]:
    raw_roots: list[Path] = []
    if explicit_roots is not None:
        raw_roots.extend(explicit_roots)
    else:
        for env_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
            value = os.environ.get(env_name)
            if value:
                raw_roots.append(Path(value))
        raw_roots.extend(DEFAULT_ANDROID_SDK_ROOTS)

    roots: list[Path] = []
    seen: set[str] = set()
    for root in raw_roots:
        expanded = root.expanduser()
        key = str(expanded)
        if key not in seen:
            roots.append(expanded)
            seen.add(key)
    return roots


def _installed_android_jars(sdk_roots: Sequence[Path]) -> list[tuple[int, Path]]:
    jars: list[tuple[int, Path]] = []
    for sdk_root in sdk_roots:
        platforms = sdk_root / "platforms"
        if not platforms.is_dir():
            continue
        for platform in platforms.glob("android-*"):
            if not platform.is_dir():
                continue
            try:
                api_level = int(platform.name.removeprefix("android-"))
            except ValueError:
                continue
            android_jar = platform / "android.jar"
            if android_jar.is_file():
                jars.append((api_level, android_jar.resolve()))
    return sorted(jars)


def _resolve_android_jar(
    *,
    explicit_android_jar: Path | None,
    source_root: Path,
    auto_android_jar: bool,
    android_sdk_roots: Sequence[Path] | None,
) -> AndroidJarResolution:
    if explicit_android_jar is not None:
        return AndroidJarResolution(
            path=_require_jar_file(explicit_android_jar, label="android.jar"),
            source="explicit",
            warnings=[],
        )
    if not auto_android_jar:
        return AndroidJarResolution(path=None, source=None, warnings=[])

    sdk_roots = _candidate_android_sdk_roots(android_sdk_roots)
    compile_sdk = _find_compile_sdk(source_root)
    warnings: list[str] = []

    if compile_sdk is not None:
        for sdk_root in sdk_roots:
            android_jar = sdk_root / "platforms" / f"android-{compile_sdk}" / "android.jar"
            if android_jar.is_file():
                return AndroidJarResolution(
                    path=android_jar.resolve(),
                    source=f"auto:compileSdk={compile_sdk}",
                    warnings=[],
                )
        warnings.append(f"compileSdk {compile_sdk} found, but matching android.jar was not found")

    installed = _installed_android_jars(sdk_roots)
    if installed:
        api_level, android_jar = installed[-1]
        if compile_sdk is None:
            warnings.append(
                f"compileSdk was not found; using highest installed Android platform android-{api_level}"
            )
        else:
            warnings.append(f"using highest installed Android platform android-{api_level} instead")
        return AndroidJarResolution(
            path=android_jar,
            source=f"auto:highest-installed=android-{api_level}",
            warnings=warnings,
        )

    warnings.append(
        "android.jar was not provided and no Android SDK platform jar was auto-detected; "
        "type resolution quality may be degraded"
    )
    return AndroidJarResolution(path=None, source=None, warnings=warnings)


def build_android_cpg_plan(
    *,
    source_root: Path,
    output_path: Path,
    frontend: str = "auto",
    joern_cli_dir: Path = DEFAULT_JOERN_CLI_DIR,
    kotlin2cpg_bin: Path | None = None,
    javasrc2cpg_bin: Path | None = None,
    android_jar: Path | None = None,
    auto_android_jar: bool = True,
    android_sdk_roots: Sequence[Path] | None = None,
    classpath_entries: Sequence[Path] = (),
    inference_jar_entries: Sequence[Path] = (),
    include_java_sources: bool | None = None,
    excludes: Sequence[str] = (),
    exclude_regex: str | None = None,
    enable_file_content: bool = False,
    kotlin_download_dependencies: bool = False,
    kotlin_generate_nodes_for_dependencies: bool = False,
    java_fetch_dependencies: bool = False,
    java_delombok_mode: str | None = None,
    java_delombok_java_home: Path | None = None,
    java_jdk_path: Path | None = None,
    java_disable_type_fallback: bool = False,
    allow_aar_classpath: bool = False,
) -> AndroidCpgPlan:
    summary = detect_source_languages(source_root)
    selected_frontend = _resolve_frontend(frontend, summary)

    joern_dir = _resolve_existing_path(joern_cli_dir, label="joern CLI directory")
    kotlin_bin = (
        _resolve_existing_path(kotlin2cpg_bin or joern_dir / "kotlin2cpg", label="kotlin2cpg")
        if selected_frontend == "kotlin"
        else None
    )
    java_bin = (
        _resolve_existing_path(javasrc2cpg_bin or joern_dir / "javasrc2cpg", label="javasrc2cpg")
        if selected_frontend == "java"
        else None
    )
    resolved_output = output_path.expanduser().resolve()

    android_resolution = _resolve_android_jar(
        explicit_android_jar=android_jar,
        source_root=Path(summary.source_root),
        auto_android_jar=auto_android_jar,
        android_sdk_roots=android_sdk_roots,
    )
    for warning in android_resolution.warnings:
        print(f"[WARN] {warning}", file=sys.stderr)
    android_entries = [android_resolution.path] if android_resolution.path else []
    dependency_jars = _expand_jar_entries(
        classpath_entries,
        label="classpath/dependency entry",
        allow_aar_classpath=allow_aar_classpath,
    )
    # kotlin2cpg consumes dependency symbols via --classpath. javasrc2cpg does
    # not expose --classpath in the current local CLI, so the same dependency
    # jars are converted into --inference-jar-paths below when Java is selected.
    classpath = [str(entry) for entry in android_entries] + dependency_jars

    java_inference_entries = [*android_entries, *classpath_entries, *inference_jar_entries]
    java_inference_paths = (
        _expand_jar_entries(
            java_inference_entries,
            label="Java inference entry",
            allow_aar_classpath=allow_aar_classpath,
        )
        if selected_frontend == "java"
        else []
    )

    command: list[str]
    uses_java_sources = False
    if selected_frontend == "kotlin":
        assert kotlin_bin is not None
        kotlin_classpath = _kotlin_classpath_entries(
            android_entries=android_entries,
            classpath_entries=classpath_entries,
            allow_aar_classpath=allow_aar_classpath,
        )
        command = [str(kotlin_bin), summary.source_root, "-o", str(resolved_output)]
        uses_java_sources = (
            summary.java_files > 0 if include_java_sources is None else include_java_sources
        )
        if uses_java_sources:
            command.append("--include-java-sources")
        for entry in kotlin_classpath:
            command.extend(["--classpath", entry])
        if kotlin_download_dependencies:
            # Network-backed dependency discovery is a fallback for ad-hoc runs.
            # For experiments, prefer passing the Gradle-resolved jars explicitly
            # so the CPG is reproducible across machines and time.
            command.append("--download-dependencies")
        if kotlin_generate_nodes_for_dependencies:
            # This imports dependency code into the CPG, not just type symbols.
            # It can expose library-internal call paths, but it also increases
            # graph size and can mix app evidence with framework/library bodies.
            command.append("--generate-nodes-for-dependencies")
    else:
        assert java_bin is not None
        command = [str(java_bin), summary.source_root, "-o", str(resolved_output)]
        if java_inference_paths:
            command.extend(["--inference-jar-paths", ",".join(java_inference_paths)])
        if java_fetch_dependencies:
            # Same tradeoff as kotlin2cpg --download-dependencies: useful as a
            # fallback, but less reproducible than explicit Gradle-resolved jars.
            command.append("--fetch-dependencies")
        if java_delombok_mode:
            command.extend(["--delombok-mode", java_delombok_mode])
        if java_delombok_java_home:
            java_home = _resolve_existing_path(java_delombok_java_home, label="delombok java home")
            command.extend(
                [
                    "--delombok-java-home",
                    str(java_home),
                ]
            )
        if java_jdk_path:
            jdk_path = _resolve_existing_path(java_jdk_path, label="JDK path")
            command.extend(["--jdk-path", str(jdk_path)])
        if java_disable_type_fallback:
            command.append("--disable-type-fallback")

    for exclude in excludes:
        command.extend(["--exclude", exclude])
    if exclude_regex:
        command.extend(["--exclude-regex", exclude_regex])
    if enable_file_content:
        command.append("--enable-file-content")

    return AndroidCpgPlan(
        source_root=summary.source_root,
        output_path=str(resolved_output),
        frontend=selected_frontend,
        include_java_sources=uses_java_sources,
        kotlin_files=summary.kotlin_files,
        java_files=summary.java_files,
        android_jar=str(android_resolution.path) if android_resolution.path else None,
        android_jar_source=android_resolution.source,
        classpath=classpath,
        inference_jar_paths=java_inference_paths if selected_frontend == "java" else [],
        dependency_nodes_requested=kotlin_generate_nodes_for_dependencies,
        warnings=android_resolution.warnings,
        command=command,
    )


def _write_manifest(path: Path, result: AndroidCpgResult, *, dry_run: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dry_run": dry_run,
        "returncode": result.returncode,
        "plan": asdict(result.plan),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def generate_android_cpg(
    *,
    plan: AndroidCpgPlan,
    manifest_path: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> AndroidCpgResult:
    output = Path(plan.output_path)
    if output.exists() and not dry_run:
        if not force:
            raise FileExistsError(f"CPG output already exists; pass --force to overwrite: {output}")
        if output.is_file() or output.is_symlink():
            output.unlink()
        else:
            raise IsADirectoryError(f"refusing to overwrite directory CPG output path: {output}")

    completed: subprocess.CompletedProcess[str] | None = None
    if not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(plan.command, check=True, text=True)

    result = AndroidCpgResult(
        plan=plan,
        manifest_path=str(manifest_path.resolve()) if manifest_path else None,
        returncode=completed.returncode if completed else None,
    )
    if manifest_path:
        _write_manifest(manifest_path, result, dry_run=dry_run)
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a Joern CPG for Android Java/Kotlin source trees",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source-root", required=True, help="Android source root, e.g. app/src/main")
    parser.add_argument("--output", required=True, help="CPG output path")
    parser.add_argument("--frontend", choices=["auto", "kotlin", "java"], default="auto")
    parser.add_argument("--joern-cli-dir", default=str(DEFAULT_JOERN_CLI_DIR))
    parser.add_argument("--kotlin2cpg-bin", default=None)
    parser.add_argument("--javasrc2cpg-bin", default=None)
    parser.add_argument(
        "--android-jar",
        default=None,
        help="Android platform android.jar; auto-detected from compileSdk/installed SDK if omitted",
    )
    parser.add_argument(
        "--android-sdk-root",
        action="append",
        default=[],
        help="Android SDK root to use for android.jar auto-detection",
    )
    parser.add_argument(
        "--no-auto-android-jar",
        dest="auto_android_jar",
        action="store_false",
        default=True,
        help="Disable compileSdk/SDK-based android.jar auto-detection",
    )
    parser.add_argument(
        "--classpath",
        action="append",
        default=[],
        help=(
            "Dependency .jar or directory of .jar files. Use for AndroidX, "
            "Material, generated R.jar, ViewBinding/DataBinding classes, "
            "BuildConfig, and third-party classes.jar artifacts. Used as "
            "kotlin2cpg --classpath and expanded into javasrc2cpg "
            "--inference-jar-paths."
        ),
    )
    parser.add_argument(
        "--dependency-jar",
        action="append",
        default=[],
        help=(
            "Alias for --classpath. Prefer this name for Android research "
            "runs where these jars are type-resolution evidence, not extra app "
            "source."
        ),
    )
    parser.add_argument(
        "--inference-jar-path",
        action="append",
        default=[],
        help=(
            "Additional jar or jar directory for javasrc2cpg "
            "--inference-jar-paths. Use for Java-only type inference inputs "
            "that should not be treated as Kotlin classpath entries."
        ),
    )
    java_group = parser.add_mutually_exclusive_group()
    java_group.add_argument("--include-java-sources", action="store_true", default=None)
    java_group.add_argument("--no-include-java-sources", dest="include_java_sources", action="store_false")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--exclude-regex", default=None)
    parser.add_argument("--enable-file-content", action="store_true")
    parser.add_argument(
        "--kotlin-download-dependencies",
        action="store_true",
        help=(
            "Ask kotlin2cpg to download dependencies. Useful for ad-hoc "
            "diagnosis, but explicit Gradle-resolved jars are preferred for "
            "reproducible artifact runs."
        ),
    )
    parser.add_argument(
        "--kotlin-generate-nodes-for-dependencies",
        action="store_true",
        help=(
            "Import dependency bodies into the CPG. This can expose "
            "library-internal paths but increases graph size and mixes app "
            "evidence with framework/library code; keep off by default."
        ),
    )
    parser.add_argument(
        "--java-fetch-dependencies",
        action="store_true",
        help=(
            "Ask javasrc2cpg to fetch dependencies. Useful for ad-hoc "
            "diagnosis, but explicit Gradle-resolved jars are preferred for "
            "reproducible artifact runs."
        ),
    )
    parser.add_argument("--java-delombok-mode", choices=["no-delombok", "default", "types-only", "run-delombok"])
    parser.add_argument("--java-delombok-java-home", default=None)
    parser.add_argument("--java-jdk-path", default=None)
    parser.add_argument("--java-disable-type-fallback", action="store_true")
    parser.add_argument(
        "--allow-aar-classpath",
        action="store_true",
        help=(
            "Do not reject raw .aar files. This is not recommended for Android "
            "research runs; prefer extracting classes.jar from each AAR."
        ),
    )
    parser.add_argument("--manifest-output", default=None, help="Optional JSON manifest output path")
    parser.add_argument("--dry-run", action="store_true", help="Print the generation plan without executing Joern")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing CPG file output")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    plan = build_android_cpg_plan(
        source_root=Path(args.source_root),
        output_path=Path(args.output),
        frontend=args.frontend,
        joern_cli_dir=Path(args.joern_cli_dir),
        kotlin2cpg_bin=Path(args.kotlin2cpg_bin) if args.kotlin2cpg_bin else None,
        javasrc2cpg_bin=Path(args.javasrc2cpg_bin) if args.javasrc2cpg_bin else None,
        android_jar=Path(args.android_jar) if args.android_jar else None,
        auto_android_jar=args.auto_android_jar,
        android_sdk_roots=[Path(entry) for entry in args.android_sdk_root] or None,
        classpath_entries=[Path(entry) for entry in [*args.classpath, *args.dependency_jar]],
        inference_jar_entries=[Path(entry) for entry in args.inference_jar_path],
        include_java_sources=args.include_java_sources,
        excludes=args.exclude,
        exclude_regex=args.exclude_regex,
        enable_file_content=args.enable_file_content,
        kotlin_download_dependencies=args.kotlin_download_dependencies,
        kotlin_generate_nodes_for_dependencies=args.kotlin_generate_nodes_for_dependencies,
        java_fetch_dependencies=args.java_fetch_dependencies,
        java_delombok_mode=args.java_delombok_mode,
        java_delombok_java_home=Path(args.java_delombok_java_home) if args.java_delombok_java_home else None,
        java_jdk_path=Path(args.java_jdk_path) if args.java_jdk_path else None,
        java_disable_type_fallback=args.java_disable_type_fallback,
        allow_aar_classpath=args.allow_aar_classpath,
    )
    manifest_path = Path(args.manifest_output) if args.manifest_output else None
    result = generate_android_cpg(
        plan=plan,
        manifest_path=manifest_path,
        dry_run=args.dry_run,
        force=args.force,
    )
    print(json.dumps(asdict(result.plan), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
