"""STS2 오프닝 선택지 추천 엔진."""

from __future__ import annotations

import re

from recommender import analyze_deck
from save_parser import RunState
from screen_capture import DetectedChoice
from utils import clean_game_text


_NUMBER_RE = re.compile(r"(\d+)")


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _extract_numbers(text: str) -> list[int]:
    return [int(match) for match in _NUMBER_RE.findall(text or "")]


def _choice_text(option: DetectedChoice) -> str:
    title = clean_game_text(option.title)
    description = clean_game_text(option.description)
    return f"{title}\n{description}".strip()


def score_opening_choice(
    option: DetectedChoice,
    state: RunState,
    cards_db: list[dict],
) -> tuple[float, list[str]]:
    analysis = analyze_deck(state, cards_db)
    basics_ratio = analysis["basics"] / max(analysis["total"], 1)
    text = _choice_text(option)
    numbers = _extract_numbers(text)

    score = 2.0
    reasons: list[str] = []

    if "덱에서" in text and "제거" in text:
        remove_count = max(numbers) if numbers else 1
        bonus = _clamp(0.8 * remove_count + basics_ratio * 1.0, 0.8, 1.9)
        score += bonus
        reasons.append(f"초반제거 +{bonus:.1f}")

    if "골드" in text and ("얻" in text or "획득" in text):
        gain = max(numbers) if numbers else 100
        bonus = _clamp(gain / 72 + (0.35 if state.floor <= 1 else 0.0), 0.8, 2.6)
        score += bonus
        reasons.append(f"초반골드 +{bonus:.1f}")

    if "카드 보상" in text and "강화" in text:
        reward_count = max((number for number in numbers if number <= 5), default=3)
        bonus = _clamp(0.4 + reward_count * 0.3, 0.9, 1.6)
        score += bonus
        reasons.append(f"강화보상 +{bonus:.1f}")

    if "보물 상자" in text and ("비어" in text or "없" in text):
        penalty = 1.65 if state.act == 0 else 1.35
        score -= penalty
        reasons.append(f"보물손실 -{penalty:.1f}")

    if "유물" in text and ("얻" in text or "획득" in text):
        bonus = 1.15
        score += bonus
        reasons.append(f"유물 +{bonus:.1f}")

    if "최대 체력" in text and ("얻" in text or "증가" in text):
        amount = max(numbers) if numbers else 3
        bonus = _clamp(amount * 0.18, 0.5, 1.4)
        score += bonus
        reasons.append(f"최대체력 +{bonus:.1f}")

    if "포션" in text and ("얻" in text or "생성" in text):
        bonus = 0.25 if len(state.potions) < state.max_potion_slots else 0.05
        score += bonus
        reasons.append(f"포션 +{bonus:.1f}")

    if not reasons:
        reasons.append("무난")

    return round(_clamp(score, 0.0, 6.5), 2), reasons[:4]


def recommend_opening_choices(
    options: list[DetectedChoice],
    state: RunState,
    cards_db: list[dict],
) -> dict | None:
    if len(options) < 2:
        return None

    ranked = []
    for option in options:
        score, reasons = score_opening_choice(option, state, cards_db)
        ranked.append(
            {
                "id": f"OPENING_{option.position}",
                "title": clean_game_text(option.title),
                "description": clean_game_text(option.description),
                "score": score,
                "reasons": reasons,
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return {
        "event_id": "OPENING_CHOICE",
        "event_name": "시작 선택",
        "page_id": "INITIAL",
        "match_score": 1.0,
        "options": ranked,
        "best_idx": 0,
    }
