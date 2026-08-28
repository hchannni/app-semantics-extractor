"""
code_context_loader.py — Variant별 코드 컨텍스트 데이터 로더

각 Test Variant에서 LFM 프롬프트에 주입할 코드 관련 컨텍스트를 로드하는 순수 데이터
로딩 계층이다. 소스 코드의 파싱, AST 분석, 정적 분석 실행은 이 모듈의 책임이 아니다.
변환·직렬화 로직은 fusion/context_merger.py 의 영역이다.

Joern Context Slicer 출력 구조 (신규, 3파일 분리):
  context-slicer-output/
    slices.json        — MethodSlice[] (메타데이터 + refs, 코드 없음)
    method-bodies.json — {method_full_name: MethodInfo} 전역 lookup
    type-index.json    — {type_full_name: TypeInfo} 전역 lookup

Variant별 로드 담당:
  Variant 2 — load_raw_source_code()         : 런타임 .kt/.java 소스 파일 연결
  Variant 3/4 — build_sliced_methods_payload_from_context_slicer() : context-slicer-output/ → sliced methods
    (visible 슬라이스의 method_body_refs 합집합으로 본문 행 확장; method_slice_role: primary | body_ref)
  Variant 4 — build_cfg_context_from_method_cfg_index() : method-cfg-index + method_body_refs → CFG list
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, TypedDict

from ..utils import log, read_json, sha256_file, write_json

# 소스 코드 수집 시 무시할 디렉토리 이름
_IGNORED_DIRS: frozenset[str] = frozenset({"build", ".gradle", ".idea", "__pycache__", ".git"})

# 수집 대상 소스 파일 확장자
_SOURCE_EXTENSIONS: frozenset[str] = frozenset({".kt", ".java"})


class RawSourceChunkFile(TypedDict):
    source_path: str
    start_line: int
    end_line: int
    char_count: int
    sha256: str


class RawSourceChunk(TypedDict):
    chunk_id: str
    files: list[RawSourceChunkFile]
    source_paths: list[str]
    file_count: int
    char_count: int
    sha256: str
    oversized_file: bool
    source_text: str


def _normalize_resource_id(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    raw = resource_id.strip()
    if not raw:
        return None
    if raw.startswith("R.id."):
        return raw
    m = re.search(r":id/([A-Za-z0-9_]+)$", raw)
    if m:
        return f"R.id.{m.group(1)}"
    return None


def _lightweight_method_path(full_name: str) -> str:
    """메서드 full_name에서 패키지 제거, ClassName.methodName:Signature만 반환.

    시그니처 내부에 android.view.View 등 '.' 포함 가능하므로, 첫 번째 ':' 기준으로
    앞부분만 split하여 클래스·메서드명 추출.

    예: com.better.alarm.ui.list.AlarmListAdapter.getView:android.view.View(...)
        → AlarmListAdapter.getView:android.view.View(...)
    """
    if not full_name or not isinstance(full_name, str):
        return str(full_name or "")
    if ":" not in full_name:
        parts = full_name.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else full_name
    before_colon, _, after_colon = full_name.partition(":")
    parts = before_colon.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:]) + ":" + after_colon
    return full_name


def _extract_resource_ids_from_a11y(a11y_xml_text: str) -> set[str]:
    """a11y XML에서 resource-id를 추출하여 정규화된 R.id.<name> 집합으로 반환한다."""
    out: set[str] = set()
    try:
        root = ET.fromstring(a11y_xml_text)
        for node in root.iter("node"):
            raw = node.attrib.get("resource-id")
            if raw:
                n = _normalize_resource_id(raw)
                if n:
                    out.add(n)
    except ET.ParseError:
        pass
    return out


# ─────────────────────────────────────────────
# Variant 2: Raw Source Code
# ─────────────────────────────────────────────

def _is_test_source_set(source_set: str) -> bool:
    normalized = source_set.lower()
    return (
        normalized == "test"
        or normalized.startswith("test")
        or normalized.startswith("androidtest")
        or normalized.endswith("test")
    )


def _android_source_set_for_path(source_root: Path, source_file: Path) -> str | None:
    """Return the Gradle/Android source set name when it is visible from source_root."""
    try:
        relative_parts = source_file.relative_to(source_root).parts
    except ValueError:
        relative_parts = source_file.parts

    if source_root.name == "src" and relative_parts:
        return relative_parts[0]

    for index, part in enumerate(relative_parts[:-1]):
        if part == "src" and index + 1 < len(relative_parts):
            return relative_parts[index + 1]
    return None


def _is_runtime_source_file(source_root: Path, source_file: Path) -> bool:
    source_set = _android_source_set_for_path(source_root, source_file)
    if source_set is None:
        return True
    return not _is_test_source_set(source_set)


def _source_files_under(source_root: Path) -> list[Path]:
    source_files: list[Path] = []
    for root, dirs, filenames in os.walk(source_root):
        dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
        for fn in filenames:
            p = Path(root) / fn
            if (
                p.suffix.lower() in _SOURCE_EXTENSIONS
                and _is_runtime_source_file(source_root, p)
            ):
                source_files.append(p)
    return sorted(source_files, key=lambda x: str(x))


def load_raw_source_code(source_root: Path) -> str:
    """Variant 2: 앱 런타임 .kt/.java 파일을 하나의 텍스트 블롭으로 반환한다.

    Android/Gradle source set이 경로에서 보이는 경우 `test`, `androidTest`, `commonTest` 등
    테스트 source set은 제외한다. `src/main`, flavor, build type source set은 포함하며,
    `app/src/main/java`처럼 좁은 root가 직접 들어온 경우에는 그대로 수집한다.

    Args:
        source_root: Android 앱 소스 루트 경로 (예: samples/SimpleAlarmClock/app-source-code).

    Returns:
        파일 헤더와 함께 연결된 전체 소스 코드 문자열. source_root가 존재하지 않으면 빈 문자열.
    """
    if not source_root.exists():
        log(f"[code_context_loader] source_root not found: {source_root}", "yellow")
        return ""

    source_files = _source_files_under(source_root)

    if not source_files:
        log(f"[code_context_loader] no .kt/.java files found under: {source_root}", "yellow")
        return ""

    parts: list[str] = []
    for p in source_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log(f"[code_context_loader] failed to read {p}: {e}", "yellow")
            continue
        rel = p.relative_to(source_root)
        parts.append(f"\n\n===== FILE: {rel} =====\n{text}")

    log(f"[code_context_loader] variant 2: loaded {len(source_files)} source files", "blue")
    return "".join(parts)


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _raw_source_chunk_text(files: list[tuple[RawSourceChunkFile, str]]) -> str:
    parts: list[str] = []
    for file_meta, text in files:
        parts.append(f"\n\n===== FILE: {file_meta['source_path']} =====\n{text}")
    return "".join(parts)


def _build_raw_source_chunk(
    *,
    chunk_id: str,
    files: list[tuple[RawSourceChunkFile, str]],
    max_chars: int,
) -> RawSourceChunk:
    source_text = _raw_source_chunk_text(files)
    file_metas = [file_meta for file_meta, _ in files]
    return {
        "chunk_id": chunk_id,
        "files": file_metas,
        "source_paths": [file_meta["source_path"] for file_meta in file_metas],
        "file_count": len(file_metas),
        "char_count": len(source_text),
        "sha256": sha256(source_text.encode("utf-8", errors="replace")).hexdigest(),
        "oversized_file": len(file_metas) == 1 and len(source_text) > max_chars,
        "source_text": source_text,
    }


def build_raw_source_file_chunks(
    source_root: Path,
    *,
    target_chars: int = 200000,
    max_chars: int = 250000,
) -> list[RawSourceChunk]:
    """Variant 2-chunked: load raw .kt/.java source as deterministic packed chunks.

    Files are atomic: a source file is never split across chunks. `target_chars`
    and `max_chars` guide multi-file packing; if one file exceeds `max_chars`, it
    becomes a single oversized chunk. This is a mechanical packaging layer only.
    It does not parse, slice, summarize, rank, or filter source by UI/resource
    relevance.
    """
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    effective_target_chars = min(target_chars, max_chars)

    if not source_root.exists():
        log(f"[code_context_loader] source_root not found: {source_root}", "yellow")
        return []

    source_files = _source_files_under(source_root)
    if not source_files:
        log(f"[code_context_loader] no .kt/.java files found under: {source_root}", "yellow")
        return []

    chunks: list[RawSourceChunk] = []
    current_files: list[tuple[RawSourceChunkFile, str]] = []
    current_chars = 0

    def flush_current() -> None:
        nonlocal current_files, current_chars
        if not current_files:
            return
        chunk_id = f"src_{len(chunks) + 1:04d}"
        chunks.append(
            _build_raw_source_chunk(
                chunk_id=chunk_id,
                files=current_files,
                max_chars=max_chars,
            )
        )
        current_files = []
        current_chars = 0

    for p in source_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log(f"[code_context_loader] failed to read {p}: {e}", "yellow")
            continue
        if not text.strip():
            continue
        rel = p.relative_to(source_root).as_posix()
        file_meta: RawSourceChunkFile = {
            "source_path": rel,
            "start_line": 1,
            "end_line": _line_count(text),
            "char_count": len(text),
            "sha256": sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        }
        file_entry = (file_meta, text)
        file_chunk_chars = len(_raw_source_chunk_text([file_entry]))
        projected_chars = current_chars + file_chunk_chars

        if current_files and (
            current_chars >= effective_target_chars or projected_chars > max_chars
        ):
            flush_current()

        current_files.append(file_entry)
        current_chars += file_chunk_chars

    flush_current()

    log(
        f"[code_context_loader] variant 2-chunked: built {len(chunks)} chunks "
        f"from {sum(chunk['file_count'] for chunk in chunks)} source files",
        "blue",
    )
    return chunks


# ─────────────────────────────────────────────
# Variant 3/4: context-slicer-output/ → sliced methods
# ─────────────────────────────────────────────

def load_context_slicer_output(
    context_slicer_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Joern context-slicer-output/ 디렉토리에서 slices.json + method-bodies.json + type-index.json을 로드한다.

    Args:
        context_slicer_dir: Joern이 생성한 context-slicer-output 디렉토리 경로.

    Returns:
        (slices, method_bodies, type_index) 튜플.
          slices: MethodSlice[] (dict 리스트)
          method_bodies: {method_full_name: {body, file, start_line, end_line, signature}} lookup
          type_index: {type_full_name: {kind, file, start_line, end_line, body}} lookup
    """
    slices_path = context_slicer_dir / "slices.json"
    method_bodies_path = context_slicer_dir / "method-bodies.json"
    type_index_path = context_slicer_dir / "type-index.json"

    if not context_slicer_dir.is_dir():
        raise FileNotFoundError(
            f"context-slicer-output directory not found: {context_slicer_dir}"
        )
    if not slices_path.exists():
        raise FileNotFoundError(f"slices.json not found: {slices_path}")
    if not method_bodies_path.exists():
        raise FileNotFoundError(f"method-bodies.json not found: {method_bodies_path}")

    with open(slices_path, "r", encoding="utf-8") as f:
        slices_raw = json.load(f)
    if not isinstance(slices_raw, list):
        raise ValueError(
            f"slices.json must be a JSON array, got {type(slices_raw).__name__}: {slices_path}"
        )
    slices = [item for item in slices_raw if isinstance(item, dict)]

    with open(method_bodies_path, "r", encoding="utf-8") as f:
        method_bodies_raw = json.load(f)
    if not isinstance(method_bodies_raw, dict):
        raise ValueError(
            f"method-bodies.json must be a JSON object, got {type(method_bodies_raw).__name__}"
        )
    method_bodies: dict[str, dict[str, Any]] = {
        k: v for k, v in method_bodies_raw.items() if isinstance(v, dict)
    }

    type_index: dict[str, dict[str, Any]] = {}
    if type_index_path.exists():
        with open(type_index_path, "r", encoding="utf-8") as f:
            type_index_raw = json.load(f)
        if isinstance(type_index_raw, dict):
            type_index = {k: v for k, v in type_index_raw.items() if isinstance(v, dict)}
        else:
            log(
                f"[code_context_loader] type-index.json has unexpected format, skipping: {type_index_path}",
                "yellow",
            )
    else:
        log(
            f"[code_context_loader] type-index.json not found, domain type context disabled: {type_index_path}",
            "yellow",
        )

    log(
        f"[code_context_loader] loaded context-slicer-output: "
        f"slices={len(slices)}, method_bodies={len(method_bodies)}, type_index={len(type_index)}",
        "blue",
    )
    return slices, method_bodies, type_index


def _build_path_overviews_from_call_paths(
    call_paths: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """slice 레벨 call_paths(BACKWARD)에서 per-anchor path_overview 리스트를 재구성한다.

    신규 스키마에서는 path_overviews가 usage 레벨이 아닌 slice 레벨에 있으므로,
    해당 슬라이스의 모든 visible anchor에 동일하게 적용할 공통 overview를 생성한다.
    """
    overviews: list[dict[str, Any]] = []
    for cp in call_paths:
        if str(cp.get("direction") or "").upper() != "BACKWARD":
            continue
        path_methods: list[str] = [
            str(m) for m in (cp.get("path_methods") or []) if m
        ]
        root_callback = str(cp.get("root_callback") or "")
        callback_kind = str(cp.get("callback_kind") or "OTHER")
        overviews.append({
            "resolution_path": [_lightweight_method_path(m) for m in path_methods],
            "root_callback": _lightweight_method_path(root_callback),
            "callback_kind": callback_kind,
            "resolution_depth": max(0, len(path_methods) - 1),
        })
    return overviews


def _extract_forward_call_paths(
    call_paths: list[dict[str, Any]],
    primary_method: str = "",
) -> list[list[str]]:
    """slice 레벨 call_paths(FORWARD)에서 downstream 호출 체인 리스트를 추출한다.

    FORWARD 경로는 primary method가 호출하는 downstream 함수 체인을 나타낸다.
    Joern slicer는 root callback의 FORWARD 경로도 slice에 포함하므로,
    primary_method로 시작하는 경로만 필터링하여 다른 메서드의 call chain이
    잘못 귀속되는 것을 방지한다.
    중복 path_methods 리스트는 제거하여 반환한다.
    """
    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    for cp in call_paths:
        if str(cp.get("direction") or "").upper() != "FORWARD":
            continue
        path_methods: list[str] = [
            str(m) for m in (cp.get("path_methods") or []) if m
        ]
        if not path_methods:
            continue
        if primary_method and path_methods[0] != primary_method:
            continue
        key = tuple(path_methods)
        if key not in seen:
            seen.add(key)
            result.append(path_methods)
    return result


def _collect_domain_types(
    type_refs: list[str],
    type_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """domain_type_refs FQN 목록에 해당하는 타입을 필터링 없이 전부 반환한다.

    type_refs 순서를 유지하여 type_index에서 조회하고,
    body 줄 수 오름차순으로 정렬하여 반환한다.

    Returns:
        타입 entry 리스트. 각 항목은 type_full_name, short_name, kind, file, body 키를 포함.
    """
    result: list[dict[str, Any]] = []
    for fqn in type_refs:
        entry = type_index.get(fqn)
        if not entry:
            continue
        body: str = entry.get("body") or ""
        body_lines = body.count("\n") + 1 if body.strip() else 0
        result.append({
            "type_full_name": fqn,
            "short_name": fqn.split(".")[-1],
            "kind": entry.get("kind", "CLASS"),
            "file": entry.get("file", ""),
            "body": body,
            "body_lines": body_lines,
        })
    result.sort(key=lambda x: x["body_lines"])
    return result


def _extract_backward_call_paths(
    call_paths: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """slice 레벨 call_paths(BACKWARD)에서 upstream 호출 체인 리스트를 추출한다.

    BACKWARD 경로는 root callback에서 primary method까지의 호출 경로를 나타낸다.
    각 항목은 path_methods(primary→root 순서), root_callback FQN, callback_kind를 포함한다.
    중복 path_methods 리스트는 제거하여 반환한다.
    """
    seen: set[tuple[str, ...]] = set()
    result: list[dict[str, Any]] = []
    for cp in call_paths:
        if str(cp.get("direction") or "").upper() != "BACKWARD":
            continue
        path_methods: list[str] = [
            str(m) for m in (cp.get("path_methods") or []) if m
        ]
        if not path_methods:
            continue
        key = tuple(path_methods)
        if key not in seen:
            seen.add(key)
            result.append({
                "path_methods": path_methods,
                "root_callback": str(cp.get("root_callback") or ""),
                "callback_kind": str(cp.get("callback_kind") or "OTHER"),
            })
    return result


def _body_ref_method_entry_from_bodies(
    fqn: str,
    method_bodies_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """`method_body_refs`에만 등장해 primary 키에 없었던 FQN용 행: 본문은 lookup, 앵커·path 메타는 비움."""
    body_info = method_bodies_lookup.get(fqn) or {}
    body = str(body_info.get("body") or "")
    file_path = str(body_info.get("file") or "")
    start_line = body_info.get("start_line")
    end_line = body_info.get("end_line")
    return {
        "method_full_name": fqn,
        "method_slice_role": "body_ref",
        "resource_ids": [],
        "backward_call_paths": [],
        "forward_call_paths": [],
        "source_file": file_path,
        "anchor_location": f"{file_path}:{start_line}" if file_path and start_line is not None else None,
        "range_start_line": start_line,
        "range_end_line": end_line,
        "source_code": body,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _resource_slice_record(
    *,
    slice_index: int,
    primary_method: str,
    method_slice: dict[str, Any],
    usage_codes: list[str],
    forward_call_paths: list[list[str]],
    backward_call_paths: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "slice_index": slice_index,
        "primary_method": primary_method,
        "method_body_refs": _string_list(method_slice.get("method_body_refs")),
        "domain_type_refs": _string_list(method_slice.get("domain_type_refs")),
        "usage_codes": usage_codes,
        "forward_call_paths": forward_call_paths,
        "backward_call_paths": backward_call_paths,
    }


def build_sliced_methods_payload_from_context_slicer(
    *,
    a11y_path: Path,
    context_slicer_dir: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Variant 3/4: context-slicer-output/ 디렉토리를 로드하여 sliced_methods_payload를 생성한다.

    visible 슬라이스마다 primary_method 본문을 싣고, 같은 슬라이스·같은 화면에서 합쳐진
    ``method_body_refs`` 집합에 대해 primary로 아직 없는 FQN은 body_ref 행으로 method-bodies
    lookup 본문을 추가한다 (토큰 상한은 호출 측/후속 작업).

    Args:
        a11y_path: adb dump로 생성된 a11y XML 파일 경로.
        context_slicer_dir: Joern이 생성한 context-slicer-output 디렉토리 경로.
        output_path: 결과 JSON 저장 경로 (None이면 저장 생략).

    Returns:
        sliced_methods_payload dict. 'methods', 'stats', 'meta' 키를 포함.
    """
    if not a11y_path.exists():
        raise FileNotFoundError(f"a11y xml not found: {a11y_path}")

    a11y_xml_text = a11y_path.read_text(encoding="utf-8")
    visible_resource_ids = _extract_resource_ids_from_a11y(a11y_xml_text)

    method_slices, method_bodies_lookup, type_index = load_context_slicer_output(context_slicer_dir)

    # anchor_usage_overview: resource_id -> [ { usage_code, path_overviews } ]
    anchor_usage_overview: dict[str, list[dict[str, Any]]] = {}
    for rid in sorted(visible_resource_ids):
        anchor_usage_overview[rid] = []
    resource_slice_index: dict[str, list[dict[str, Any]]] = {
        rid: [] for rid in sorted(visible_resource_ids)
    }

    # method_full_name -> method entry (dedupe)
    methods_by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
    methods_input_total = 0
    # domain_type_refs union: visible 메서드 슬라이스에서 참조되는 타입 FQN 집합
    domain_type_refs_union: list[str] = []
    domain_type_refs_seen: set[str] = set()
    method_body_refs_union: set[str] = set()

    for slice_index, ms in enumerate(method_slices, start=1):
        if ms.get("incomplete"):
            continue

        # 신규 스키마: primary_method는 최상위 문자열 필드
        primary_method = str(ms.get("primary_method") or "").strip()
        if not primary_method:
            continue

        affecting = ms.get("affecting_usages") or []
        call_paths = ms.get("call_paths") or []

        # slice 레벨 path_overviews 재구성 (BACKWARD → depth 계산용)
        path_overviews_for_slice = _build_path_overviews_from_call_paths(call_paths)
        # FORWARD 호출 체인 추출 (primary_method 자신으로 시작하는 경로만)
        forward_call_paths = _extract_forward_call_paths(call_paths, primary_method)
        # BACKWARD 호출 체인 추출 (root callback → primary method 경로)
        backward_call_paths = _extract_backward_call_paths(call_paths)

        resource_ids: list[str] = []
        usage_codes_by_resource_id: dict[str, list[str]] = {}
        for usage in affecting:
            anchor_id = usage.get("anchor_id")
            if isinstance(anchor_id, str) and anchor_id.strip():
                if anchor_id in visible_resource_ids:
                    if anchor_id not in resource_ids:
                        resource_ids.append(anchor_id)
                    # anchor_usage_overview에 usage_code + slice 레벨 path_overviews 추가
                    usage_code = ""
                    up = usage.get("usage_point") or {}
                    if isinstance(up, dict):
                        usage_code = str(up.get("code") or "").strip()
                    if usage_code:
                        usage_codes_by_resource_id.setdefault(anchor_id, [])
                        if usage_code not in usage_codes_by_resource_id[anchor_id]:
                            usage_codes_by_resource_id[anchor_id].append(usage_code)
                    anchor_usage_overview[anchor_id].append({
                        "usage_code": usage_code,
                        "path_overviews": path_overviews_for_slice,
                    })

        if not resource_ids:
            continue

        methods_input_total += 1
        for rid in resource_ids:
            resource_slice_index[rid].append(
                _resource_slice_record(
                    slice_index=slice_index,
                    primary_method=primary_method,
                    method_slice=ms,
                    usage_codes=usage_codes_by_resource_id.get(rid, []),
                    forward_call_paths=forward_call_paths,
                    backward_call_paths=backward_call_paths,
                )
            )

        for ref in (ms.get("method_body_refs") or []):
            if isinstance(ref, str) and ref.strip():
                method_body_refs_union.add(ref.strip())

        # visible 슬라이스의 domain_type_refs 누적 (순서 보존 dedup)
        for ref in (ms.get("domain_type_refs") or []):
            if isinstance(ref, str) and ref not in domain_type_refs_seen:
                domain_type_refs_seen.add(ref)
                domain_type_refs_union.append(ref)

        # method body는 method-bodies.json lookup에서 조회
        body_info = method_bodies_lookup.get(primary_method) or {}
        body = str(body_info.get("body") or "")
        file_path = str(body_info.get("file") or "")
        start_line = body_info.get("start_line")
        end_line = body_info.get("end_line")

        if primary_method not in methods_by_key:
            methods_by_key[primary_method] = {
                "method_full_name": primary_method,
                "method_slice_role": "primary",
                "resource_ids": [],
                "backward_call_paths": backward_call_paths,
                "forward_call_paths": forward_call_paths,
                "source_file": file_path,
                "anchor_location": f"{file_path}:{start_line}" if file_path and start_line is not None else None,
                "range_start_line": start_line,
                "range_end_line": end_line,
                "source_code": body,
            }

        entry = methods_by_key[primary_method]
        for rid in resource_ids:
            if rid not in entry["resource_ids"]:
                entry["resource_ids"].append(rid)

    primary_key_set = frozenset(methods_by_key.keys())
    for fqn in sorted(method_body_refs_union - set(methods_by_key.keys())):
        methods_by_key[fqn] = _body_ref_method_entry_from_bodies(fqn, method_bodies_lookup)

    output_methods = list(methods_by_key.values())
    methods_primary_unique = len(primary_key_set)
    methods_body_ref_rows_added = len(output_methods) - methods_primary_unique
    domain_types = _collect_domain_types(domain_type_refs_union, type_index)
    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "context_slicer_output",
            "context_slicer_dir": str(context_slicer_dir),
            "a11y_path": str(a11y_path),
            "collection_policy": (
                "a11y_filtered_method_slices_with_body_ref_rows"
            ),
        },
        "visible_resource_ids": sorted(visible_resource_ids),
        "anchor_usage_overview": anchor_usage_overview,
        "resource_slice_index": resource_slice_index,
        "methods": output_methods,
        "domain_types": domain_types,
        "stats": {
            "methods_input_total": methods_input_total,
            "methods_primary_unique": methods_primary_unique,
            "methods_body_ref_rows_added": methods_body_ref_rows_added,
            "methods_deduped_total": methods_primary_unique,
            "methods_duplicates_removed": max(methods_input_total - methods_primary_unique, 0),
            "methods_total": len(output_methods),
            "methods_extracted_ok": len(
                [m for m in output_methods if m.get("source_code", "").strip()]
            ),
            "methods_empty_body": len(
                [m for m in output_methods if not str(m.get("source_code") or "").strip()]
            ),
            "methods_fallback": 0,
            "methods_failed": 0,
            "unique_source_files": len(
                {m.get("source_file") for m in output_methods if m.get("source_file")}
            ),
        },
    }

    if output_path is not None:
        write_json(output_path, payload)
        log(f"[code_context_loader] sliced methods saved: {output_path}", "blue")

    stats = payload.get("stats", {})
    log(
        f"[code_context_loader] context-slicer stats — "
        f"total={stats.get('methods_total', '?')}, "
        f"primary={stats.get('methods_primary_unique', '?')}, "
        f"body_ref_rows={stats.get('methods_body_ref_rows_added', '?')}, "
        f"ok={stats.get('methods_extracted_ok', '?')}, "
        f"failed={stats.get('methods_failed', '?')}, "
        f"visible_resource_ids={len(visible_resource_ids)}, "
        f"domain_types={len(domain_types)}",
        "blue",
    )
    return payload


# ─────────────────────────────────────────────
# Variant 4: method-cfg-index 기반 CFG 컨텍스트
# ─────────────────────────────────────────────

def build_cfg_context_from_method_cfg_index(
    *,
    a11y_path: Path,
    context_slicer_dir: Path,
    method_cfg_index_path: Path,
) -> dict[str, Any]:
    """Variant 4: context-slicer-output + method-cfg-index + a11y로 CFG payload 리스트를 생성한다.

    a11y resource-id로 visible MethodSlice만 필터링하고, 각 slice의 method_body_refs에서
    method_full_name을 수집한다. method-cfg-index에서 method_full_name → cfg_path를 조회하여
    CFG JSON을 로드한다.

    Args:
        a11y_path: adb dump로 생성된 a11y XML 파일 경로.
        context_slicer_dir: Joern context-slicer-output 디렉토리 경로.
        method_cfg_index_path: Joern method-cfg-index.json 경로.

    Returns:
        {"method_cfg_list": [cfg_raw_payload, ...], "meta": {...}}
    """
    if not a11y_path.exists():
        raise FileNotFoundError(f"a11y xml not found: {a11y_path}")
    if not method_cfg_index_path.exists():
        raise FileNotFoundError(f"method-cfg-index.json not found: {method_cfg_index_path}")

    a11y_xml_text = a11y_path.read_text(encoding="utf-8")
    visible_resource_ids = _extract_resource_ids_from_a11y(a11y_xml_text)

    # slices만 로드 (CFG 조회에는 method_body_refs만 필요)
    method_slices, _, _ = load_context_slicer_output(context_slicer_dir)
    index_dir = method_cfg_index_path.parent

    # method-cfg-index 로드: method_full_name -> {method_key, cfg_path}
    index_data = read_json(method_cfg_index_path)
    methods_index: list[dict[str, Any]] = index_data.get("methods") or []
    name_to_entry: dict[str, dict[str, Any]] = {}
    for m in methods_index:
        if not isinstance(m, dict):
            continue
        full_name = str(m.get("method_full_name") or "").strip()
        if full_name:
            name_to_entry[full_name] = m

    # method_body_refs에서 method_full_name 수집 (a11y 필터된 slice만)
    # 신규 스키마: involved_methods 대신 method_body_refs (문자열 목록)
    method_full_names: set[str] = set()
    for ms in method_slices:
        if ms.get("incomplete"):
            continue
        affecting = ms.get("affecting_usages") or []
        has_visible = False
        for usage in affecting:
            anchor_id = usage.get("anchor_id")
            if isinstance(anchor_id, str) and anchor_id.strip() and anchor_id in visible_resource_ids:
                has_visible = True
                break
        if not has_visible:
            continue

        # 신규 스키마: method_body_refs는 문자열 목록 (기존 involved_methods[i]["method_full_name"])
        body_refs = ms.get("method_body_refs") or []
        for ref in body_refs:
            if isinstance(ref, str) and ref.strip():
                method_full_names.add(ref.strip())

    cfg_list: list[dict[str, Any]] = []
    for full_name in sorted(method_full_names):
        entry = name_to_entry.get(full_name)
        if not entry:
            log(f"[code_context_loader] CFG not in index: {full_name}", "yellow")
            continue

        cfg_path_str = str(entry.get("cfg_path") or "")
        method_key = str(entry.get("method_key") or "")

        cfg_path_resolved: Path | None = None
        if cfg_path_str:
            abs_path = Path(cfg_path_str)
            if abs_path.exists():
                cfg_path_resolved = abs_path
        if cfg_path_resolved is None and method_key:
            fallback = index_dir / "method-cfg-reports" / "json" / f"{method_key}.json"
            if fallback.exists():
                cfg_path_resolved = fallback

        if cfg_path_resolved is None:
            log(f"[code_context_loader] CFG file not found for {full_name}", "yellow")
            continue

        try:
            raw = read_json(cfg_path_resolved)
            if isinstance(raw, dict) and raw.get("analysis"):
                cfg_list.append(raw)
        except Exception as e:
            log(f"[code_context_loader] failed to load CFG {cfg_path_resolved}: {e}", "yellow")

    log(
        f"[code_context_loader] variant 4: loaded {len(cfg_list)} CFGs from method_body_refs "
        f"(visible_resource_ids={len(visible_resource_ids)}, unique_methods={len(method_full_names)})",
        "blue",
    )

    return {
        "method_cfg_list": cfg_list,
        "meta": {
            "source": "method_cfg_index",
            "context_slicer_dir": str(context_slicer_dir),
            "method_cfg_index_path": str(method_cfg_index_path),
            "a11y_path": str(a11y_path),
            "involved_methods_count": len(method_full_names),
            "cfg_loaded_count": len(cfg_list),
        },
    }


# ─────────────────────────────────────────────
# 유틸: 기존 정적 분석 JSON 로드 (레거시)
# ─────────────────────────────────────────────

def load_static_analysis_json(path: Path) -> dict[str, Any]:
    """Variant 4: 외부(Joern)에서 사전 생성된 정적 분석 결과 JSON을 로드한다.

    본 파이프라인은 Joern을 직접 실행하지 않는다. Joern이 생성한 JSON을 단순히 읽어
    딕셔너리로 반환하는 역할만 수행한다 (AGENTS.md §4 — 정적 분석 엔진 자체 구현 금지).

    지원하는 JSON 구조:
      - 오브젝트(dict): resource-id를 키로 사용하는 형태 (PROJECT_SPEC §5 기준 표준 형식)
      - 배열(list): Joern 쿼리 결과 배열 형태 (cfa-dfa-hints.json 등 실제 출력 포맷)

    Args:
        path: 정적 분석 결과 JSON 파일 경로.

    Returns:
        {
          "payload": dict | list,   # 원본 JSON 페이로드
          "sha256": str,            # 파일 SHA-256 해시 (실험 재현성용)
          "path": str,              # 파일 경로 문자열
        }

    Raises:
        FileNotFoundError: 파일이 존재하지 않는 경우.
        ValueError: JSON 파싱 실패 시.
    """
    if not path.exists():
        raise FileNotFoundError(f"static analysis JSON not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid static analysis JSON at {path}: {e}") from e

    if not isinstance(payload, (dict, list)):
        raise ValueError(
            f"static analysis JSON must be an object or array, "
            f"got {type(payload).__name__}: {path}"
        )

    file_sha256 = sha256_file(path)

    if isinstance(payload, dict):
        item_count = len(payload)
        shape = "object"
    else:
        item_count = len(payload)
        shape = "array"

    log(
        f"[code_context_loader] variant 4: loaded static analysis JSON "
        f"({shape}, {item_count} items) from {path.name}",
        "blue",
    )

    return {
        "payload": payload,
        "sha256": file_sha256,
        "path": str(path),
    }


# ─────────────────────────────────────────────
# 유틸리티: 기존 Predicate 로드
# ─────────────────────────────────────────────

def load_existing_predicates(predicate_path: Path) -> list[dict[str, Any]]:
    """저장된 predicates.json에서 State_Definitions 목록을 로드한다.

    Args:
        predicate_path: predicates.json 파일 경로.

    Returns:
        State_Definitions 리스트. 파일이 없거나 파싱 실패 시 빈 리스트.
    """
    if not predicate_path.exists():
        log(f"[code_context_loader] predicate file not found: {predicate_path}", "yellow")
        return []

    try:
        data = read_json(predicate_path)
    except Exception as e:
        log(f"[code_context_loader] failed to load predicates: {e}", "yellow")
        return []

    defs = data.get("State_Definitions") or data.get("Predicates", [])
    if not isinstance(defs, list):
        return []

    log(f"[code_context_loader] loaded {len(defs)} existing predicates", "blue")
    return defs
