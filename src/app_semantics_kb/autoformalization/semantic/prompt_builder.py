"""
prompt_builder.py — Variant별 LLM 프롬프트 조립 모듈

MergedContext를 받아 python/prompts/*.txt 템플릿 파일을 로드하고,
변수를 삽입하여 (system_prompt, user_prompt) 튜플을 반환한다.

AGENTS.md §3.4 준수: 모든 프롬프트는 .txt 파일로 분리하며,
코드 내에 프롬프트 문자열을 직접 하드코딩하지 않는다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from ..fusion.context_merger import MergedContext
from ..utils import log

_DEFAULT_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_VARIANT_USER_PROMPT_FILE: dict[int, str] = {
    1: "user_variant1.txt",
    2: "user_variant2.txt",
    3: "user_variant3.txt",
    4: "user_variant4.txt",
}

# Variant 1 uses UI-only system prompt. Variant 2 injects raw source into a
# cache-friendly system prefix. Variants 3/4 use the code-grounded instruction prompt.
_VARIANT_SYSTEM_PROMPT_FILE: dict[int, str] = {
    1: "system_prompt_variant1.txt",
    2: "system_prompt_variant234.txt",
    3: "system_prompt_variant234.txt",
    4: "system_prompt_variant234.txt",
}


# ─────────────────────────────────────────────
# 템플릿 로드 (프로세스 내 캐싱)
# ─────────────────────────────────────────────

@lru_cache(maxsize=16)
def _load_template(path: Path) -> str:
    """프롬프트 .txt 파일을 읽어 문자열로 반환한다. 결과는 lru_cache로 캐싱된다."""
    if not path.exists():
        raise FileNotFoundError(f"prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _get_system_prompt(variant: int, prompts_dir: Path) -> str:
    filename = _VARIANT_SYSTEM_PROMPT_FILE.get(variant)
    if filename is None:
        raise ValueError(f"no system prompt file for variant {variant!r}")
    return _load_template(prompts_dir / filename)


def _get_user_template(variant: int, prompts_dir: Path) -> str:
    filename = _VARIANT_USER_PROMPT_FILE.get(variant)
    if filename is None:
        raise ValueError(f"no prompt template for variant {variant!r}")
    return _load_template(prompts_dir / filename)


# ─────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────

def build_prompt(
    variant: int,
    context: MergedContext,
    prompts_dir: Path | None = None,
) -> tuple[str, str]:
    """Variant와 MergedContext를 받아 (system_prompt, user_prompt)를 반환한다.

    Args:
        variant: Test Variant 번호 (1~4).
        context: fusion.context_merger.merge_context()의 반환값.
        prompts_dir: 프롬프트 .txt 파일 디렉토리. None이면 python/prompts/ 사용.

    Returns:
        (system_prompt, user_prompt) 튜플 — llm_client.query_llm()에 직접 전달 가능.

    Raises:
        FileNotFoundError: 템플릿 파일이 없는 경우.
        ValueError: 지원하지 않는 Variant인 경우.
        KeyError: 템플릿의 {변수명}이 context에 없는 경우.
    """
    if variant not in (1, 2, 3, 4):
        raise ValueError(f"variant must be 1~4, got {variant!r}")

    resolved_dir = prompts_dir or _DEFAULT_PROMPTS_DIR
    user_template = _get_user_template(variant, resolved_dir)
    format_kwargs = _make_format_kwargs(context)

    if variant == 2:
        system_prompt = _build_v2_source_cached_system_prompt(
            format_kwargs=format_kwargs,
            prompts_dir=resolved_dir,
        )
    else:
        system_prompt = _get_system_prompt(variant, resolved_dir)

    user_prompt = user_template.format(**format_kwargs)

    log(
        f"[prompt_builder] variant={variant} "
        f"system_chars={len(system_prompt)} "
        f"user_chars={len(user_prompt)}",
        "blue",
    )
    return system_prompt, user_prompt


def _build_v2_source_cached_system_prompt(
    *,
    format_kwargs: dict[str, Any],
    prompts_dir: Path,
) -> str:
    """V2 기본 실험용 source-cache system prompt를 만든다."""
    system_instructions = _load_template(prompts_dir / "system_prompt_variant234.txt")
    system_template = _load_template(prompts_dir / "system_source_cached.txt")
    return system_template.format(
        system_instructions=system_instructions,
        raw_source_code=format_kwargs["raw_source_code"],
    )


def build_prompt_v2prime(
    context: MergedContext,
    prompts_dir: Path | None = None,
) -> tuple[str, str]:
    """V2' / S1 프롬프트를 구성한다.

    전체 source code는 system prompt prefix에 고정하고, user prompt에는 현재 화면과
    기존 predicate chain만 둔다. V2'는 멀티턴/S1 전용 user template을 사용한다.
    """
    resolved_dir = prompts_dir or _DEFAULT_PROMPTS_DIR
    system_instructions = _load_template(resolved_dir / "system_prompt_variant234.txt")
    system_template = _load_template(resolved_dir / "system_source_cached.txt")
    user_template = _load_template(resolved_dir / "user_step1_v2prime.txt")
    format_kwargs = _make_format_kwargs(context)

    system_prompt = system_template.format(
        system_instructions=system_instructions,
        raw_source_code=format_kwargs["raw_source_code"],
    )
    user_prompt = user_template.format(
        app_name=format_kwargs["app_name"],
        existing_predicates=format_kwargs["existing_predicates"],
        accessibility_tree=format_kwargs["accessibility_tree"],
    )

    log(
        f"[prompt_builder] v2prime "
        f"system_chars={len(system_prompt)} "
        f"user_chars={len(user_prompt)}",
        "blue",
    )
    return system_prompt, user_prompt


# ─────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────

def _make_format_kwargs(context: MergedContext) -> dict[str, Any]:
    """MergedContext에서 .format()에 필요한 kwargs dict를 만든다.

    None 값은 빈 문자열로 치환하여 KeyError 없이 모든 템플릿 변수를 처리한다.
    Path 타입은 문자열로 변환한다.
    """
    def _to_str(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, Path):
            return str(v)
        return str(v)

    return {
        "app_name": _to_str(context.get("app_name")),
        "existing_predicates": _to_str(context.get("existing_predicates")),
        "accessibility_tree": _to_str(context.get("accessibility_tree")),
        "raw_source_code": _to_str(context.get("raw_source_code")),
        "sliced_methods_source_blob": _to_str(context.get("sliced_methods_source_blob")),
        "cfg_context_json": _to_str(context.get("cfg_context_json")),
    }
