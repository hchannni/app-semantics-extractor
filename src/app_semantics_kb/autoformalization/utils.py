"""
utils.py — autoformalization 공용 유틸리티 모듈

target-home autoformalization 패키지에서 공유하는 기반 함수들을 제공한다.
legacy lane의 공통 유틸 인터페이스와 호환되도록 유지한다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COLOR: dict[str, str] = {
    "red": "\033[1;91m",
    "green": "\033[1;92m",
    "yellow": "\033[1;93m",
    "blue": "\033[1;94m",
    "magenta": "\033[1;95m",
    "cyan": "\033[1;96m",
    "white": "\033[1;97m",
    "reset": "\033[0;0m",
}


def log(msg: str, color: str = "white") -> None:
    """컬러 로그를 stdout에 출력한다."""
    prefix = COLOR.get(color, COLOR["white"])
    reset = COLOR["reset"]
    print(f"{prefix}{msg}{reset}")


def iso_now() -> str:
    """UTC 기준 ISO 8601 형식의 현재 시각을 반환한다."""
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    """파일의 SHA-256 해시를 반환한다."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    """payload를 JSON으로 직렬화하여 path에 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def read_json(path: Path) -> dict[str, Any]:
    """path에서 JSON 오브젝트를 읽어 dict로 반환한다."""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}, got {type(payload).__name__}")
    return payload


def read_json_array(path: Path) -> list[dict[str, Any]]:
    """path에서 JSON 배열을 읽어 list[dict]로 반환한다."""
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, list):
        raise ValueError(f"expected JSON array in {path}, got {type(payload).__name__}")
    return [item for item in payload if isinstance(item, dict)]


def pretty_json(obj: Any) -> str:
    """Python 객체를 들여쓰기된 JSON 문자열로 반환한다."""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def try_parse_json(text: str) -> Any | None:
    """문자열을 JSON으로 파싱 시도한다. 실패 시 None을 반환한다."""
    try:
        return json.loads(text)
    except Exception:
        return None
