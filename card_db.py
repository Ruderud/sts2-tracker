"""STS2 카드 데이터베이스 - Spire Codex API에서 가져온 한국어 카드 데이터."""

import json
import os
from difflib import SequenceMatcher
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "cards_kor.json"


def download_card_db():
    """Spire Codex API에서 한국어 카드 데이터 다운로드."""
    import urllib.request

    url = "https://spire-codex.com/api/cards?lang=kor"
    os.makedirs(DB_PATH.parent, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "STS2Tracker/1.0"})
    with urllib.request.urlopen(req) as resp:
        with open(DB_PATH, "wb") as f:
            f.write(resp.read())
    print(f"Downloaded {DB_PATH}")


def load_card_db() -> list[dict]:
    """카드 DB 로드. 없으면 다운로드."""
    if not DB_PATH.exists():
        download_card_db()
    with open(DB_PATH) as f:
        return json.load(f)


def build_name_index(cards: list[dict]) -> dict[str, dict]:
    """한국어 이름 → 카드 데이터 매핑."""
    return {card["name"]: card for card in cards}


def fuzzy_match(query: str, cards: list[dict], threshold: float = 0.5) -> list[tuple[dict, float]]:
    """OCR 텍스트를 카드 이름+설명과 매칭.

    Returns: [(card_data, score)] sorted by score desc.
    """
    results = []
    query_clean = query.strip().replace(" ", "")
    for card in cards:
        # 이름 매칭 - 쿼리에 카드 이름이 포함되면 높은 점수
        name_clean = card["name"].strip().replace(" ", "")
        name_score = SequenceMatcher(None, query_clean, name_clean).ratio()
        if len(name_clean) >= 2 and name_clean in query_clean:
            name_score = max(name_score, 0.95)

        # 설명 매칭 - 부분 문자열 및 슬라이딩 윈도우
        desc = (card.get("description") or "").replace(" ", "").replace("\n", "")
        # [gold], [star:N] 같은 태그 제거
        import re
        desc_plain = re.sub(r"\[/?[a-z:0-9]+\]", "", desc)
        desc_score = 0.0
        if len(query_clean) >= 4 and len(desc_plain) > 0:
            # 쿼리가 설명에 포함
            if query_clean in desc_plain:
                desc_score = min(len(query_clean) / max(len(desc_plain), 1) + 0.5, 0.95)
            else:
                # 슬라이딩 윈도우: 쿼리 길이만큼 설명을 잘라서 최대 유사도
                qlen = len(query_clean)
                best_partial = 0.0
                for start in range(0, max(1, len(desc_plain) - qlen + 1), max(1, qlen // 4)):
                    window = desc_plain[start:start + qlen]
                    partial = SequenceMatcher(None, query_clean, window).ratio()
                    best_partial = max(best_partial, partial)
                desc_score = best_partial * 0.85

        score = max(name_score, desc_score)
        if score >= threshold:
            results.append((card, score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def match_card(ocr_text: str, cards: list[dict]) -> dict | None:
    """OCR 텍스트에서 가장 가까운 카드 찾기."""
    matches = fuzzy_match(ocr_text, cards, threshold=0.3)
    return matches[0][0] if matches else None


if __name__ == "__main__":
    cards = load_card_db()
    print(f"Loaded {len(cards)} cards")

    # 테스트: EasyOCR 결과 매칭 (이름 + 설명 텍스트)
    test_queries = [
        "천상의 권능",
        "버린 카드 더미의 카드 1장 뽑을 카드 더미 맨 위에 놓습니다",
        "보유한 카드중 비용 카드 1장당 피해 증가합니다",
    ]
    for q in test_queries:
        matches = fuzzy_match(q, cards, threshold=0.3)
        print(f"\n'{q}' →")
        for card, score in matches[:3]:
            print(f"  [{score:.2f}] {card['id']} ({card['name']})")
