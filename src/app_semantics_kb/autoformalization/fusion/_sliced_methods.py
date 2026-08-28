"""fusion._sliced_methods — V3/V4 sliced methods payload를 LFM 프롬프트용 텍스트로 직렬화.

동작 모드:
  V3 (include_analysis_meta=False, cfg_index=None):
    Sliced Methods Source Context + Domain Type Context 만 포함
  V4 (include_analysis_meta=True, cfg_index=<dict>):
    + Resource → Method Index, Call Path Overview
    + 각 [METHOD N]에 forward_calls (**직접 호출 1-hop**); CFG basic block listing은 appendix
"""

from __future__ import annotations

from typing import Any

from ..utils import log, pretty_json


# ─────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────

def render_sliced_methods_blob(
    payload: dict[str, Any],
    *,
    include_analysis_meta: bool = False,
    cfg_index: dict[str, dict[str, str]] | None = None,
) -> str:
    """sliced_methods_payload를 프롬프트용 텍스트 블롭으로 변환한다.

    Args:
        payload: extractors.code_context_loader.build_sliced_methods_payload_from_context_slicer() 결과.
        include_analysis_meta: True면 V4 분석 메타(R→M Index, Call Path Overview, anchors/forward_calls)를 포함.
        cfg_index: V4 전용. method_full_name → {"summary", "blocks"} 인덱스. fusion._cfg.build_cfg_index() 결과.

    프로세스 내 예외는 catch하여 fallback으로 payload JSON dump를 반환한다.
    """
    try:
        return _render_sliced_methods_source_blob(
            payload,
            include_analysis_meta=include_analysis_meta,
            cfg_index=cfg_index,
        )
    except Exception as e:
        log(f"[fusion._sliced_methods] render failed: {e}", "yellow")
        return pretty_json(payload)


# ─────────────────────────────────────────────
# 메인 렌더러
# ─────────────────────────────────────────────

def _render_sliced_methods_source_blob(
    sliced_context_payload: dict[str, Any],
    *,
    include_analysis_meta: bool,
    cfg_index: dict[str, dict[str, str]] | None,
) -> str:
    visible_resource_ids: list[str] = sliced_context_payload.get("visible_resource_ids") or []
    methods: list[dict[str, Any]] = sliced_context_payload.get("methods") or []
    domain_types: list[dict[str, Any]] = sliced_context_payload.get("domain_types") or []

    if not isinstance(visible_resource_ids, list):
        visible_resource_ids = []
    if not isinstance(methods, list):
        methods = []
    if not isinstance(domain_types, list):
        domain_types = []

    visible_set = set(visible_resource_ids)
    out: list[str] = []

    # 섹션 1: Sliced Methods Source Context (V3/V4 공통 기반 — 순서 동일)
    # V4는 per-method 헤더에 anchors/cfg_summary/forward_calls만 추가.
    # CFG basic block listing은 인라인으로 붙이지 않고 섹션 5 appendix에서 분리 렌더링.
    out.append("# Sliced Methods Source Context")
    cfg_entries_for_appendix: list[tuple[int, str, str, str]] = []  # (idx, full_name, summary, blocks)
    for idx, method in enumerate(methods, start=1):
        if not isinstance(method, dict):
            continue
        full_name = str(method.get("method_full_name") or "")
        cfg_entry = (cfg_index or {}).get(full_name)
        out.append("")
        out.append(_render_method_entry(idx, method))
        if include_analysis_meta and cfg_entry and cfg_entry.get("blocks"):
            cfg_entries_for_appendix.append((
                idx,
                full_name,
                cfg_entry.get("summary") or "",
                cfg_entry["blocks"],
            ))

    # 섹션 2: Domain Type Context (V3/V4 공통 기반 — 순서 동일)
    domain_section = _render_domain_types_section(domain_types)
    if domain_section:
        out.append("")
        out.append("---")
        out.append("")
        out.append(domain_section)

    # 섹션 3 (V4 전용 appendix): Resource → Method Index
    # 메서드·도메인 타입 섹션 이후에 배치하여 LLM의 추출 범위가
    # visible resource ID로 과도하게 앵커링되는 것을 방지한다.
    if include_analysis_meta and visible_resource_ids:
        index = _build_resource_to_method_index(visible_resource_ids, methods)
        out.append("")
        out.append("---")
        out.append("")
        out.append(_render_resource_method_index(visible_resource_ids, index))

    # 섹션 4 (V4 전용 appendix): Call Path Overview
    if include_analysis_meta:
        overview = _build_call_path_overview(methods, visible_set)
        if overview:
            out.append("")
            out.append("---")
            out.append("")
            out.append(_render_call_path_overview(overview))

    # 섹션 5 (V4 전용 appendix): CFG Details
    # 소스 코드와 분리하여 방해 없이 제어 흐름 세부 정보를 참조할 수 있도록 한다.
    # cfg_summary는 여기 헤더에 포함하여 복잡도 정보를 제공한다.
    if cfg_entries_for_appendix:
        out.append("")
        out.append("---")
        out.append("")
        out.append("# CFG Details")
        out.append("(Basic block listings for methods with non-trivial control flow.)")
        for method_idx, method_full_name, summary, blocks in cfg_entries_for_appendix:
            short = _short_method_name(method_full_name)
            header = f"## [METHOD {method_idx}] {short}"
            if summary:
                header += f"  ({summary})"
            out.append("")
            out.append(header)
            out.append(blocks)

    return "\n".join(out)


def _direct_forward_callees_for_method(
    method_full_name: str,
    forward_call_paths: object,
) -> list[str]:
    """FORWARD path에서 path_methods[0]==이 메서드일 때만 path_methods[1]을 직접 호출로 수집한다."""
    fqn = str(method_full_name or "").strip()
    if not fqn:
        return []
    raw = forward_call_paths or []
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for path in raw:
        if not isinstance(path, list) or len(path) < 2:
            continue
        src = str(path[0] or "").strip()
        if src != fqn:
            continue
        callee = str(path[1] or "").strip()
        if not callee or callee in seen:
            continue
        seen.add(callee)
        out.append(callee)
    return out


# ─────────────────────────────────────────────
# 메서드 단위 렌더링
# ─────────────────────────────────────────────

def _render_method_entry(
    idx: int,
    method: dict[str, Any],
    *,
    include_analysis_meta: bool = False,
    cfg_summary: str | None = None,
    cfg_blocks: str | None = None,
) -> str:
    """[METHOD N] 단일 블록을 렌더링한다.

    V3/V4 공통: method_full_name → file → (direct forward_calls) → source code 구조로 동일하게 렌더링한다.
    cfg_summary / cfg_blocks은 appendix CFG Details에서 처리한다.
    Call Path Overview(BACKWARD)와 달리 forward는 [METHOD N] 안에서 **직접 호출(1-hop)** 만 표시한다.

    Args:
        include_analysis_meta: 현재 메서드 엔트리 렌더링에는 영향 없음 (appendix 섹션 제어용).
        cfg_summary: appendix CFG Details 섹션에서 사용 (이 함수에서는 무시).
        cfg_blocks: appendix CFG Details 섹션에서 사용 (이 함수에서는 무시).
    """
    source_file = str(method.get("source_file") or "")
    lang = "kotlin" if source_file.endswith(".kt") else ("java" if source_file.endswith(".java") else "")

    source_code = str(method.get("source_code") or "")
    source_code = source_code.replace("```", "` ` `")
    if not source_code.strip():
        source_code = "// source unavailable"

    anchor_location = str(method.get("anchor_location") or source_file or "unknown")

    lines: list[str] = [
        f"[METHOD {idx}]",
        f"method_full_name: {method.get('method_full_name')}",
        f"file: {anchor_location}",
    ]

    callees = _direct_forward_callees_for_method(
        str(method.get("method_full_name") or ""),
        method.get("forward_call_paths"),
    )
    if callees:
        lines.append("forward_calls (direct):")
        for c in callees:
            lines.append(f"  {_short_method_name(c)}")

    lines += [
        f"```{lang}".rstrip(),
        source_code,
        "```",
    ]

    return "\n".join(lines)


def _short_method_name(full_name: str) -> str:
    """FQN에서 패키지를 제거하고 클래스명부터의 short form을 반환한다.

    예:
      com.better.alarm.ui.AlarmsListFragment.configureBottomDrawer.<lambda>17:void()
      → AlarmsListFragment.configureBottomDrawer.<lambda>17

    규칙: `:` 이후 반환형 제거 → `.` split → 대문자로 시작하는 첫 세그먼트부터 끝까지 join.
    """
    name_part = full_name.split(":")[0]
    segments = name_part.split(".")
    for i, seg in enumerate(segments):
        if seg and seg[0].isupper():
            return ".".join(segments[i:])
    return name_part


# ─────────────────────────────────────────────
# 섹션 1 — Resource → Method Index (V4 only)
# ─────────────────────────────────────────────

def _build_resource_to_method_index(
    visible_resource_ids: list[str],
    methods: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """resource_id → 연관 method 항목 리스트의 역인덱스를 생성한다."""
    index: dict[str, list[dict[str, Any]]] = {rid: [] for rid in visible_resource_ids}

    for idx, method in enumerate(methods, start=1):
        if not isinstance(method, dict):
            continue
        full_name = str(method.get("method_full_name") or "")
        short_name = _short_method_name(full_name) if full_name else "unknown"
        for rid in (method.get("resource_ids") or []):
            if rid not in index:
                continue
            index[rid].append({
                "method_idx": idx,
                "method_full_name": full_name,
                "short_name": short_name,
            })

    return index


def _render_resource_method_index(
    visible_resource_ids: list[str],
    index: dict[str, list[dict[str, Any]]],
) -> str:
    out: list[str] = []
    out.append("# Resource → Method Index")
    out.append("")
    out.append(f"Visible resource IDs ({len(visible_resource_ids)}):")
    out.append(", ".join(visible_resource_ids))
    out.append("")

    for rid in visible_resource_ids:
        out.append(f"## {rid}")
        entries = index.get(rid) or []
        if not entries:
            out.append("  (no associated method)")
        else:
            max_idx = max(e["method_idx"] for e in entries)
            idx_width = len(str(max_idx))
            for e in entries:
                idx_str = str(e["method_idx"]).rjust(idx_width)
                out.append(f"  [METHOD {idx_str}] {e['short_name']}")
        out.append("")

    return "\n".join(out)


# ─────────────────────────────────────────────
# 섹션 2 — Call Path Overview (V4 only)
# ─────────────────────────────────────────────

def _build_call_path_overview(
    methods: list[dict[str, Any]],
    visible_resource_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """BACKWARD 경로를 root_callback → [(path_methods, resource_ids)] 구조로 group한다."""
    overview: dict[str, list[dict[str, Any]]] = {}

    for method in methods:
        if not isinstance(method, dict):
            continue
        method_resource_ids = [
            rid for rid in (method.get("resource_ids") or [])
            if rid in visible_resource_ids
        ]
        for bcp in (method.get("backward_call_paths") or []):
            root_cb = str(bcp.get("root_callback") or "")
            path_methods: list[str] = bcp.get("path_methods") or []
            if not root_cb or not path_methods:
                continue

            if root_cb not in overview:
                overview[root_cb] = []

            existing_paths = [tuple(e["path_methods"]) for e in overview[root_cb]]
            if tuple(path_methods) not in existing_paths:
                overview[root_cb].append({
                    "path_methods": path_methods,
                    "resource_ids": method_resource_ids,
                })

    return overview


def _render_call_path_overview(
    overview: dict[str, list[dict[str, Any]]],
) -> str:
    if not overview:
        return ""

    out: list[str] = []
    out.append("# Call Path Overview")
    out.append(
        "(root callback → method call chain; "
        "depth-limited to 6 hops, one representative path per root callback)"
    )
    out.append("")

    for root_cb_fqn, paths in overview.items():
        root_short = _short_method_name(root_cb_fqn)
        out.append(f"  {root_short}")
        for entry in paths:
            path_methods: list[str] = entry["path_methods"]
            resource_ids: list[str] = entry["resource_ids"]

            reversed_path = list(reversed(path_methods))
            chain_parts = [_short_method_name(m) for m in reversed_path[1:]]
            rid_str = f"  [{', '.join(resource_ids)}]" if resource_ids else ""

            if chain_parts:
                chain = " → ".join(chain_parts)
                out.append(f"    → {chain}{rid_str}")
            else:
                out.append(f"    (direct){rid_str}")
        out.append("")

    return "\n".join(out).rstrip()


# ─────────────────────────────────────────────
# 섹션 4 — Domain Type Context (V3/V4 공통)
# ─────────────────────────────────────────────

_KIND_MARKERS: list[tuple[str, str]] = [
    ("data class ", "data class"),
    ("sealed class ", "sealed class"),
    ("enum class ", "enum class"),
    ("interface ", "interface"),
]


def _infer_kotlin_kind(body: str) -> str:
    """Kotlin 소스 body에서 실제 타입 종류를 추출한다."""
    stripped = body.lstrip()
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("@"):
            continue
        for marker, label in _KIND_MARKERS:
            if marker in line:
                return label
        return "class"
    return "class"


def _render_domain_types_section(domain_types: list[dict[str, Any]]) -> str:
    if not domain_types:
        return ""

    out: list[str] = ["# Domain Type Context", ""]
    for entry in domain_types:
        short_name = entry.get("short_name") or entry.get("type_full_name", "").split(".")[-1]
        body = entry.get("body", "").strip()
        file_path = entry.get("file", "")
        kotlin_kind = _infer_kotlin_kind(body)

        header = f"## {short_name}  ({kotlin_kind} · {file_path})" if file_path else f"## {short_name}  ({kotlin_kind})"
        out.append(header)
        if body:
            out.append(body)
        out.append("")

    return "\n".join(out).rstrip()
