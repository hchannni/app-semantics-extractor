"""Run Gradle tasks that generate Android ViewBinding artifacts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app_semantics_kb.static_analysis.android_resources.gradle_artifacts import (
    prepare_view_binding_artifacts,
    write_build_artifacts_manifest,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare Gradle-generated Android ViewBinding artifacts",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--project-root", required=True, help="Android project root containing gradlew")
    parser.add_argument("--module", default=":app", help="Gradle Android module path")
    parser.add_argument("--variant", default="debug", help="Android variant, e.g. debug or coreDebug")
    parser.add_argument("--android-home", default=None, help="Android SDK root; defaults to ANDROID_HOME")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="Gradle task timeout")
    parser.add_argument("--output", required=True, help="Path to write build artifact manifest JSON")
    parser.add_argument(
        "--allow-failure",
        action="store_true",
        help="Write a failure manifest and exit 0 instead of failing",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    android_home = args.android_home or os.environ.get("ANDROID_HOME")
    artifacts = prepare_view_binding_artifacts(
        project_root=Path(args.project_root),
        module=args.module,
        variant=args.variant,
        android_home=android_home,
        timeout_seconds=args.timeout_seconds,
    )
    output = write_build_artifacts_manifest(Path(args.output), artifacts)
    print(output)
    print(
        "view binding artifacts: "
        f"status={artifacts.status} "
        f"task={artifacts.gradle_task} "
        f"generated_source_dir={artifacts.generated_source_dir}"
    )
    if artifacts.status != "generated" and not args.allow_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
