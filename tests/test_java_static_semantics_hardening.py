from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app_semantics_kb.static_analysis.cli.run_android_static_semantics_artifacts import (
    DEFAULT_ARTIFACT_CPG_EXCLUDE_REGEX,
    DEFAULT_ARTIFACT_CPG_EXCLUDES,
    AndroidStaticSemanticsArtifactConfig,
    build_android_static_semantics_artifact_plan,
)
from app_semantics_kb.static_analysis.exporter.static_semantic_bundle_builder import (
    build_static_semantic_bundle,
)
from app_semantics_kb.static_analysis.slicer.legacy_context_slicer_bridge import (
    build_slicer_canonicalization,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
JAVA_FRONTEND_ROOT = (
    REPO_ROOT
    / "src"
    / "app_semantics_kb"
    / "static_analysis"
    / "frontend-java"
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_empty_context_slicer(base_dir: Path) -> None:
    context_dir = base_dir / "context-slicer-output"
    _write_json(context_dir / "slices.json", [])
    _write_json(context_dir / "method-bodies.json", {})
    _write_json(context_dir / "type-index.json", {})


class JavaStaticSemanticsHardeningTest(unittest.TestCase):
    def test_bundle_resolves_duplicate_resource_ids_by_anchor_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_empty_context_slicer(base)
            _write_json(
                base / "view-anchors-v2.json",
                [
                    {
                        "resource_id": "R.id.title",
                        "anchor_name": "titleHeader",
                        "location": "Header.java:10",
                        "view_type": "TextView",
                        "cpg_node_id": "101",
                    },
                    {
                        "resource_id": "R.id.title",
                        "anchor_name": "titleFooter",
                        "location": "Footer.java:20",
                        "view_type": "TextView",
                        "cpg_node_id": "202",
                    },
                ],
            )
            _write_json(
                base / "anchor-usages.json",
                [
                    {
                        "anchor": {
                            "resource_id": "R.id.title",
                            "anchor_name": "titleFooter",
                            "location": "Footer.java:20",
                            "cpg_node_id": "202",
                        },
                        "usages": [
                            {
                                "usage_point": {
                                    "code": "titleFooter.getText()",
                                    "file": "Footer.java",
                                    "start_line": 21,
                                    "usage_kind": "GETTER",
                                },
                                "enclosing_method_full_name": "pkg.Footer.render:void()",
                            }
                        ],
                    }
                ],
            )

            bundle = build_static_semantic_bundle(legacy_joern_dir=base, run_id="test")
            footer_anchor_uid = next(
                anchor["anchor_uid"]
                for anchor in bundle["anchors"]
                if anchor["anchor_name"] == "titleFooter"
            )

            self.assertEqual(bundle["usages"][0]["anchor_uid"], footer_anchor_uid)
            self.assertEqual(bundle["semantic_evidence"][0]["kind"], "read")

    def test_bundle_warns_for_dynamic_resource_ids_without_exporting_fake_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_empty_context_slicer(base)
            _write_json(
                base / "view-anchors-v2.json",
                [
                    {
                        "resource_id": "UNKNOWN_DYNAMIC_RESOURCE",
                        "anchor_name": "dynamic",
                        "location": "Dynamic.java:7",
                        "view_type": "UNKNOWN",
                        "cpg_node_id": "303",
                    }
                ],
            )
            _write_json(base / "anchor-usages.json", [])

            bundle = build_static_semantic_bundle(legacy_joern_dir=base, run_id="test")

            self.assertEqual(bundle["anchors"], [])
            self.assertIn(
                "dynamic_resource_id",
                {warning["code"] for warning in bundle["analysis_warnings"]},
            )

    def test_missing_method_source_records_nullable_reason_and_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            context_dir = base / "context-slicer-output"
            _write_json(
                context_dir / "slices.json",
                [
                    {
                        "primary_method": "pkg.Editor.lambda$showSearchDialog$0:void()",
                        "affecting_usages": [],
                        "call_paths": [],
                    }
                ],
            )
            _write_json(context_dir / "method-bodies.json", {})
            _write_json(context_dir / "type-index.json", {})

            result = build_slicer_canonicalization(base)

            self.assertEqual(
                result.method_slices[0]["source_code_nullable_reason"],
                "lambda_or_anonymous_body_omitted",
            )
            self.assertIn(
                "missing_method_source",
                {warning["code"] for warning in result.analysis_warnings},
            )

    def test_artifact_plan_excludes_generated_sources_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            joern_cli = root / "joern"
            joern_cli.mkdir()
            (joern_cli / "javasrc2cpg").write_text("#!/bin/sh\n", encoding="utf-8")
            source_root = root / "app" / "src" / "main" / "java"
            source_root.mkdir(parents=True)
            (source_root / "Foo.java").write_text("class Foo {}\n", encoding="utf-8")

            plan = build_android_static_semantics_artifact_plan(
                AndroidStaticSemanticsArtifactConfig(
                    app_id="sample-java",
                    project_root=root,
                    source_root=source_root,
                    frontend="java",
                    joern_cli_dir=joern_cli,
                    auto_android_jar=False,
                    dry_run=True,
                )
            )

            command = plan.cpg_plan.command if plan.cpg_plan is not None else []
            for excluded in DEFAULT_ARTIFACT_CPG_EXCLUDES:
                self.assertIn(["--exclude", excluded], [command[i : i + 2] for i in range(len(command) - 1)])
            self.assertIn(
                ["--exclude-regex", DEFAULT_ARTIFACT_CPG_EXCLUDE_REGEX],
                [command[i : i + 2] for i in range(len(command) - 1)],
            )

    def test_artifact_plan_combines_default_and_custom_exclude_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            joern_cli = root / "joern"
            joern_cli.mkdir()
            (joern_cli / "javasrc2cpg").write_text("#!/bin/sh\n", encoding="utf-8")
            source_root = root / "app" / "src" / "main" / "java"
            source_root.mkdir(parents=True)
            (source_root / "Foo.java").write_text("class Foo {}\n", encoding="utf-8")

            plan = build_android_static_semantics_artifact_plan(
                AndroidStaticSemanticsArtifactConfig(
                    app_id="sample-java",
                    project_root=root,
                    source_root=source_root,
                    frontend="java",
                    joern_cli_dir=joern_cli,
                    auto_android_jar=False,
                    cpg_exclude_regex=r"(^|.*/)tmp-generated(/|$)",
                    dry_run=True,
                )
            )

            command = plan.cpg_plan.command if plan.cpg_plan is not None else []
            exclude_regexes = [
                command[i + 1]
                for i, value in enumerate(command[:-1])
                if value == "--exclude-regex"
            ]
            self.assertEqual(1, len(exclude_regexes))
            self.assertIn(DEFAULT_ARTIFACT_CPG_EXCLUDE_REGEX, exclude_regexes[0])
            self.assertIn(r"(^|.*/)tmp-generated(/|$)", exclude_regexes[0])

    def test_artifact_plan_can_opt_into_generated_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            joern_cli = root / "joern"
            joern_cli.mkdir()
            (joern_cli / "javasrc2cpg").write_text("#!/bin/sh\n", encoding="utf-8")
            source_root = root / "app" / "src" / "main" / "java"
            source_root.mkdir(parents=True)
            (source_root / "Foo.java").write_text("class Foo {}\n", encoding="utf-8")

            plan = build_android_static_semantics_artifact_plan(
                AndroidStaticSemanticsArtifactConfig(
                    app_id="sample-java",
                    project_root=root,
                    source_root=source_root,
                    frontend="java",
                    joern_cli_dir=joern_cli,
                    auto_android_jar=False,
                    include_generated_sources=True,
                    dry_run=True,
                )
            )

            command = plan.cpg_plan.command if plan.cpg_plan is not None else []
            self.assertNotIn("--exclude", command)
            self.assertNotIn("--exclude-regex", command)

    def test_java_scala_contract_mentions_structural_classifier_and_alias_rules(self) -> None:
        signals = (JAVA_FRONTEND_ROOT / "anchor_usages" / "analysis" / "JavaAnchorUsagesUiSignals.sc").read_text(
            encoding="utf-8"
        )
        detector = (
            JAVA_FRONTEND_ROOT
            / "anchor_usages"
            / "analysis"
            / "JavaAnchorUsagesSemanticUsageDetector.sc"
        ).read_text(encoding="utf-8")
        rules = (JAVA_FRONTEND_ROOT / "view_anchors" / "JavaViewInstanceRules.sc").read_text(
            encoding="utf-8"
        )

        ignored_block = signals.split("private val ignoredNames = Set(", 1)[1].split(")", 1)[0]
        self.assertNotIn('"append"', ignored_block)
        self.assertIn("isReceiverMutator", signals)
        self.assertIn("isReadContext", detector)
        self.assertIn("UsageKind.Getter", detector)
        self.assertIn("resourceAliasesIn", rules)
        self.assertIn("UNKNOWN_DYNAMIC_RESOURCE", rules)


if __name__ == "__main__":
    unittest.main()
