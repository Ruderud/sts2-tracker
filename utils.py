"""유틸리티 함수들."""

import re

# 게임 텍스트 태그 정리
_STAR_RE = re.compile(r"\[star:(\d+)\]")
_TAG_RE = re.compile(r"\[[^\]]+\]")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_game_text(text: str) -> str:
    """게임 설명/이벤트 텍스트에서 스타일 태그 제거."""
    text = _STAR_RE.sub(r"★\1", text or "")
    text = _TAG_RE.sub("", text)
    text = text.replace("\\n", "\n")
    return text.strip()


def normalize_ocr_text(text: str) -> str:
    """OCR/Fuzzy 매칭용 정규화."""
    text = clean_game_text(text).lower()
    text = _WHITESPACE_RE.sub("", text)
    return text


def clean_description(text: str) -> str:
    """기존 카드 설명 정리 호환 함수."""
    return clean_game_text(text)
