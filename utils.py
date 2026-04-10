"""유틸리티 함수들."""

import re

# 게임 텍스트 태그 정리
_TAG_RE = re.compile(r"\[/?(?:gold|star:\d+|b|i|u|color=[^\]]*)\]")
_STAR_RE = re.compile(r"\[star:(\d+)\]")


def clean_description(text: str) -> str:
    """게임 설명 텍스트에서 [gold], [star:N] 등 태그 제거."""
    # [star:1] → ★1 로 변환
    text = _STAR_RE.sub(r"★\1", text)
    # 나머지 태그 제거
    text = _TAG_RE.sub("", text)
    return text
