from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app_semantics_kb.autoformalization.cli.shared.static_code_context import (
    build_static_code_context_payloads,
)
from app_semantics_kb.autoformalization.cli.shared.static_semantics_inputs import (
    find_latest_static_semantics_run_dir,
    infer_run_dir_from_context_slicer_dir,
    is_valid_static_semantics_run_dir,
    read_static_semantic_ref_from_run_dir,
    static_semantics_paths_from_run,
    validate_and_infer_run_dir_from_variant4_paths,
)


class CliSharedStaticHelpersTest(unittest.TestCase):
    def test_static_semantics_validation_honors_bundle_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "context-slicer-output").mkdir(parents=True)
            (run_dir / "method-cfg-index.json").write_text("{}", encoding="utf-8")

            self.assertTrue(is_valid_static_semantics_run_dir(run_dir))
            self.assertFalse(
                is_valid_static_semantics_run_dir(run_dir, require_bundle=True)
            )

            (run_dir / "static_semantic_bundle.json").write_text(
                '{"header": {"run_id": "static-run-1"}}',
                encoding="utf-8",
            )
            self.assertTrue(
                is_valid_static_semantics_run_dir(run_dir, require_bundle=True)
            )
            self.assertEqual(
                read_static_semantic_ref_from_run_dir(run_dir),
                "static-run-1",
            )

    def test_latest_static_semantics_run_uses_mtime_and_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            older = self._make_static_run(runs_root / "older", with_bundle=True)
            newer_without_bundle = self._make_static_run(
                runs_root / "newer_without_bundle",
                with_bundle=False,
            )
            newest = self._make_static_run(runs_root / "newest", with_bundle=True)

            os.utime(older, (1, 1))
            os.utime(newer_without_bundle, (2, 2))
            os.utime(newest, (3, 3))

            self.assertEqual(
                find_latest_static_semantics_run_dir(runs_root),
                newest,
            )
            self.assertEqual(
                find_latest_static_semantics_run_dir(runs_root, require_bundle=True),
                newest,
            )

    def test_static_path_resolution_and_explicit_inference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make_static_run(Path(tmp) / "run", with_bundle=True)
            context_dir = run_dir / "context-slicer-output"
            cfg_path = run_dir / "method-cfg-index.json"

            paths = static_semantics_paths_from_run(
                run_dir=run_dir,
                context_slicer_dir=None,
                method_cfg_index_path=None,
            )
            self.assertEqual(paths.context_slicer_dir, context_dir)
            self.assertEqual(paths.method_cfg_index_path, cfg_path)
            self.assertEqual(
                infer_run_dir_from_context_slicer_dir(context_dir),
                run_dir,
            )
            self.assertEqual(
                validate_and_infer_run_dir_from_variant4_paths(context_dir, cfg_path),
                run_dir,
            )

    def test_static_context_builder_preserves_missing_input_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a11y_path = Path(tmp) / "input.xml"
            a11y_path.write_text("<hierarchy />", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "context required"):
                build_static_code_context_payloads(
                    a11y_path=a11y_path,
                    context_slicer_dir=None,
                    method_cfg_index_path=None,
                    include_sliced_methods=True,
                    include_static_analysis=False,
                    context_required_message="context required",
                )

            with self.assertRaisesRegex(ValueError, "cfg required"):
                context_dir = Path(tmp) / "context-slicer-output"
                context_dir.mkdir()
                (context_dir / "slices.json").write_text("[]", encoding="utf-8")
                (context_dir / "method-bodies.json").write_text("{}", encoding="utf-8")
                build_static_code_context_payloads(
                    a11y_path=a11y_path,
                    context_slicer_dir=context_dir,
                    method_cfg_index_path=None,
                    include_sliced_methods=False,
                    include_static_analysis=True,
                    cfg_required_message="cfg required",
                )

    def _make_static_run(self, run_dir: Path, *, with_bundle: bool) -> Path:
        (run_dir / "context-slicer-output").mkdir(parents=True)
        (run_dir / "method-cfg-index.json").write_text("{}", encoding="utf-8")
        if with_bundle:
            (run_dir / "static_semantic_bundle.json").write_text(
                '{"header": {"run_id": "run"}}',
                encoding="utf-8",
            )
        return run_dir


if __name__ == "__main__":
    unittest.main()
