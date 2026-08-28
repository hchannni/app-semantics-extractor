"""
generate_semantic_contracts.py — target-home semantic contract generation CLI

Prompt 파일과 입력 이미지를 받아 semantic 단계를 실행하고,
PredicateBundle + PredicateEvidenceBundle JSON을 출력 디렉토리에 저장한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..fusion import build_provenance_seed_map, load_optional_payload
from ..semantic.contract_runner import (
    load_provenance_seed_map,
    run_semantic_contract_generation,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate target-home semantic contracts (PredicateBundle + PredicateEvidenceBundle)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--system-prompt-file", required=True, help="Path to system prompt text file")
    parser.add_argument("--user-prompt-file", required=True, help="Path to user prompt text file")
    parser.add_argument("--screenshot-path", default=None, help="Optional screenshot path")
    parser.add_argument("--variant", type=int, choices=[1, 2, 3, 4], required=True)
    parser.add_argument("--output-dir", required=True, help="Directory where semantic contract JSON files will be written")
    parser.add_argument("--run-id", required=True, help="Run identifier stored in artifact headers")
    parser.add_argument("--fusion-ref", default=None, help="Optional FusionBundle run_id reference")
    parser.add_argument("--static-semantic-ref", default=None, help="Optional StaticSemanticBundle run_id reference")
    parser.add_argument("--screen-context", default=None, help="Optional screen label")
    parser.add_argument("--provenance-seed-json", default=None, help="Optional predicate_name -> mechanical provenance seed JSON")
    parser.add_argument("--sliced-methods-payload-json", default=None, help="Optional sliced_methods_payload JSON for automatic mechanical provenance seed generation")
    parser.add_argument("--static-analysis-payload-json", default=None, help="Optional static_analysis_payload JSON for automatic mechanical provenance seed generation")
    parser.add_argument("--model", default="gpt-5.2", help="OpenAI model ID")
    parser.add_argument("--timeout", type=float, default=120.0, help="LLM request timeout in seconds")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8")
    user_prompt = Path(args.user_prompt_file).read_text(encoding="utf-8")
    screenshot_path = Path(args.screenshot_path) if args.screenshot_path else None
    provenance_seeds = load_provenance_seed_map(
        Path(args.provenance_seed_json) if args.provenance_seed_json else None
    )
    if not provenance_seeds:
        provenance_seeds = build_provenance_seed_map(
            sliced_methods_payload=load_optional_payload(
                Path(args.sliced_methods_payload_json) if args.sliced_methods_payload_json else None
            ),
            static_analysis_payload=load_optional_payload(
                Path(args.static_analysis_payload_json) if args.static_analysis_payload_json else None
            ),
        )

    result = run_semantic_contract_generation(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        screenshot_path=screenshot_path,
        variant=args.variant,
        output_dir=Path(args.output_dir),
        run_id=args.run_id,
        fusion_ref=args.fusion_ref,
        static_semantic_ref=args.static_semantic_ref,
        screen_context=args.screen_context,
        provenance_by_predicate_name=provenance_seeds,
        model=args.model,
        timeout=args.timeout,
    )

    print(result.paths["predicate_bundle"])
    print(result.paths["predicate_evidence_bundle"])


if __name__ == "__main__":
    main()
