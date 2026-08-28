"""Run Gradle tasks that generate Android resource merge and R artifacts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app_semantics_kb.static_analysis.android_resources.gradle_artifacts import (
    prepare_resource_artifacts,
    write_resource_artifacts_manifest,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare Android Gradle resource artifacts for static semantics",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--project-root", required=True, help="Android project root containing gradlew")
    parser.add_argument("--module", default=":app", help="Gradle Android module path")
    parser.add_argument("--variant", default="debug", help="Android variant, e.g. debug or coreDebug")
    parser.add_argument("--android-home", default=None, help="Android SDK root; defaults to ANDROID_HOME")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="Gradle task timeout")
    parser.add_argument("--output", required=True, help="Path to write resource artifact manifest JSON")
    parser.add_argument("--allow-failure", action="store_true", help="Write a failure manifest and exit 0")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    artifacts = prepare_resource_artifacts(
        project_root=Path(args.project_root),
        module=args.module,
        variant=args.variant,
        android_home=args.android_home or os.environ.get("ANDROID_HOME"),
        timeout_seconds=args.timeout_seconds,
    )
    output = write_resource_artifacts_manifest(Path(args.output), artifacts)
    print(output)
    print(
        "resource artifacts: "
        f"status={artifacts.status} "
        f"task={artifacts.gradle_task} "
        f"merger_xml={artifacts.merger_xml} "
        f"r_jars={len(artifacts.r_jars)}"
    )
    if artifacts.status != "generated" and not args.allow_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
