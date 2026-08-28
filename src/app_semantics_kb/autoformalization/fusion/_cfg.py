"""fusion._cfg — Joern CFG payload를 컴파일러 IR 스타일 텍스트로 직렬화하는 헬퍼.

V4 전용. 두 가지 사용 방식을 지원한다:
  1. 메서드 단위 통합 (옵션 C, 권장):
     - extract_cfg_summary()로 summary 한 줄 추출
     - render_cfg_blocks_only()로 [B*] block listing만 추출
     - build_cfg_index()는 method_full_name → {"summary", "blocks"} 인덱스 반환
  2. 단일 텍스트 직렬화 (legacy / 디버깅):
     - serialize_static_analysis(): 기존 통합 출력
"""

from __future__ import annotations

from typing import Any

from ..utils import log, pretty_json


# ─────────────────────────────────────────────
# 옵션 C — 메서드별 통합용 헬퍼
# ─────────────────────────────────────────────

def extract_cfg_summary(cfg_payload: dict[str, Any]) -> str:
    """단일 CFG payload에서 'branches=N · paths=K · loops=…' 한 줄을 추출한다."""
    analysis = cfg_payload.get("analysis") or {}
    cfg_summary = analysis.get("cfgSummary") or {}
    block_paths = analysis.get("blockPaths") or []
    branch_count = cfg_summary.get("branchCount", 0)
    path_count = len(block_paths)
    has_loop = cfg_summary.get("hasLoop", False)
    return f"branches={branch_count} · paths≤{path_count} (sampled) · loops={str(has_loop).lower()}"


def render_cfg_blocks_only(cfg_payload: dict[str, Any]) -> str:
    """CFG payload에서 [B*] basic block listing 본문만 반환한다.

    `## fullName (file:line)` 헤더와 `branches=… · paths=…` summary 라인은
    포함하지 않는다 — 호출자가 [METHOD N] 헤더에 이미 두었다고 가정.
    """
    analysis = cfg_payload.get("analysis") or {}
    cfg = analysis.get("cfg") or {}
    blocks: list[dict[str, Any]] = cfg.get("blocks") or []
    entry_id: str = cfg.get("entryBlockId") or (blocks[0]["id"] if blocks else "")
    if not entry_id or not blocks:
        return ""

    ordered = _cfg_topological_order(blocks, entry_id)
    out: list[str] = []

    for block in ordered:
        bid = str(block.get("id", "?"))

        tags: list[str] = []
        if block.get("isEntry"):
            tags.append("ENTRY")
        if block.get("isJoin"):
            tags.append("JOIN")
        if block.get("isExit"):
            tags.append("EXIT")
        tag_str = (" " + " ".join(tags)) if tags else ""

        out.append(f"[{bid}]{tag_str}")

        for instr in (block.get("instructions") or []):
            line = str(instr).strip()
            if line:
                out.append(f"  {line}")

        term = block.get("terminator") or {}
        kind = str(term.get("kind") or "")
        if kind == "branch":
            pred = str(term.get("predicate") or "").strip()
            branches: list[dict[str, Any]] = term.get("branches") or []
            succs = ", ".join(str(b["to"]) for b in branches if b.get("to"))
            pred_part = f"({pred}) " if pred else ""
            out.append(f"  branch {pred_part}succ: {succs}")
        elif kind == "flow":
            succs = block.get("succBlockIds") or []
            if succs:
                out.append(f"  → {succs[0]}")

        out.append("")

    return "\n".join(out).rstrip()


def build_cfg_index(static_analysis_payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    """method_full_name → {"summary": str, "blocks": str} 인덱스를 만든다.

    `_render_method_entry`에서 fullName으로 lookup 해 [METHOD N] 블록에 흡수하는 용도.
    payload가 method_cfg_list 포맷이 아니거나 비어 있으면 빈 dict 반환.
    """
    if not isinstance(static_analysis_payload, dict):
        return {}

    cfg_list = static_analysis_payload.get("method_cfg_list")
    if not isinstance(cfg_list, list):
        return {}

    index: dict[str, dict[str, str]] = {}
    for raw in cfg_list:
        if not isinstance(raw, dict):
            continue
        analysis = raw.get("analysis") or {}
        method = analysis.get("method") or {}
        full_name = str(method.get("fullName") or "")
        if not full_name:
            continue
        index[full_name] = {
            "summary": extract_cfg_summary(raw),
            "blocks": render_cfg_blocks_only(raw),
        }
    return index


# ─────────────────────────────────────────────
# Legacy 통합 직렬화 (단일 텍스트로 dump)
# ─────────────────────────────────────────────

def render_cfg_block_listing(cfg_payload: dict[str, Any]) -> str:
    """단일 CFG payload를 ## 헤더 + summary + block listing으로 통합 렌더링한다.

    옵션 C에서는 사용하지 않지만, 단일 CFG 디버깅 / legacy 호환을 위해 보존.
    """
    analysis = cfg_payload.get("analysis") or {}
    method = analysis.get("method") or {}

    full_name = str(method.get("fullName") or method.get("name") or "unknown")
    location = str(method.get("location") or "")
    name_part = full_name.split(":")[0]
    segs = name_part.split(".")
    short_name = ".".join(segs[-2:]) if len(segs) >= 2 else name_part

    summary = extract_cfg_summary(cfg_payload)
    blocks_text = render_cfg_blocks_only(cfg_payload)

    out: list[str] = [
        f"## {short_name}  ({location})",
        summary,
        "",
    ]
    if blocks_text:
        out.append(blocks_text)
    return "\n".join(out).rstrip()


def serialize_static_analysis(payload: dict[str, Any] | list[Any]) -> str:
    """Joern CFG 출력 JSON을 basic block listing 텍스트로 직렬화한다.

    옵션 C에서는 메서드별 통합 인덱스를 쓰므로 이 함수는 호출되지 않는다.
    legacy 호환과 디버깅 dump 용도로 보존한다.

    지원 포맷:
      - method_cfg_list (build_cfg_context_from_method_cfg_index 출력)
      - 단일 CFG raw (analysis.cfg + analysis.blockPaths)
    """
    if not isinstance(payload, dict):
        log("[fusion._cfg] unexpected static analysis payload type, using JSON dump", "yellow")
        return pretty_json(payload)

    if "method_cfg_list" in payload and isinstance(payload.get("method_cfg_list"), list):
        return _render_cfg_list(payload)

    analysis = payload.get("analysis")
    if isinstance(analysis, dict) and "cfg" in analysis:
        return render_cfg_block_listing(payload)

    log("[fusion._cfg] unrecognised static analysis format, using JSON dump", "yellow")
    return pretty_json(payload)


# ─────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────

def _cfg_topological_order(
    blocks: list[dict[str, Any]],
    entry_id: str,
) -> list[dict[str, Any]]:
    """BFS로 entry부터 도달 가능한 블록을 topological order로 반환한다."""
    block_map: dict[str, dict[str, Any]] = {b["id"]: b for b in blocks if b.get("id")}
    visited: list[str] = []
    queue: list[str] = [entry_id]
    seen: set[str] = {entry_id}
    while queue:
        bid = queue.pop(0)
        block = block_map.get(bid)
        if block is None:
            continue
        visited.append(bid)
        for succ in (block.get("succBlockIds") or []):
            if succ not in seen:
                seen.add(succ)
                queue.append(succ)
    return [block_map[bid] for bid in visited if bid in block_map]


def _render_cfg_list(payload: dict[str, Any]) -> str:
    """build_cfg_context_from_method_cfg_index 출력을 CFG별 block listing으로 렌더링한다."""
    cfg_list: list[dict[str, Any]] = payload.get("method_cfg_list") or []
    meta = payload.get("meta") or {}

    parts: list[str] = [
        "# CFG Context",
        f"methods: {meta.get('cfg_loaded_count', len(cfg_list))} / {meta.get('involved_methods_count', '?')}",
        "",
    ]
    for raw in cfg_list:
        if not isinstance(raw, dict):
            continue
        parts.append(render_cfg_block_listing(raw))
        parts.append("")

    return "\n".join(parts).rstrip()
