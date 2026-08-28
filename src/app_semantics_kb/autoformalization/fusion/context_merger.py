"""context_merger — Variant별 컨텍스트 병합 dispatcher.

extractors/ 가 로드한 이질적인 데이터(a11y XML, 소스 코드, Joern 정적 분석 JSON)를
prompt_builder가 바로 소비할 수 있는 단일 MergedContext로 조립한다.

본 모듈은 dispatcher 역할만 수행하며, 실제 렌더링 로직은 다음 헬퍼 모듈에 위임한다:
  - fusion._a11y           : a11y XML 경량화
  - fusion._sliced_methods : V3/V4 sliced methods 텍스트 블롭
  - fusion._cfg            : V4 CFG basic block listing 인덱스
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from ..utils import log, pretty_json
from ._a11y import compact_a11y
from ._cfg import build_cfg_index
from ._sliced_methods import render_sliced_methods_blob


class MergedContext(TypedDict):
    """prompt_builder가 소비하는 단일 컨텍스트 딕셔너리.

    각 필드명은 프롬프트 템플릿의 {변수명}과 직접 대응한다.
    Variant에서 사용하지 않는 필드는 None으로 설정된다.
    """

    variant: int
    app_name: str
    screenshot_path: Path | None
    accessibility_tree: str
    existing_predicates: str
    raw_source_code: str | None
    sliced_methods_source_blob: str | None
    cfg_context_json: str | None


def merge_context(
    *,
    variant: int,
    a11y_xml_text: str,
    screenshot_path: Path | None,
    existing_predicates: list[dict[str, Any]],
    app_name: str = "AlarmClock",
    raw_source_code: str | None = None,
    sliced_methods_payload: dict[str, Any] | None = None,
    static_analysis_payload: dict[str, Any] | list[Any] | None = None,
) -> MergedContext:
    """Variant별로 컨텍스트를 조립하여 MergedContext를 반환한다.

    Variant policy:
      V1: a11y + screenshot 만
      V2: V1 + raw_source_code
      V3: V1 + sliced methods (코드 로직만; 분석 메타 제외)
      V4: V1 + sliced methods (분석 메타 포함, [METHOD N]에 CFG 흡수 — 옵션 C)

    Args:
        variant: Test Variant 번호 (1~4).
        app_name: User prompt에 기록할 앱 이름.
        a11y_xml_text: adb dump로 얻은 a11y XML 원문.
        screenshot_path: 캡처된 스크린샷 PNG 경로 (없으면 None).
        existing_predicates: 기존 State_Definitions 목록.
        raw_source_code: V2용 전체 소스 코드 텍스트.
        sliced_methods_payload: V3/V4용 sliced methods context dict.
        static_analysis_payload: V4용 정적 분석 JSON. method_cfg_list 포맷 권장.

    Returns:
        MergedContext — prompt_builder.py에 직접 전달 가능한 딕셔너리.
    """
    if variant not in (1, 2, 3, 4):
        raise ValueError(f"variant must be 1~4, got {variant!r}")

    a11y_compact = compact_a11y(a11y_xml_text)
    existing_json = pretty_json(existing_predicates)

    raw_source = None
    sliced_blob = None
    cfg_text = None

    if variant == 2:
        raw_source = raw_source_code or ""
        if not raw_source:
            log("[context_merger] variant 2: raw_source_code is empty", "yellow")

    if variant in (3, 4):
        if sliced_methods_payload is None:
            raise ValueError(f"variant {variant} requires sliced_methods_payload")

        cfg_index: dict[str, dict[str, str]] | None = None
        if variant == 4:
            if static_analysis_payload is None:
                raise ValueError("variant 4 requires static_analysis_payload")
            if not isinstance(static_analysis_payload, dict):
                log(
                    "[context_merger] V4 static_analysis_payload is not a dict; "
                    "CFG will be skipped",
                    "yellow",
                )
            else:
                cfg_index = build_cfg_index(static_analysis_payload)
                if not cfg_index:
                    log(
                        "[context_merger] V4 cfg_index is empty (payload may not be in "
                        "method_cfg_list format)",
                        "yellow",
                    )

        sliced_blob = render_sliced_methods_blob(
            sliced_methods_payload,
            include_analysis_meta=(variant == 4),
            cfg_index=cfg_index,
        )

        # 옵션 C: V4 CFG는 sliced_blob의 [METHOD N] 블록에 인라인으로 흡수되므로
        #         별도 cfg_context_json은 더 이상 사용하지 않는다.
        cfg_text = None

    log(
        f"[context_merger] variant={variant} "
        f"a11y_chars={len(a11y_compact)} "
        f"predicates={len(existing_predicates)} "
        f"raw_src={'yes' if raw_source else 'no'} "
        f"sliced_methods_blob={'yes' if sliced_blob else 'no'} "
        f"cfg_inline={'yes' if variant == 4 else 'no'}",
        "blue",
    )

    return MergedContext(
        variant=variant,
        app_name=app_name,
        screenshot_path=screenshot_path,
        accessibility_tree=a11y_compact,
        existing_predicates=existing_json,
        raw_source_code=raw_source,
        sliced_methods_source_blob=sliced_blob,
        cfg_context_json=cfg_text,
    )
