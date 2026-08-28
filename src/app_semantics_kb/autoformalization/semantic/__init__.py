"""semantic — 병합된 컨텍스트를 기반으로 LFM을 호출하여 State Predicate를 생성하는 모듈.

하위 모듈은 lazy import로 제공한다.
openai 등 선택적 의존성이 없는 환경에서도 패키지 자체는 import 가능하다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import (
        FPFNAnalysisResponse,
        FPFNResult,
        FNVariableItem,
        FPVariableItem,
        LLMResult,
        PredicateResponse,
        StatePredicate,
        V2ChunkCandidate,
        V2ChunkCandidateResponse,
        V2ChunkResult,
        Variable,
        build_user_content,
        query_llm,
        query_llm_in_session,
        query_llm_followup,
        query_v2_chunk_candidates,
        result_to_dict_list,
    )
    from .bundle_builder import (
        MechanicalProvenanceSeed,
        build_predicate_bundle,
        build_predicate_evidence_bundle,
        build_semantic_contract_bundles,
        save_semantic_contract_bundles,
        build_and_save_semantic_contract_bundles,
    )
    from .contract_runner import (
        SemanticContractRunResult,
        load_provenance_seed_map,
        run_semantic_contract_generation,
    )
    from .prompt_builder import build_prompt, build_prompt_v2prime
    from .critic_runner import (
        CriticResponse,
        CriticResult,
        PredicateVerdict,
        apply_monotone_decrease,
        build_critic_user_prompt,
        query_critic_in_session,
        run_critic_turn,
        serialize_predicate_candidates_with_ids,
    )


_LLM_CLIENT_NAMES = {
    "query_llm",
    "query_llm_in_session",
    "query_llm_followup",
    "build_user_content",
    "result_to_dict_list",
    "PredicateResponse",
    "StatePredicate",
    "Variable",
    "V2ChunkCandidate",
    "V2ChunkCandidateResponse",
    "V2ChunkResult",
    "LLMResult",
    "FPFNAnalysisResponse",
    "FPFNResult",
    "FNVariableItem",
    "FPVariableItem",
}

_BUNDLE_BUILDER_NAMES = {
    "MechanicalProvenanceSeed",
    "build_predicate_bundle",
    "build_predicate_evidence_bundle",
    "build_semantic_contract_bundles",
    "save_semantic_contract_bundles",
    "build_and_save_semantic_contract_bundles",
}

_CONTRACT_RUNNER_NAMES = {
    "SemanticContractRunResult",
    "load_provenance_seed_map",
    "run_semantic_contract_generation",
}

_CRITIC_RUNNER_NAMES = {
    "PredicateVerdict",
    "CriticResponse",
    "CriticResult",
    "serialize_predicate_candidates_with_ids",
    "apply_monotone_decrease",
    "build_critic_user_prompt",
    "query_critic_in_session",
    "run_critic_turn",
}


def __getattr__(name: str):
    """최초 접근 시 하위 모듈을 lazy import한다."""
    if name in {"build_prompt", "build_prompt_v2prime"}:
        from . import prompt_builder as _prompt_builder
        _f = getattr(_prompt_builder, name)
        return _f
    if name in _BUNDLE_BUILDER_NAMES:
        import importlib
        mod = importlib.import_module(".bundle_builder", __name__)
        return getattr(mod, name)
    if name in _CONTRACT_RUNNER_NAMES:
        import importlib
        mod = importlib.import_module(".contract_runner", __name__)
        return getattr(mod, name)
    if name in _CRITIC_RUNNER_NAMES:
        import importlib
        mod = importlib.import_module(".critic_runner", __name__)
        return getattr(mod, name)
    if name in _LLM_CLIENT_NAMES:
        import importlib
        mod = importlib.import_module(".llm_client", __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "build_prompt",
    "build_prompt_v2prime",
    "PredicateResponse",
    "StatePredicate",
    "Variable",
    "LLMResult",
    "query_llm",
    "query_llm_in_session",
    "query_llm_followup",
    "query_v2_chunk_candidates",
    "build_user_content",
    "result_to_dict_list",
    "FPFNAnalysisResponse",
    "FPFNResult",
    "FNVariableItem",
    "FPVariableItem",
    "MechanicalProvenanceSeed",
    "build_predicate_bundle",
    "build_predicate_evidence_bundle",
    "build_semantic_contract_bundles",
    "save_semantic_contract_bundles",
    "build_and_save_semantic_contract_bundles",
    "SemanticContractRunResult",
    "load_provenance_seed_map",
    "run_semantic_contract_generation",
    "PredicateVerdict",
    "CriticResponse",
    "CriticResult",
    "serialize_predicate_candidates_with_ids",
    "apply_monotone_decrease",
    "build_critic_user_prompt",
    "query_critic_in_session",
    "run_critic_turn",
]
