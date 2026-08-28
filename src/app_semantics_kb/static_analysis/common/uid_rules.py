from __future__ import annotations

import hashlib


def stable_uid(prefix: str, *parts: object) -> str:
    raw = "||".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def anchor_uid(resource_id: str, anchor_name: str, location: str) -> str:
    return stable_uid("anchor", resource_id, anchor_name, location)


def method_uid(method_full_name: str) -> str:
    return stable_uid("method", method_full_name)


def evidence_uid(
    anchor_ref: str,
    method_full_name: str,
    code_snippet: object,
    source_file: object,
    start_line: object,
) -> str:
    return stable_uid(
        "evidence",
        anchor_ref,
        method_full_name,
        code_snippet,
        source_file,
        start_line,
    )


def handler_uid(root_callback: str, callback_kind: str, resource_id: str) -> str:
    return stable_uid("handler", root_callback, callback_kind, resource_id)


def type_uid(type_full_name: str) -> str:
    return stable_uid("type", type_full_name)
