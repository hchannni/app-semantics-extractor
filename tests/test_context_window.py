from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from app_semantics_kb.autoformalization.cli.shared.variant_registry import (
    base_variant,
    default_prompt_strategy,
    default_variant_dir_name,
    parse_variant,
)
from app_semantics_kb.autoformalization.fusion.context_window import (
    DOMAIN_ATTRIBUTION_SLICE_LOCAL,
    build_context_chunk,
    build_context_evidence_index,
    context_chunk_to_static_payload,
    pack_rendered_prompt_chunks,
    pack_evidence_graph_chunks,
    render_chunk_evidence,
    slim_rendered_prompt_chunks_manifest,
    slim_chunks_manifest,
)
from app_semantics_kb.autoformalization.semantic.context_window_chunked import (
    prepare_context_window_chunked_generation,
    run_context_window_chunked_generation,
)
from app_semantics_kb.autoformalization.semantic.static_critic_evidence import (
    render_static_critic_evidence,
)
from app_semantics_kb.autoformalization.semantic.llm_client import (
    LLMResult,
    PredicateResponse,
    StatePredicate,
)


def _method(
    name: str,
    *,
    resource_ids: list[str] | None = None,
    role: str = "primary",
    body: str | None = None,
) -> dict:
    return {
        "method_full_name": name,
        "method_slice_role": role,
        "resource_ids": resource_ids or [],
        "backward_call_paths": [
            {
                "path_methods": [name, "pkg.Root.onCreate:void()"],
                "root_callback": "pkg.Root.onCreate:void()",
                "callback_kind": "LIFECYCLE",
            }
        ],
        "forward_call_paths": [[name, "pkg.Shared.helper:void()"]],
        "source_file": "src/main/java/pkg/Sample.java",
        "anchor_location": "src/main/java/pkg/Sample.java:1",
        "range_start_line": 1,
        "range_end_line": 3,
        "source_code": body or f"void {name.split('.')[-1].split(':')[0]}() {{ state++; }}",
    }


def _cfg(full_name: str) -> dict:
    return {
        "analysis": {
            "method": {
                "fullName": full_name,
                "location": "src/main/java/pkg/Sample.java:1",
            },
            "cfgSummary": {"branchCount": 0, "hasLoop": False},
            "blockPaths": [["B0"]],
            "cfg": {
                "entryBlockId": "B0",
                "blocks": [
                    {
                        "id": "B0",
                        "isEntry": True,
                        "isExit": True,
                        "instructions": ["return"],
                        "terminator": {"kind": "return"},
                        "succBlockIds": [],
                    }
                ],
            },
        }
    }


def _payloads() -> tuple[dict, dict]:
    method_a = "pkg.A.onClick:void()"
    method_b = "pkg.B.onClick:void()"
    shared = "pkg.Shared.helper:void()"
    sliced_payload = {
        "visible_resource_ids": ["R.id.a", "R.id.b", "R.id.empty"],
        "anchor_usage_overview": {
            "R.id.a": [{"usage_code": "findViewById(R.id.a)"}],
            "R.id.b": [{"usage_code": "findViewById(R.id.b)"}],
            "R.id.empty": [],
        },
        "resource_slice_index": {
            "R.id.a": [
                {
                    "slice_index": 1,
                    "primary_method": method_a,
                    "method_body_refs": [shared],
                    "domain_type_refs": ["pkg.Model"],
                    "usage_codes": ["findViewById(R.id.a)"],
                    "forward_call_paths": [[method_a, shared]],
                    "backward_call_paths": [],
                }
            ],
            "R.id.b": [
                {
                    "slice_index": 2,
                    "primary_method": method_b,
                    "method_body_refs": [shared],
                    "domain_type_refs": ["pkg.Model"],
                    "usage_codes": ["findViewById(R.id.b)"],
                    "forward_call_paths": [[method_b, shared]],
                    "backward_call_paths": [],
                }
            ],
            "R.id.empty": [],
        },
        "methods": [
            _method(
                method_a,
                resource_ids=["R.id.a"],
                body="void onClick() { Model model = new Model(); state++; }",
            ),
            _method(
                method_b,
                resource_ids=["R.id.b"],
                body="void onClick() { Model model = new Model(); state++; }",
            ),
            _method(shared, role="body_ref", body="void helper() { shared++; }"),
        ],
        "domain_types": [
            {
                "type_full_name": "pkg.Model",
                "short_name": "Model",
                "kind": "CLASS",
                "file": "src/main/java/pkg/Model.java",
                "body": "class Model { boolean enabled; }",
                "body_lines": 1,
            }
        ],
    }
    static_payload = {
        "method_cfg_list": [_cfg(method_a), _cfg(method_b), _cfg(shared)]
    }
    return sliced_payload, static_payload


def _empty_payloads() -> tuple[dict, dict]:
    return (
        {
            "visible_resource_ids": ["R.id.content", "R.id.title"],
            "anchor_usage_overview": {
                "R.id.content": [],
                "R.id.title": [],
            },
            "resource_slice_index": {
                "R.id.content": [],
                "R.id.title": [],
            },
            "methods": [],
            "domain_types": [],
        },
        {"method_cfg_list": []},
    )


def _max_rendered_chars(chunk: dict) -> int:
    return max(
        len(render_chunk_evidence(chunk, mode=mode))
        for mode in ("v3", "v4", "critic")
    )


class ContextWindowChunkingTest(unittest.TestCase):
    def test_chunked_variant_registry_aliases(self) -> None:
        self.assertEqual(parse_variant("3c"), "3chunked")
        self.assertEqual(parse_variant("v3-chunked"), "3chunked")
        self.assertEqual(parse_variant("4c"), "4chunked")
        self.assertEqual(parse_variant("v4_chunked"), "4chunked")
        self.assertEqual(base_variant("3chunked"), 3)
        self.assertEqual(base_variant("4chunked"), 4)
        self.assertEqual(default_variant_dir_name("3chunked"), "variant_3chunked")
        self.assertEqual(default_variant_dir_name("4chunked"), "variant_4chunked")
        self.assertEqual(
            default_prompt_strategy("3chunked"),
            "v3_context_window_evidence_graph_chunked_merge",
        )
        self.assertEqual(
            default_prompt_strategy("4chunked"),
            "v4_context_window_evidence_graph_chunked_merge",
        )

    def test_graph_index_restores_body_ref_provenance_and_dedupe_stats(self) -> None:
        sliced_payload, static_payload = _payloads()
        index = build_context_evidence_index(sliced_payload, static_payload)

        methods = {entry["method_full_name"]: entry for entry in index["methods"]}
        shared = methods["pkg.Shared.helper:void()"]
        self.assertEqual(
            shared["body_ref_for_primary_methods"],
            ["pkg.A.onClick:void()", "pkg.B.onClick:void()"],
        )
        self.assertEqual(shared["body_ref_for_resource_ids"], ["R.id.a", "R.id.b"])
        self.assertEqual(index["stats"]["unique_method_count"], 3)
        self.assertEqual(index["stats"]["method_duplicate_occurrences_avoided"], 1)
        self.assertEqual(index["stats"]["unique_domain_type_count"], 1)
        self.assertEqual(index["stats"]["domain_type_duplicate_occurrences_avoided"], 1)
        self.assertEqual(index["domain_attribution"]["policy"], "method_local")
        self.assertEqual(index["domain_attribution"]["omitted_from_all_chunks"], [])

    def test_method_local_domain_attribution_preserves_omitted_provenance(self) -> None:
        sliced_payload, static_payload = _payloads()
        payload = deepcopy(sliced_payload)
        for records in payload["resource_slice_index"].values():
            for record in records:
                record["domain_type_refs"].append("pkg.Unused")
        payload["domain_types"].append(
            {
                "type_full_name": "pkg.Unused",
                "short_name": "Unused",
                "kind": "CLASS",
                "file": "src/main/java/pkg/Unused.java",
                "body": "class Unused { int hidden; }",
                "body_lines": 1,
            }
        )

        index = build_context_evidence_index(payload, static_payload)
        methods = {entry["method_full_name"]: entry for entry in index["methods"]}
        method_a = methods["pkg.A.onClick:void()"]
        chunk = build_context_chunk(
            index=index,
            chunk_id="chunk_0001",
            method_names=["pkg.A.onClick:void()"],
        )

        self.assertEqual(method_a["domain_type_refs"], ["pkg.Model"])
        self.assertIn("pkg.Unused", method_a["available_but_not_attached_domain_type_refs"])
        self.assertIn("pkg.Unused", index["domain_attribution"]["omitted_from_all_chunks"])
        self.assertEqual(chunk["mandatory_domain_type_refs"], ["pkg.Model"])
        self.assertIn("pkg.Unused", chunk["available_but_not_attached_domain_type_refs"])
        self.assertNotIn("class Unused", render_chunk_evidence(chunk, mode="v3"))

    def test_slice_local_policy_keeps_broad_domain_refs_for_repro(self) -> None:
        sliced_payload, static_payload = _payloads()
        payload = deepcopy(sliced_payload)
        payload["resource_slice_index"]["R.id.a"][0]["domain_type_refs"].append(
            "pkg.Unused"
        )
        payload["domain_types"].append(
            {
                "type_full_name": "pkg.Unused",
                "short_name": "Unused",
                "kind": "CLASS",
                "file": "src/main/java/pkg/Unused.java",
                "body": "class Unused { int hidden; }",
                "body_lines": 1,
            }
        )

        index = build_context_evidence_index(
            payload,
            static_payload,
            domain_attribution_policy=DOMAIN_ATTRIBUTION_SLICE_LOCAL,
        )
        chunk = build_context_chunk(
            index=index,
            chunk_id="chunk_0001",
            method_names=["pkg.A.onClick:void()"],
        )

        self.assertEqual(index["domain_attribution"]["policy"], "slice_local")
        self.assertIn("pkg.Unused", chunk["mandatory_domain_type_refs"])
        self.assertIn("class Unused", render_chunk_evidence(chunk, mode="v3"))

    def test_packer_dedupes_methods_and_cfg_across_chunks(self) -> None:
        sliced_payload, static_payload = _payloads()
        index = build_context_evidence_index(sliced_payload, static_payload)
        packed = pack_evidence_graph_chunks(
            index,
            target_chars=1,
            max_chars=100000,
        )
        manifest = slim_chunks_manifest(packed)

        self.assertEqual(
            manifest["meta"]["source"],
            "shared_evidence_aware_evidence_graph_packer",
        )
        self.assertEqual(manifest["stats"]["chunk_count"], 1)
        self.assertGreaterEqual(manifest["stats"]["post_merge_savings"], 1)
        self.assertEqual(manifest["shared_evidence_duplicates"]["methods"], [])
        self.assertEqual(manifest["shared_evidence_duplicates"]["cfg_methods"], [])
        chunk_methods = [set(chunk["method_full_names"]) for chunk in packed["chunks"]]
        self.assertTrue(
            any(
                {"pkg.A.onClick:void()", "pkg.B.onClick:void()"}.issubset(methods)
                for methods in chunk_methods
            )
        )
        method_occurrences = Counter(
            method
            for chunk in packed["chunks"]
            for method in chunk["method_full_names"]
        )
        self.assertEqual(method_occurrences["pkg.Shared.helper:void()"], 1)
        self.assertEqual(
            manifest["dedupe_savings"]["method_duplicate_occurrences_avoided"],
            1,
        )

    def test_manifest_records_cross_chunk_domain_duplicate_rough_chars(self) -> None:
        sliced_payload, static_payload = _payloads()
        for method in sliced_payload["methods"]:
            if method["method_full_name"] in {
                "pkg.A.onClick:void()",
                "pkg.B.onClick:void()",
            }:
                method["source_code"] = "Model model = new Model(); state++;\n" * 500
        index = build_context_evidence_index(sliced_payload, static_payload)
        single_sizes = [
            _max_rendered_chars(
                build_context_chunk(
                    index=index,
                    chunk_id="chunk_0000",
                    method_names=[method_name],
                )
            )
            for method_name in ("pkg.A.onClick:void()", "pkg.B.onClick:void()")
        ]
        pair_size = _max_rendered_chars(
            build_context_chunk(
                index=index,
                chunk_id="chunk_0000",
                method_names=["pkg.A.onClick:void()", "pkg.B.onClick:void()"],
            )
        )
        max_chars = max(single_sizes) + 10
        self.assertGreater(pair_size, max_chars)

        packed = pack_evidence_graph_chunks(index, target_chars=1, max_chars=max_chars)
        manifest = slim_chunks_manifest(packed)

        duplicate_domains = {
            entry["type_full_name"]: entry
            for entry in manifest["shared_evidence_duplicates"]["domain_types"]
        }
        self.assertIn("pkg.Model", duplicate_domains)
        self.assertGreater(duplicate_domains["pkg.Model"]["duplicated_rough_chars"], 0)
        self.assertGreater(
            manifest["stats"]["domain_type_cross_chunk_duplicated_rough_chars"],
            0,
        )

    def test_single_method_over_max_is_oversized_without_truncation(self) -> None:
        sliced_payload, static_payload = _payloads()
        index = build_context_evidence_index(sliced_payload, static_payload)
        method_name = "pkg.A.onClick:void()"
        single_chunk = build_context_chunk(
            index=index,
            chunk_id="chunk_0000",
            method_names=[method_name],
        )
        max_chars = _max_rendered_chars(single_chunk) - 1

        packed = pack_evidence_graph_chunks(
            index,
            target_chars=max_chars,
            max_chars=max_chars,
        )
        oversized_chunk = next(
            chunk
            for chunk in packed["chunks"]
            if chunk["method_full_names"] == [method_name]
        )

        self.assertTrue(oversized_chunk["oversized"])
        self.assertEqual(
            oversized_chunk["split_reason"],
            "single_method_full_evidence_over_max_chars",
        )
        self.assertEqual(
            oversized_chunk["domain_types"][0]["body"],
            "class Model { boolean enabled; }",
        )

    def test_static_payload_is_subset_to_chunk_methods(self) -> None:
        sliced_payload, static_payload = _payloads()
        index = build_context_evidence_index(sliced_payload, static_payload)
        chunk = build_context_chunk(
            index=index,
            chunk_id="chunk_0001",
            method_names=["pkg.A.onClick:void()", "pkg.Shared.helper:void()"],
        )
        static_subset = context_chunk_to_static_payload(chunk)
        cfg_names = [
            item["analysis"]["method"]["fullName"]
            for item in static_subset["method_cfg_list"]
        ]

        self.assertEqual(
            cfg_names,
            ["pkg.A.onClick:void()", "pkg.Shared.helper:void()"],
        )

    def test_domain_types_keep_full_body(self) -> None:
        sliced_payload, static_payload = _payloads()
        index = build_context_evidence_index(sliced_payload, static_payload)
        chunk = build_context_chunk(
            index=index,
            chunk_id="chunk_0001",
            method_names=["pkg.A.onClick:void()"],
        )

        self.assertEqual(
            chunk["domain_types"][0]["body"],
            "class Model { boolean enabled; }",
        )
        self.assertNotIn("context_window_body_omitted", chunk["domain_types"][0])

    def test_rendering_modes_reuse_existing_variant_shapes(self) -> None:
        sliced_payload, static_payload = _payloads()
        index = build_context_evidence_index(sliced_payload, static_payload)
        chunk = build_context_chunk(
            index=index,
            chunk_id="chunk_0001",
            method_names=["pkg.A.onClick:void()", "pkg.Shared.helper:void()"],
        )

        v3_text = render_chunk_evidence(chunk, mode="v3")
        self.assertIn("# Sliced Methods Source Context", v3_text)
        self.assertNotIn("# Resource", v3_text)
        self.assertNotIn("# CFG Details", v3_text)
        self.assertIn("class Model { boolean enabled; }", v3_text)

        v4_text = render_chunk_evidence(chunk, mode="v4")
        self.assertIn("# Resource", v4_text)
        self.assertIn("# CFG Details", v4_text)

        critic_text = render_chunk_evidence(chunk, mode="critic")
        self.assertIn("STATIC ANALYSIS EVIDENCE CHUNK", critic_text)
        self.assertIn(
            "This is one chunk of the page-local static analysis payload",
            critic_text,
        )
        self.assertIn("--- SLICED METHODS AND CFG EVIDENCE ---", critic_text)

    def test_static_critic_evidence_default_header_is_variant_neutral(self) -> None:
        sliced_payload, static_payload = _payloads()

        critic_text = render_static_critic_evidence(
            sliced_methods_payload=sliced_payload,
            static_analysis_payload=static_payload,
            critic_label="V2-chunked Step 2 critic",
        )

        self.assertIn("STATIC ANALYSIS EVIDENCE", critic_text)
        self.assertIn(
            "Generated from context-slicer output and the method-CFG index",
            critic_text,
        )
        self.assertIn("the V2-chunked Step 2 critic", critic_text)
        self.assertNotIn("used by V4", critic_text)

    def test_naive_rendered_prompt_chunker_preserves_text_without_loss(self) -> None:
        text = "alpha\n" + ("beta gamma\n" * 20) + ("x" * 25) + "\nomega\n"
        packed = pack_rendered_prompt_chunks(text, target_chars=40, max_chars=50)
        manifest = slim_rendered_prompt_chunks_manifest(packed)
        joined = "".join(chunk["text"] for chunk in packed["chunks"])

        self.assertEqual(joined, text)
        self.assertEqual(manifest["stats"]["source_char_count"], len(text))
        self.assertEqual(manifest["stats"]["chunked_char_count"], len(text))
        self.assertLessEqual(manifest["stats"]["max_chunk_chars"], 50)
        self.assertNotIn("text", manifest["chunks"][0])

    def test_context_window_chunked_prepare_uses_v3_v4_prompt_shapes(self) -> None:
        sliced_payload, static_payload = _payloads()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            v3 = prepare_context_window_chunked_generation(
                output_dir=tmp_path / "variant_3chunked" / "page_11",
                run_id="test",
                page=11,
                variant_key="3chunked",
                base_variant=3,
                prompt_strategy="v3_context_window_evidence_graph_chunked_merge",
                app_name="Sample",
                a11y_xml="<hierarchy><node resource-id='R.id.a'/></hierarchy>",
                screenshot_path=None,
                existing_predicates=[],
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=static_payload,
                model="fake",
                prompt_cache_key="base",
                domain_attribution_policy="method_local",
                target_chars=400000,
                max_chars=500000,
            )
            v4 = prepare_context_window_chunked_generation(
                output_dir=tmp_path / "variant_4chunked" / "page_11",
                run_id="test",
                page=11,
                variant_key="4chunked",
                base_variant=4,
                prompt_strategy="v4_context_window_evidence_graph_chunked_merge",
                app_name="Sample",
                a11y_xml="<hierarchy><node resource-id='R.id.a'/></hierarchy>",
                screenshot_path=None,
                existing_predicates=[],
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=static_payload,
                model="fake",
                prompt_cache_key="base",
                domain_attribution_policy="method_local",
                target_chars=400000,
                max_chars=500000,
            )

            self.assertEqual(v3.paths["status"], "prepared")
            self.assertEqual(v4.paths["status"], "prepared")
            v3_user = (
                tmp_path
                / "variant_3chunked/page_11/chunk_prompts/chunk_0001_user.txt"
            ).read_text(encoding="utf-8")
            v4_user = (
                tmp_path
                / "variant_4chunked/page_11/chunk_prompts/chunk_0001_user.txt"
            ).read_text(encoding="utf-8")

            self.assertIn("# Sliced Methods Source Context", v3_user)
            self.assertNotIn("# CFG Details", v3_user)
            self.assertIn("# Resource", v4_user)
            self.assertIn("# CFG Details", v4_user)
            self.assertEqual(v3.chunks_manifest["stats"]["chunk_count"], 1)
            self.assertEqual(v4.chunks_manifest["stats"]["chunk_count"], 1)

    def test_context_window_chunked_run_merges_fake_chunk_responses(self) -> None:
        sliced_payload, static_payload = _payloads()
        calls: list[dict] = []

        def fake_query(**kwargs) -> LLMResult:
            calls.append(kwargs)
            idx = len(calls)
            response = PredicateResponse(
                Analysis=f"chunk {idx}",
                State_Definitions=[
                    StatePredicate(
                        name=f"Chunk{idx}State",
                        description="fake",
                        variables=[],
                    )
                ],
            )
            return LLMResult(
                model=kwargs["model"],
                variant=kwargs["variant"],
                response=response,
                prompt_tokens=10,
                completion_tokens=3,
                latency_sec=0.25,
                raw_json=response.model_dump_json(),
            )

        with tempfile.TemporaryDirectory() as tmp:
            run = run_context_window_chunked_generation(
                output_dir=Path(tmp) / "variant_3chunked" / "page_11",
                run_id="test",
                page=11,
                variant_key="3chunked",
                base_variant=3,
                prompt_strategy="v3_context_window_evidence_graph_chunked_merge",
                app_name="Sample",
                a11y_xml="<hierarchy><node resource-id='R.id.a'/></hierarchy>",
                screenshot_path=Path(tmp) / "screen.png",
                existing_predicates=[],
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=static_payload,
                model="fake",
                timeout=1.0,
                prompt_cache_key="base",
                domain_attribution_policy="method_local",
                target_chars=400000,
                max_chars=500000,
                query_fn=fake_query,
            )

            self.assertEqual(len(calls), run.chunks_manifest["stats"]["chunk_count"])
            self.assertEqual(run.final_result.prompt_tokens, 10 * len(calls))
            self.assertEqual(run.paths["status"], "success")
            self.assertTrue((Path(tmp) / "variant_3chunked/page_11/response_parsed.json").is_file())
            self.assertEqual(
                len(run.final_result.response.State_Definitions),
                len(calls),
            )

    def test_context_window_chunked_runs_image_a11y_fallback_when_no_static_chunks(self) -> None:
        sliced_payload, static_payload = _empty_payloads()
        calls: list[dict] = []

        def fake_query(**kwargs) -> LLMResult:
            calls.append(kwargs)
            response = PredicateResponse(
                Analysis="image fallback",
                State_Definitions=[
                    StatePredicate(
                        name="MenuOverlay",
                        description="fake",
                        variables=[],
                    )
                ],
            )
            return LLMResult(
                model=kwargs["model"],
                variant=kwargs["variant"],
                response=response,
                prompt_tokens=7,
                completion_tokens=2,
                latency_sec=0.1,
                raw_json=response.model_dump_json(),
            )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "variant_3chunked" / "page_12"
            run = run_context_window_chunked_generation(
                output_dir=output_dir,
                run_id="test",
                page=12,
                variant_key="3chunked",
                base_variant=3,
                prompt_strategy="v3_context_window_evidence_graph_chunked_merge",
                app_name="Sample",
                a11y_xml="<hierarchy><node text='File Settings' resource-id='R.id.title'/></hierarchy>",
                screenshot_path=Path(tmp) / "screen.png",
                existing_predicates=[],
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=static_payload,
                model="fake",
                timeout=1.0,
                prompt_cache_key="base",
                domain_attribution_policy="method_local",
                target_chars=400000,
                max_chars=500000,
                query_fn=fake_query,
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(run.chunks_manifest["stats"]["static_evidence_chunk_count"], 0)
            self.assertEqual(run.chunks_manifest["stats"]["llm_prompt_chunk_count"], 1)
            self.assertEqual(
                run.chunks_manifest["stats"]["static_evidence_covered_resource_count"],
                0,
            )
            self.assertEqual(run.chunks_manifest["stats"]["llm_prompt_resource_count"], 2)
            self.assertEqual(run.chunks_manifest["stats"]["chunk_count"], 1)
            self.assertTrue(run.chunks_manifest["chunks"][0]["fallback"])
            self.assertEqual(run.paths["status"], "success")
            self.assertEqual(len(run.final_result.response.State_Definitions), 1)
            user_prompt = (
                output_dir / "chunk_prompts" / "chunk_0001_user.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("File Settings", user_prompt)
            self.assertIn("No page-local static method", user_prompt)

    def test_context_window_chunked_run_writes_chunk_failure_artifact(self) -> None:
        sliced_payload, static_payload = _payloads()

        def failing_query(**_: dict) -> LLMResult:
            raise RuntimeError("forced failure")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "variant_3chunked" / "page_11"
            with self.assertRaises(RuntimeError):
                run_context_window_chunked_generation(
                    output_dir=output_dir,
                    run_id="test",
                    page=11,
                    variant_key="3chunked",
                    base_variant=3,
                    prompt_strategy="v3_context_window_evidence_graph_chunked_merge",
                    app_name="Sample",
                    a11y_xml="<hierarchy><node resource-id='R.id.a'/></hierarchy>",
                    screenshot_path=Path(tmp) / "screen.png",
                    existing_predicates=[],
                    sliced_methods_payload=sliced_payload,
                    static_analysis_payload=static_payload,
                    model="fake",
                    timeout=1.0,
                    prompt_cache_key="base",
                    domain_attribution_policy="method_local",
                    target_chars=400000,
                    max_chars=500000,
                    query_fn=failing_query,
                )

            failure_path = output_dir / "chunk_outputs" / "chunk_0001.failure.json"
            self.assertTrue(failure_path.is_file())
            self.assertIn("forced failure", failure_path.read_text(encoding="utf-8"))

    def test_context_window_chunked_retries_and_cleans_stale_failure(self) -> None:
        sliced_payload, static_payload = _payloads()
        calls = 0

        class APITimeoutError(Exception):
            pass

        def flaky_query(**kwargs) -> LLMResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise APITimeoutError("temporary timeout")
            response = PredicateResponse(
                Analysis="retry succeeded",
                State_Definitions=[
                    StatePredicate(
                        name="RecoveredState",
                        description="fake",
                        variables=[],
                    )
                ],
            )
            return LLMResult(
                model=kwargs["model"],
                variant=kwargs["variant"],
                response=response,
                prompt_tokens=11,
                completion_tokens=2,
                latency_sec=0.1,
                raw_json=response.model_dump_json(),
            )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "variant_3chunked" / "page_11"
            run_context_window_chunked_generation(
                output_dir=output_dir,
                run_id="test",
                page=11,
                variant_key="3chunked",
                base_variant=3,
                prompt_strategy="v3_context_window_evidence_graph_chunked_merge",
                app_name="Sample",
                a11y_xml="<hierarchy><node resource-id='R.id.a'/></hierarchy>",
                screenshot_path=Path(tmp) / "screen.png",
                existing_predicates=[],
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=static_payload,
                model="fake",
                timeout=1.0,
                prompt_cache_key="base",
                domain_attribution_policy="method_local",
                target_chars=400000,
                max_chars=500000,
                chunk_max_attempts=2,
                chunk_retry_base_delay=0.0,
                chunk_retry_max_delay=0.0,
                query_fn=flaky_query,
            )

            self.assertEqual(calls, 2)
            self.assertFalse(
                (output_dir / "chunk_outputs" / "chunk_0001.failure.json").exists()
            )
            chunk_output = json.loads(
                (output_dir / "chunk_outputs" / "chunk_0001.json").read_text(
                    encoding="utf-8"
                )
            )
            merge_meta = json.loads(
                (output_dir / "merge_meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(chunk_output["attempt_count"], 2)
            self.assertEqual(chunk_output["retry_failure_count"], 1)
            self.assertEqual(
                merge_meta["stale_failure_cleanup"]["removed_count"],
                1,
            )

    def test_context_window_chunked_resumes_existing_successful_chunk(self) -> None:
        sliced_payload, static_payload = _payloads()
        calls = 0

        def successful_query(**kwargs) -> LLMResult:
            nonlocal calls
            calls += 1
            response = PredicateResponse(
                Analysis="first run",
                State_Definitions=[
                    StatePredicate(
                        name="ReusableState",
                        description="fake",
                        variables=[],
                    )
                ],
            )
            return LLMResult(
                model=kwargs["model"],
                variant=kwargs["variant"],
                response=response,
                prompt_tokens=13,
                completion_tokens=3,
                latency_sec=0.2,
                raw_json=response.model_dump_json(),
            )

        def forbidden_query(**_: dict) -> LLMResult:
            raise AssertionError("existing chunk should have been reused")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "variant_3chunked" / "page_11"
            run_context_window_chunked_generation(
                output_dir=output_dir,
                run_id="test",
                page=11,
                variant_key="3chunked",
                base_variant=3,
                prompt_strategy="v3_context_window_evidence_graph_chunked_merge",
                app_name="Sample",
                a11y_xml="<hierarchy><node resource-id='R.id.a'/></hierarchy>",
                screenshot_path=Path(tmp) / "screen.png",
                existing_predicates=[],
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=static_payload,
                model="fake",
                timeout=1.0,
                prompt_cache_key="base",
                domain_attribution_policy="method_local",
                target_chars=400000,
                max_chars=500000,
                query_fn=successful_query,
            )
            self.assertEqual(calls, 1)

            second = run_context_window_chunked_generation(
                output_dir=output_dir,
                run_id="test",
                page=11,
                variant_key="3chunked",
                base_variant=3,
                prompt_strategy="v3_context_window_evidence_graph_chunked_merge",
                app_name="Sample",
                a11y_xml="<hierarchy><node resource-id='R.id.a'/></hierarchy>",
                screenshot_path=Path(tmp) / "screen.png",
                existing_predicates=[],
                sliced_methods_payload=sliced_payload,
                static_analysis_payload=static_payload,
                model="fake",
                timeout=1.0,
                prompt_cache_key="base",
                domain_attribution_policy="method_local",
                target_chars=400000,
                max_chars=500000,
                query_fn=forbidden_query,
            )

            self.assertEqual(calls, 1)
            self.assertEqual(second.final_result.prompt_tokens, 13)
            merge_meta = json.loads(
                (output_dir / "merge_meta.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                merge_meta["chunk_execution"][0]["reused_from_existing"]
            )


if __name__ == "__main__":
    unittest.main()
