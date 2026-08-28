"""Materialize a jar directory suitable for Joern kotlin2cpg --classpath."""

from __future__ import annotations

import argparse
from pathlib import Path

from app_semantics_kb.static_analysis.android_resources.gradle_artifacts import (
    read_resource_artifacts_manifest,
)
from app_semantics_kb.static_analysis.android_resources.merged_resources import (
    dependency_class_jars_from_merger,
    materialize_cpg_classpath,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy Android, R, and dependency jars into a kotlin2cpg classpath directory",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--resource-artifact-manifest", required=True)
    parser.add_argument("--android-jar", default=None, help="Android platform android.jar")
    parser.add_argument("--output-dir", required=True, help="Directory to populate with classpath jars")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    artifacts = read_resource_artifacts_manifest(Path(args.resource_artifact_manifest))
    merger_xml = Path(artifacts.merger_xml) if artifacts.merger_xml else None
    dependency_jars = dependency_class_jars_from_merger(merger_xml) if merger_xml else []
    copied = materialize_cpg_classpath(
        output_dir=Path(args.output_dir),
        android_jar=Path(args.android_jar) if args.android_jar else None,
        r_jars=[Path(path) for path in artifacts.r_jars],
        dependency_jars=dependency_jars,
    )
    print(Path(args.output_dir))
    print(f"cpg classpath jars: copied={len(copied)} dependency_jars={len(dependency_jars)}")


if __name__ == "__main__":
    main()
