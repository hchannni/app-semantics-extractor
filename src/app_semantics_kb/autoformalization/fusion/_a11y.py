"""fusion._a11y — accessibility XML 경량화 헬퍼.

context_merger의 dispatcher가 호출하는 단일 책임 모듈.
"""

from __future__ import annotations

from ..extractors.a11y_tree_parser import A11yTreeParser
from ..utils import log


def compact_a11y(xml_text: str) -> str:
    """a11y XML 원문을 경량화한다. 구조는 유지하고 false/빈 값 속성만 제거한다.

    파싱 실패 시 원문을 그대로 반환하여 파이프라인이 중단되지 않게 한다.
    """
    if not xml_text.strip():
        return ""
    try:
        return A11yTreeParser().compact_xml(xml_text, pretty=True)
    except Exception as e:
        log(f"[fusion._a11y] compact failed, using raw XML: {e}", "yellow")
        return xml_text
